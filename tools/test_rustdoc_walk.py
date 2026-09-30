# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/rustdoc_walk.py`, the stitcher behind `verify/parity/core-api.json` (#271).

microvms-core re-exports most of its API from the crates below it, and rustdoc JSON doesn't
inline a re-export from another crate: core's own JSON names a moved type and nothing inside it.
The stitcher follows each such re-export into the JSON of the crate that defines it. These cases
build a two-crate workspace with the real toolchain and the same `build` the snapshot uses,
because a stitcher tested against hand-written JSON proves the hand-written JSON.

cargo comes from `mise.toml`'s toolchain, so run this under `mise x` or a mise task.
"""

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

import rustdoc_walk

# `inner` defines everything; `outer` is the composition root that re-exports it, the way
# microvms-core re-exports microvms-app. Each item is here for one branch of the walk.
INNER = """
pub mod things {
    #[derive(Clone)]
    pub struct Thing;

    impl Thing {
        pub fn go(&self) {}
        pub fn named(_name: impl Into<String>) -> Self {
            Thing
        }
        pub const LIMIT: u32 = 1;
        #[allow(dead_code)]
        fn private(&self) {}
    }

    pub enum Mode {
        Fast,
        Slow,
    }

    pub trait Store {
        fn release(&self);
        fn count(&self) -> usize {
            0
        }
    }

    impl Store for Thing {
        fn release(&self) {}
    }
}

mod hidden {
    pub struct Moved;

    impl Moved {
        pub fn arrive(&self) {}
    }
}

pub use hidden::Moved;

pub mod shadowed {
    pub struct Kept;
    pub struct Replaced;

    // Another kind in the same namespace as `outer`'s struct: a walk that only skips a path
    // it has seen with the same kind would still list these variants under the struct.
    pub enum Swapped {
        FromInner,
    }

    impl Replaced {
        pub fn from_inner(&self) {}
    }
}

pub fn helper() {}
"""

OUTER = """
pub use inner::things::{Mode, Store, Thing};
pub use inner::{helper, Moved};
pub use std::time::Duration;

mod local {
    pub struct Replaced;

    impl Replaced {
        pub fn from_outer(&self) {}
    }
}

// Both ways core merges a module with halves in two crates: a local item, and an explicit
// re-export listed after the glob, as `microvms_core::adapters` does.
pub mod shadowed {
    pub use inner::shadowed::*;
    pub use crate::local::Replaced;

    pub struct Swapped;

    impl Swapped {
        pub fn from_outer(&self) {}
    }
}

pub mod prelude {
    pub trait ThingExt {
        fn new() -> Self;
    }

    impl ThingExt for inner::things::Thing {
        fn new() -> Self {
            inner::things::Thing
        }
    }
}

