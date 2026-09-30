# SPDX-License-Identifier: Apache-2.0
"""The schema compatibility check passes every real release and fails a change a caller can't read (#298).

The fixtures are the `docs/schema.json` each of v0.8.0, v0.9.0 and v0.10.0 shipped, copied
from their tags so the cases run in a shallow checkout. Their transitions carry the changes a
field-level rule gets wrong: `ExitEvent.timed_out`, `Health.identity_steps` and
`Health.image_env_keys` became required in responses, each with a serde default, and
`PROTOCOL_VERSION` stayed `1` throughout. Every case runs the real script, on a fixture or on
a copy of one with a single thing broken, and a failing case requires the output to name the
break. The last cases build throwaway git repos with release tags, for how the base is chosen
and for a clone that has no tag to choose.

No case holds the committed schema to a fixture: that's the gate's job, against the real
previous tag. A case pinned to v0.10.0 would go on holding the tree to it after 0.11.0 ships,
and the rule isn't transitive (a field given a default in one release can go in the next).
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("check-schema-compat.py")
ROOT = SCRIPT.parent.parent
FIXTURES = SCRIPT.parent / "fixtures" / "schema-compat"
RELEASES = ("v0.8.0", "v0.9.0", "v0.10.0")

# The pointers a git hook exports, copied from test_license_headers.py (scripts aren't
# importable). Inherited from lefthook, they'd turn the throwaway repo's `git init` and
# `git commit` into writes to the real one.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


def clean_env() -> dict[str, str]:
    """`os.environ` without the inherited git pointers, read at call time."""
    return {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}


def fixture(release: str) -> dict:
    return json.loads((FIXTURES / f"{release}.json").read_text(encoding="utf-8"))


def route(schema: dict, method: str, path: str) -> dict:
    return next(
        r for r in schema["routes"] if r["method"] == method and r["path"] == path
    )


def run_check(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=clean_env(),
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    ).stdout


class SchemaCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, schema: dict | str) -> Path:
        path = self.tmp / name
        text = schema if isinstance(schema, str) else json.dumps(schema, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def compare(
        self, base: dict | str, current: dict | str, breaks: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """The script over two schemas, with `breaks` as the declared list (none by default)."""
        listed = self.tmp / "breaks.toml"
        if breaks is not None:
            listed.write_text(breaks, encoding="utf-8")
        return run_check(
            "--base-file",
            str(self.write("base.json", base)),
            "--schema",
            str(self.write("current.json", current)),
            "--breaks",
            str(listed),
        )

    def assert_compatible(self, current: dict, base: dict | None = None):
        result = self.compare(base or fixture("v0.10.0"), current)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("compatible", result.stdout)

    def assert_breaks(self, current: dict, *needles: str, base: dict | None = None):
        result = self.compare(base or fixture("v0.10.0"), current)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for needle in needles:
            self.assertIn(needle, result.stdout)

    def assert_refused(self, base: dict | str, current: dict | str, needle: str):
        result = self.compare(base, current)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)

    # ── every real release passes ─────────────────────────────────────────────

    def test_the_fixtures_carry_the_changes_a_field_level_rule_fails_on(self):
        """Without these, the transition cases below would pass on fixtures with no change in them."""
        old, mid, new = (fixture(r)["$defs"] for r in RELEASES)
        self.assertNotIn("timed_out", old["ExitEvent"]["required"])
        self.assertIn("timed_out", mid["ExitEvent"]["required"])
        self.assertIn("default", mid["ExitEvent"]["properties"]["timed_out"])
        self.assertNotIn("identity_steps", old["Health"]["required"])
        self.assertIn("identity_steps", mid["Health"]["required"])
        self.assertNotIn("image_env_keys", mid["Health"]["required"])
        self.assertIn("image_env_keys", new["Health"]["required"])
        self.assertTrue(
            all(fixture(r)["protocol_version"] == "1" for r in RELEASES),
            "every fixture is protocol version 1",
        )

    def test_v0_8_0_to_v0_9_0_is_compatible(self):
        result = self.compare(fixture("v0.8.0"), fixture("v0.9.0"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("compatible", result.stdout)

    def test_v0_9_0_to_v0_10_0_is_compatible(self):
        result = self.compare(fixture("v0.9.0"), fixture("v0.10.0"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v0_8_0_to_v0_10_0_is_compatible(self):
        result = self.compare(fixture("v0.8.0"), fixture("v0.10.0"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ── the two seeded faults ────────────────────────────────────────────────

    def test_a_request_field_made_required_fails(self):
        """An older client that omits `cwd` would be refused by this daemon."""
        schema = fixture("v0.10.0")
        schema["$defs"]["StartRequest"]["required"].append("cwd")
        self.assert_breaks(schema, "request StartRequest.cwd is required now")

    def test_a_removed_route_fails(self):
        schema = fixture("v0.10.0")
        schema["routes"].remove(route(schema, "GET", "/v1/procs"))
        self.assert_breaks(schema, "route GET /v1/procs is gone")

    # ── the rest of the direction-aware rule ─────────────────────────────────

    def test_a_removed_optional_request_field_fails(self):
        """A client that still sends it has it ignored, since no request denies unknown fields."""
        schema = fixture("v0.10.0")
        del schema["$defs"]["StartRequest"]["properties"]["cwd"]
        self.assert_breaks(schema, "request StartRequest.cwd is gone")

    def test_a_newly_required_query_field_fails(self):
        schema = fixture("v0.10.0")
        query = schema["$defs"]["StreamQuery"]
        query["required"] = [*query.get("required", []), "offset"]
        self.assert_breaks(schema, "request StreamQuery.offset is required now")

    def test_a_removed_required_response_field_fails(self):
        schema = fixture("v0.10.0")
        health = schema["$defs"]["Health"]
        health["required"].remove("bootstrapped")
        del health["properties"]["bootstrapped"]
        self.assert_breaks(schema, "response Health.bootstrapped is gone")

    def test_a_removed_response_field_with_a_default_passes(self):
        """An older client fills the field from its serde default when it's absent."""
        schema = fixture("v0.10.0")
        health = schema["$defs"]["Health"]
        health["required"].remove("image_env_keys")
        del health["properties"]["image_env_keys"]
        result = self.compare(fixture("v0.10.0"), schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_added_required_response_field_with_no_default_fails(self):
        """This client would refuse an older daemon's answer, which lacks the field."""
        schema = fixture("v0.10.0")
        health = schema["$defs"]["Health"]
        health["properties"]["uptime_secs"] = {"type": "integer"}
        health["required"].append("uptime_secs")
        self.assert_breaks(schema, "response Health.uptime_secs is required now")

    def test_an_added_required_response_field_with_a_default_passes(self):
        schema = fixture("v0.10.0")
        health = schema["$defs"]["Health"]
        health["properties"]["uptime_secs"] = {"type": "integer", "default": 0}
        health["required"].append("uptime_secs")
        result = self.compare(fixture("v0.10.0"), schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_added_optional_request_field_passes(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["StartRequest"]["properties"]["nice"] = {"type": "integer"}
        result = self.compare(fixture("v0.10.0"), schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_renamed_definition_with_its_shape_kept_passes(self):
        """Shapes are compared where a route uses them; the name isn't on the wire."""
        schema = fixture("v0.10.0")
        schema["$defs"]["ExecStartRequest"] = schema["$defs"].pop("StartRequest")
        start = route(schema, "POST", "/v1/exec/start")
        start["request"]["schema"]["$ref"] = "#/$defs/ExecStartRequest"
        self.assert_compatible(schema)

    def test_a_request_field_no_longer_required_fails(self):
        """This client may leave `command` out now, and an older daemon refuses that."""
        schema = fixture("v0.10.0")
        schema["$defs"]["StartRequest"]["required"].remove("command")
        self.assert_breaks(schema, "request StartRequest.command isn't required now")

    def test_a_response_field_no_longer_required_fails(self):
        """What `skip_serializing_if` does: an older client's `truncated: bool` has no default."""
        schema = fixture("v0.10.0")
        schema["$defs"]["ExitEvent"]["required"].remove("truncated")
        self.assert_breaks(
            schema,
            "response ExitEvent.truncated may be absent now",
            "GET /v1/exec/{id}/stream exit event",
        )

    def test_a_nullable_or_defaulted_response_field_no_longer_required_passes(self):
        """serde reads an absent `Option` as `None`, and a defaulted field from its default."""
        schema = fixture("v0.10.0")
        schema["$defs"]["ExitEvent"]["required"].remove("exit_code")
        schema["$defs"]["Health"]["required"].remove("busy")
        self.assert_compatible(schema)

    # ── a route's shapes, compared where the route uses them ─────────────────

    def test_a_route_that_gains_a_required_query_fails(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["ProcsQuery"] = {
            "type": "object",
            "properties": {"exec_id": {"type": "string"}},
            "required": ["exec_id"],
        }
        route(schema, "GET", "/v1/procs")["query"] = {
            "media_type": "application/x-www-form-urlencoded",
            "schema": {"$ref": "#/$defs/ProcsQuery"},
        }
        self.assert_breaks(
            schema, "request ProcsQuery.exec_id is required now", "(GET /v1/procs)"
        )

    def test_a_route_that_gains_a_required_body_fails(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["KillRequest"] = {
            "type": "object",
            "properties": {"signal": {"type": "integer"}},
            "required": ["signal"],
        }
        route(schema, "POST", "/v1/exec/{id}/kill")["request"] = {
            "media_type": "application/json",
            "schema": {"$ref": "#/$defs/KillRequest"},
        }
        self.assert_breaks(schema, "request KillRequest.signal is required now")

    def test_a_route_that_gains_a_body_with_nothing_required_passes(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["KillRequest"] = {
            "type": "object",
            "properties": {"signal": {"type": "integer", "default": 15}},
        }
        route(schema, "POST", "/v1/exec/{id}/kill")["request"] = {
            "media_type": "application/json",
            "schema": {"$ref": "#/$defs/KillRequest"},
        }
        self.assert_compatible(schema)

    def test_a_request_pointed_at_another_definition_fails(self):
        """StdinRequest stays in $defs, so a by-name rule sees nothing change."""
        schema = fixture("v0.10.0")
        schema["$defs"]["StdinRequestV2"] = copy.deepcopy(
            schema["$defs"]["StdinRequest"]
        )
        v2 = schema["$defs"]["StdinRequestV2"]
        v2["properties"]["fd"] = {"type": "integer"}
        v2["required"] = [*v2.get("required", []), "fd"]
        route(schema, "POST", "/v1/exec/{id}/stdin")["request"]["schema"]["$ref"] = (
            "#/$defs/StdinRequestV2"
        )
        self.assert_breaks(schema, "request StdinRequestV2.fd is required now")

    def test_a_response_pointed_at_another_definition_fails(self):
        """`/ack` still answers PollResponse, so the definition itself doesn't change."""
        schema = fixture("v0.10.0")
        poll = route(schema, "GET", "/v1/exec/{id}")
        poll["response"]["schema"]["$ref"] = "#/$defs/KillResponse"
        self.assert_breaks(
            schema, "response PollResponse.phase is gone", "(GET /v1/exec/{id})"
        )

    def test_a_response_body_that_goes_away_fails(self):
        schema = fixture("v0.10.0")
        route(schema, "GET", "/v1/exec/{id}")["response"] = None
        self.assert_breaks(schema, "response PollResponse is gone")

    def test_a_stream_event_pointed_at_another_definition_fails(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["ExitEventV2"] = {
            "type": "object",
            "properties": {"exit_code": {"type": ["integer", "null"]}},
            "required": ["exit_code"],
        }
        stream = route(schema, "GET", "/v1/exec/{id}/stream")
        exit_event = next(e for e in stream["sse_events"] if e["event"] == "exit")
        exit_event["schema"]["$ref"] = "#/$defs/ExitEventV2"
        self.assert_breaks(schema, "response ExitEvent.offset is gone")

    def test_a_field_gone_from_a_stream_event_fails(self):
        """ExitEvent is reached only through the stream route's `sse_events`."""
        schema = fixture("v0.10.0")
        exit_event = schema["$defs"]["ExitEvent"]
        exit_event["required"].remove("offset")
        del exit_event["properties"]["offset"]
        self.assert_breaks(
            schema,
            "response ExitEvent.offset is gone",
            "GET /v1/exec/{id}/stream exit event",
        )

    def test_a_field_gone_from_a_definition_another_one_names_fails(self):
        """DiskHealth is reached only through Health's `disk` field."""
        schema = fixture("v0.10.0")
        disk = schema["$defs"]["DiskHealth"]
        disk["required"].remove("available_bytes")
        del disk["properties"]["available_bytes"]
        self.assert_breaks(
            schema, "response DiskHealth.available_bytes is gone", "(GET /v1/health)"
        )

    def test_a_removed_status_fails(self):
        schema = fixture("v0.10.0")
        ack = route(schema, "POST", "/v1/exec/{id}/ack")
        ack["statuses"] = [s for s in ack["statuses"] if s["error"] != "already_acked"]
        self.assert_breaks(
            schema, "status 409 already_acked of POST /v1/exec/{id}/ack is gone"
        )

    def test_a_removed_stream_event_fails(self):
        schema = fixture("v0.10.0")
        stream = route(schema, "GET", "/v1/exec/{id}/stream")
        stream["sse_events"] = [e for e in stream["sse_events"] if e["event"] != "gap"]
        self.assert_breaks(schema, "event gap of GET /v1/exec/{id}/stream is gone")

    def test_every_break_is_named_not_just_the_first(self):
        schema = fixture("v0.10.0")
        schema["$defs"]["StartRequest"]["required"].append("cwd")
        schema["routes"].remove(route(schema, "GET", "/v1/procs"))
        self.assert_breaks(
            schema,
            "request StartRequest.cwd is required now",
            "route GET /v1/procs is gone",
        )

    def test_a_field_pointed_at_another_definition_is_compared(self):
        """Health's `disk` names DiskHealthV2 now, which dropped a field DiskHealth required."""
        schema = fixture("v0.10.0")
        v2 = copy.deepcopy(schema["$defs"]["DiskHealth"])
        v2["required"].remove("available_bytes")
        del v2["properties"]["available_bytes"]
        schema["$defs"]["DiskHealthV2"] = v2
        schema["$defs"]["Health"]["properties"]["disk"]["anyOf"][0]["$ref"] = (
            "#/$defs/DiskHealthV2"
        )
        self.assert_breaks(schema, "response DiskHealth.available_bytes is gone")

    # ── PROTOCOL_VERSION ─────────────────────────────────────────────────────

    def bumped_without_procs(self) -> dict:
        schema = fixture("v0.10.0")
        schema["protocol_version"] = "2"
        schema["routes"].remove(route(schema, "GET", "/v1/procs"))
        return schema

    def test_a_declared_break_under_a_bumped_version_passes_and_is_listed(self):
        result = self.compare(
            fixture("v0.10.0"),
            self.bumped_without_procs(),
            '[[break]]\nchange = "route GET /v1/procs is gone"\nwhy = "renamed"\n',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("protocol_version changed from 1 to 2", result.stdout)
        self.assertIn("route GET /v1/procs is gone", result.stdout)

    def test_a_bumped_version_alone_waives_nothing(self):
        """Until the next tag every change compares with the old version, so a bump can't be a pass."""
        result = self.compare(fixture("v0.10.0"), self.bumped_without_procs())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not declared", result.stdout)
        self.assertIn("route GET /v1/procs is gone", result.stdout)

    def test_a_later_break_under_the_same_bump_fails(self):
        schema = self.bumped_without_procs()
        schema["routes"].remove(route(schema, "GET", "/v1/health"))
        result = self.compare(
            fixture("v0.10.0"),
            schema,
            '[[break]]\nchange = "route GET /v1/procs is gone"\nwhy = "renamed"\n',
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not declared", result.stdout)
        self.assertIn("route GET /v1/health is gone", result.stdout)

    def test_a_declared_break_that_isnt_found_fails(self):
        schema = fixture("v0.10.0")
        schema["protocol_version"] = "2"
        result = self.compare(
            fixture("v0.10.0"),
            schema,
            '[[break]]\nchange = "route GET /v1/procs is gone"\nwhy = "renamed"\n',
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("declared but not found", result.stdout)

    def test_a_protocol_version_change_with_no_break_still_says_so(self):
        schema = fixture("v0.10.0")
        schema["protocol_version"] = "2"
        result = self.compare(fixture("v0.10.0"), schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("protocol_version changed from 1 to 2", result.stdout)

    def test_a_declared_list_with_the_version_unchanged_waives_nothing(self):
        """After the release is tagged the list is left over: noted, and a break still fails."""
        listed = '[[break]]\nchange = "route GET /v1/procs is gone"\nwhy = "renamed"\n'
        clean = self.compare(fixture("v0.10.0"), fixture("v0.10.0"), listed)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertIn("waive nothing", clean.stdout)
        schema = fixture("v0.10.0")
        schema["routes"].remove(route(schema, "GET", "/v1/procs"))
        broken = self.compare(fixture("v0.10.0"), schema, listed)
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)

    def test_a_declared_list_that_cant_be_read_is_refused(self):
        for text, needle in (
            ("[[break]\n", "isn't TOML"),
            ('[[break]]\nwhy = "x"\n', "has no `change`"),
            ('[[break]]\nchange = "route GET /v1/procs is gone"\n', "has no `why`"),
            ('break = "x"\n', "array of tables"),
        ):
            with self.subTest(text=text):
                result = self.compare(fixture("v0.10.0"), fixture("v0.10.0"), text)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(needle, result.stderr)

    def test_the_committed_declared_list_parses(self):
        """An unreadable docs/schema-breaks.toml would refuse every run; this names it first."""
        result = run_check(
            "--base-file",
            str(FIXTURES / "v0.10.0.json"),
            "--schema",
            str(FIXTURES / "v0.10.0.json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ── the floor: an input the check can't read refuses, never passes ───────

    def test_an_empty_schema_is_refused(self):
        self.assert_refused(fixture("v0.10.0"), "", "is empty")
        self.assert_refused("", fixture("v0.10.0"), "is empty")

    def test_the_committed_empty_fixture_is_refused(self):
        result = run_check(
            "--base-file", str(FIXTURES / "empty.json"), "--schema", "docs/schema.json"
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("is empty", result.stderr)

    def test_an_unparsable_schema_is_refused(self):
        self.assert_refused(fixture("v0.10.0"), '{"routes": [', "isn't JSON")

    def test_a_schema_that_isnt_an_object_is_refused(self):
        self.assert_refused(fixture("v0.10.0"), "[]", "isn't a JSON object")

    def test_a_schema_with_no_routes_is_refused(self):
        schema = fixture("v0.10.0")
        schema["routes"] = []
        self.assert_refused(fixture("v0.10.0"), schema, "no routes")
        self.assert_refused(schema, fixture("v0.10.0"), "no routes")

    def test_a_route_set_that_parses_to_nothing_is_refused(self):
        """Every route present, none with the keys the reader looks for."""
        schema = fixture("v0.10.0")
        for r in schema["routes"]:
            r["verb"] = r.pop("method")
        self.assert_refused(fixture("v0.10.0"), schema, "no routes")

    def test_routes_that_reach_no_definition_are_refused(self):
        """A reader that stopped following `$ref` would compare no fields and pass."""
        schema = fixture("v0.10.0")
        for r in schema["routes"]:
            for key in ("request", "response", "query", "sse_events"):
                r.pop(key, None)
        self.assert_refused(fixture("v0.10.0"), schema, "reach no request definition")

    def test_a_schema_with_no_protocol_version_is_refused(self):
        schema = copy.deepcopy(fixture("v0.10.0"))
        del schema["protocol_version"]
        self.assert_refused(fixture("v0.10.0"), schema, "protocol_version")


class BaseSelectionTests(unittest.TestCase):
    """The base is the highest `v*` release tag reachable from HEAD, read with `git show`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit(self, schema: dict, message: str, *tags: str) -> None:
        path = self.repo / "docs" / "schema.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        git(self.repo, "add", "docs/schema.json")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        for tag in tags:
            git(self.repo, "tag", "-a", tag, "-m", tag)

    def releases(self) -> None:
        """v0.9.0, then v0.10.0-rc.1 and v0.10.0 on one commit, then an untagged commit.

        A string sort puts v0.9.0 last, and a sort that ignores prereleases can't order the
        rc against its release. A v0.11.0 tag on a branch HEAD doesn't contain has a schema
        that fails the current one, so choosing it would show.
        """
        self.commit(fixture("v0.9.0"), "0.9.0", "v0.9.0")
        self.commit(fixture("v0.10.0"), "0.10.0", "v0.10.0-rc.1", "v0.10.0")
        git(self.repo, "checkout", "-q", "-b", "side")
        future = fixture("v0.10.0")
        future["$defs"]["StartRequest"]["properties"]["nice"] = {"type": "integer"}
        self.commit(future, "0.11.0", "v0.11.0")
        git(self.repo, "checkout", "-q", "main")
        self.commit(fixture("v0.10.0"), "after the release")
        git(self.repo, "tag", "not-a-release")

    def check(self, *args: str, repo: Path | None = None):
        return run_check("--root", str(repo or self.repo), *args, cwd=self.repo.parent)

    def test_the_highest_reachable_release_tag_is_the_base(self):
        self.releases()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("against v0.10.0:docs/schema.json", result.stdout)

    def test_a_break_against_the_chosen_tag_fails(self):
        self.releases()
        schema = fixture("v0.10.0")
        schema["$defs"]["StartRequest"]["required"].append("cwd")
        (self.repo / "docs" / "schema.json").write_text(json.dumps(schema), "utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("against v0.10.0:docs/schema.json", result.stdout)
        self.assertIn("request StartRequest.cwd is required now", result.stdout)

    def test_the_base_ref_flag_overrides_the_tag(self):
        self.releases()
        result = self.check("--base-ref", "v0.9.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("against v0.9.0:docs/schema.json", result.stdout)

    def test_a_base_ref_with_no_schema_is_refused(self):
        self.releases()
        (self.repo / "README").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "rm", "-q", "--cached", "docs/schema.json")
        git(self.repo, "commit", "-q", "-m", "no schema")
        git(self.repo, "tag", "-a", "v0.12.0", "-m", "v0.12.0")
        result = self.check("--base-ref", "v0.12.0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("v0.12.0 has no docs/schema.json", result.stderr)

    def test_a_clone_with_no_release_tag_is_refused(self):
        """Passing there would report a compatibility nothing was compared for."""
        self.commit(fixture("v0.10.0"), "untagged")
        git(self.repo, "tag", "not-a-release")
        result = self.check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no v* release tag is reachable from HEAD", result.stderr)
        self.assertIn("--base-ref", result.stderr)

    def test_no_git_on_path_is_refused_not_a_traceback(self):
        self.releases()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.repo)],
            cwd=self.repo.parent,
            capture_output=True,
            text=True,
            env={**clean_env(), "PATH": str(self.repo.parent)},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("git isn't on PATH", result.stderr)

    def test_a_shallow_clone_is_refused_naming_the_fetch(self):
        """CI's default checkout: one commit, no tags reachable, and a hint that says why."""
        self.releases()
        shallow = self.repo.parent / "shallow"
        git(
            self.repo.parent,
            "clone",
            "-q",
            "--depth",
            "1",
            self.repo.as_uri(),
            str(shallow),
        )
        result = self.check(repo=shallow)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no v* release tag is reachable from HEAD", result.stderr)
        self.assertIn("shallow", result.stderr)


if __name__ == "__main__":
    unittest.main()
