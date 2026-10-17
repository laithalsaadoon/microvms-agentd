// SPDX-License-Identifier: Apache-2.0
//! Concurrent cache installation preserves complete verified artifacts.

use microvms_edges::provision::*;
use std::path::{Path, PathBuf};

fn elf_header(machine: u16) -> Vec<u8> {
    let mut header = vec![0u8; 20];
    header[..4].copy_from_slice(b"\x7fELF");
    header[5] = 1;
    header[18..20].copy_from_slice(&machine.to_le_bytes());
    header
}

fn request(dir: &Path) -> Request<'_> {
    Request {
        version: Some("9.9.9"),
        state_dir: Some(dir),
        binary: None,
    }
}

fn no_env(_: &str) -> Option<String> {
    None
}

struct Failing;
impl Fetch for Failing {
    fn fetch(&self, _: &str, _: &Path, _: &mut dyn FnMut(&str)) -> Result<Verification, String> {
        Err("no network in tests".into())
    }
}

struct Panicking;
impl Fetch for Panicking {
    fn fetch(&self, _: &str, _: &Path, _: &mut dyn FnMut(&str)) -> Result<Verification, String> {
        panic!("a valid cache hit must not fetch")
    }
}

/// Concurrent cold-cache requests must retain their own complete verified bytes
/// (BIND-18, BIND-19), even when both downloads finish before either install.
#[test]
fn concurrent_cache_misses_own_their_temporary_files() {
    use std::sync::{Barrier, Mutex};
    struct Concurrent {
        bytes: Vec<u8>,
        written: Barrier,
        paths: Mutex<Vec<PathBuf>>,
    }
    impl Fetch for Concurrent {
        fn fetch(
            &self,
            _: &str,
            dest: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            self.paths.lock().unwrap().push(dest.to_path_buf());
            // Fetch implementations may atomically replace the supplied path.
            let downloaded = tempfile::NamedTempFile::new_in(dest.parent().unwrap()).unwrap();
            std::fs::write(downloaded.path(), &self.bytes).unwrap();
            downloaded.persist(dest).unwrap();
            self.written.wait();
            Ok(Verification::Attestation)
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let mut bytes = elf_header(REQUIRED_ELF_MACHINE);
    bytes.resize(1024 * 1024, 0x7b);
    let fetch = Concurrent {
        bytes: bytes.clone(),
        written: Barrier::new(2),
        paths: Mutex::new(Vec::new()),
    };
    let results = std::thread::scope(|scope| {
        let run = || resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {});
        let first = scope.spawn(run);
        let second = scope.spawn(run);
        [first.join().unwrap(), second.join().unwrap()]
    });
    let paths = fetch.paths.into_inner().unwrap();
    assert_ne!(
        paths[0], paths[1],
        "downloads must not share a partial file"
    );
    for result in results {
        let result = result.expect("both installs succeed");
        assert_eq!(result.bytes, bytes);
        assert_eq!(result.sha256, sha256_hex(&bytes));
    }
    let cached = resolve(&request(dir.path()), &no_env, &Panicking, &mut |_| {})
        .expect("a complete verified cache entry remains");
    assert_eq!(cached.bytes, bytes);
    assert_eq!(cached.source, Source::Cache(Verification::Attestation));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        assert_eq!(
            std::fs::metadata(&cached.path)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
    }
    let entries: Vec<_> = std::fs::read_dir(cached.path.parent().unwrap())
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(entries.len(), 2, "no temporary files survive");
}

/// A cache reader overlapping the binary-before-record install window must not
/// unlink the other caller's verified bytes (BIND-19).
#[test]
fn a_cache_miss_preserves_an_install_waiting_for_its_record() {
    let dir = tempfile::tempdir().unwrap();
    let cached = cache_path(dir.path(), "9.9.9");
    std::fs::create_dir_all(cached.parent().unwrap()).unwrap();
    let bytes = elf_header(REQUIRED_ELF_MACHINE);
    // Pause an install immediately after persisting its binary. The other
    // caller sees no record, and its own fetch fails before it can replace it.
    std::fs::write(&cached, &bytes).unwrap();
    resolve(&request(dir.path()), &no_env, &Failing, &mut |_| {})
        .expect_err("the overlapping reader cannot fetch");
    assert_eq!(std::fs::read(&cached).unwrap(), bytes);
    // Complete the first install; it must still have a valid cached path.
    std::fs::write(
        cached.with_file_name("agentd.verified.json"),
        serde_json::json!({"version": "9.9.9", "sha256": sha256_hex(&bytes), "verification": "attestation"}).to_string(),
    )
    .unwrap();
    let hit = resolve(&request(dir.path()), &no_env, &Panicking, &mut |_| {})
        .expect("the completed install stays usable");
    assert_eq!(hit.bytes, bytes);
    assert_eq!(hit.source, Source::Cache(Verification::Attestation));
}