pub const VERSION: &str = "1";
"""

EXPECTED = {
    "outer::Duration": "external",
    "outer::Mode": "enum",
    "outer::Mode::Fast": "variant",
    "outer::Mode::Slow": "variant",
    "outer::Moved": "struct",
    "outer::Moved::arrive": "method",
    "outer::Store": "trait",
    "outer::Store::count": "method",
    "outer::Store::release": "method",
    "outer::Thing": "struct",
    "outer::Thing::LIMIT": "assoc_const",
    "outer::Thing::go": "method",
    "outer::Thing::named": "method",
    "outer::Thing::new": "method via outer::prelude::ThingExt",
    "outer::VERSION": "constant",
    "outer::helper": "function",
    "outer::prelude": "module",
    "outer::prelude::ThingExt": "trait",
    "outer::prelude::ThingExt::new": "method",
    "outer::shadowed": "module",
    "outer::shadowed::Kept": "struct",
    "outer::shadowed::Replaced": "struct",
    "outer::shadowed::Replaced::from_outer": "method",
    "outer::shadowed::Swapped": "struct",
    "outer::shadowed::Swapped::from_outer": "method",
}

READ = ("outer", "inner")


def write_workspace(root: Path) -> None:
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["inner", "outer"]\nresolver = "3"\n',
        "inner/Cargo.toml": '[package]\nname = "inner"\nversion = "0.1.0"\nedition = "2024"\n',
        "inner/src/lib.rs": INNER,
        "outer/Cargo.toml": (
            '[package]\nname = "outer"\nversion = "0.1.0"\nedition = "2024"\n\n'
            '[dependencies]\ninner = { path = "../inner" }\n'
        ),
        "outer/src/lib.rs": OUTER,
    }
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


class FixtureWorkspace(unittest.TestCase):
    """Builds the fixture's rustdoc JSON once; each case reads it or a tampered copy."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        root = Path(cls.scratch.name)
        write_workspace(root / "ws")
        cls.doc_dir = rustdoc_walk.build(
            root / "ws", ["inner", "outer"], target_dir=root / "target"
        )
        cls.surface = rustdoc_walk.surface(
            rustdoc_walk.stitch(cls.doc_dir, "outer", READ)
        )

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def tampered(self, crate: str, change) -> Path:
        """A copy of the fixture's doc directory with `change` applied to one crate's JSON."""
        copy = tempfile.TemporaryDirectory()
        self.addCleanup(copy.cleanup)
        target = Path(copy.name)
        for name in READ:
            shutil.copy(self.doc_dir / f"{name}.json", target / f"{name}.json")
        doc = json.loads((target / f"{crate}.json").read_text(encoding="utf-8"))
        change(doc)
        (target / f"{crate}.json").write_text(json.dumps(doc), encoding="utf-8")
        return target

    def test_a_reexport_from_another_crate_resolves_with_its_members(self):
        # `outer`'s own JSON has a `use` of an external id for each of these and nothing
        # inside it. The members come from `inner`'s JSON, and `Moved` is found although
        # its definition sits in a private module of `inner`.
        for path in (
            "outer::Thing::go",
            "outer::Thing::LIMIT",
            "outer::Mode::Fast",
            "outer::Store::count",
            "outer::Moved::arrive",
            "outer::helper",
        ):
            self.assertIn(path, self.surface)
        self.assertFalse(
            [path for path in self.surface if path.startswith("inner::")],
            "the stitched surface names paths at the root crate only",
        )

    def test_an_extension_trait_method_is_named_on_the_type_it_extends(self):
        # The prelude's `impl SandboxExt for Sandbox` is how core keeps `Sandbox::new`; a
        # caller writes the type's path, so the snapshot does too.
        self.assertEqual(
            self.surface.get("outer::Thing::new"), "method via outer::prelude::ThingExt"
        )
        self.assertEqual(self.surface.get("outer::prelude::ThingExt::new"), "method")

    def test_a_local_item_shadows_what_a_glob_brings_in(self):
        self.assertIn("outer::shadowed::Kept", self.surface)
        self.assertEqual(self.surface.get("outer::shadowed::Swapped"), "struct")
        self.assertIn("outer::shadowed::Swapped::from_outer", self.surface)
        self.assertNotIn("outer::shadowed::Swapped::FromInner", self.surface)
        self.assertIn("outer::shadowed::Replaced::from_outer", self.surface)
        self.assertNotIn("outer::shadowed::Replaced::from_inner", self.surface)

    def test_an_unread_crate_is_named_and_a_foreign_trait_impl_is_not_listed(self):
        self.assertEqual(self.surface.get("outer::Duration"), "external")
        self.assertNotIn("outer::Thing::clone", self.surface)
        self.assertNotIn("outer::Thing::release", self.surface)

    def test_the_stitched_surface_is_exactly_the_fixtures(self):
        self.assertEqual(self.surface, EXPECTED)

    def test_a_method_with_a_type_parameter_is_marked_generic(self):
        # generate-public-paths.py can't name one without its arguments, so it skips these.
        entries = {
            entry.path: entry
            for entry in rustdoc_walk.stitch(self.doc_dir, "outer", READ)
        }
        self.assertTrue(entries["outer::Thing::named"].generic)
        self.assertFalse(entries["outer::Thing::go"].generic)

    def test_an_unknown_format_version_is_refused_naming_it(self):
        doc_dir = self.tampered("inner", lambda doc: doc.update(format_version=9999))
        with self.assertRaisesRegex(rustdoc_walk.StitchError, "format_version 9999"):
            rustdoc_walk.stitch(doc_dir, "outer", READ)

    def test_an_empty_index_is_refused(self):
        doc_dir = self.tampered("inner", lambda doc: doc.update(index={}))
        with self.assertRaisesRegex(rustdoc_walk.StitchError, "empty index"):
            rustdoc_walk.stitch(doc_dir, "outer", READ)

    def test_a_crate_it_reads_must_have_its_json(self):
        doc_dir = self.tampered("inner", lambda doc: None)
        (doc_dir / "inner.json").unlink()
        with self.assertRaisesRegex(rustdoc_walk.StitchError, "inner.json"):
            rustdoc_walk.stitch(doc_dir, "outer", READ)

    def test_a_reexport_the_defining_crate_lacks_is_refused(self):
        # A target missing from the crate that should define it means the two JSON files
        # disagree, and recording the path alone would drop everything under it unseen.
        def drop_moved(doc):
            doc["paths"] = {
                key: value
                for key, value in doc["paths"].items()
                if value["path"][-1] != "Moved"
            }

        doc_dir = self.tampered("inner", drop_moved)
        with self.assertRaisesRegex(rustdoc_walk.StitchError, "Moved"):
            rustdoc_walk.stitch(doc_dir, "outer", READ)


if __name__ == "__main__":
    unittest.main()
