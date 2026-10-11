// SPDX-License-Identifier: Apache-2.0
//! The host input helpers preserve values and require private regular files.

use std::io::{self, Write};

use microvms_edges::secret_input::{read_secret, read_secret_file};

#[test]
fn secret_streams_preserve_values_and_report_invalid_input() {
    assert_eq!(
        read_secret(&mut &b"example-token\r\n"[..]).unwrap(),
        "example-token"
    );
    assert_eq!(read_secret(&mut &b" a b "[..]).unwrap(), " a b ");
    for mut input in [&b""[..], &b"\r\n"[..], &b"\xff"[..]] {
        assert!(read_secret(&mut input).is_err());
    }
    struct FailedRead;
    impl io::Read for FailedRead {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("input unavailable"))
        }
    }
    assert!(
        read_secret(&mut FailedRead)
            .unwrap_err()
            .to_string()
            .contains("input unavailable")
    );
}

#[test]
fn private_secret_files_preserve_values_and_refuse_other_sources() {
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(b"example-token\n").unwrap();
    assert_eq!(read_secret_file(file.path()).unwrap(), "example-token");
    let directory = tempfile::tempdir().unwrap();
    assert!(read_secret_file(directory.path()).is_err());
    assert!(read_secret_file(&directory.path().join("missing")).is_err());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o640))
            .unwrap();
        assert!(
            read_secret_file(file.path())
                .unwrap_err()
                .to_string()
                .contains("must grant no permissions")
        );
    }
}

#[cfg(unix)]
#[test]
fn secret_files_refuse_named_pipes_without_waiting_for_a_writer() {
    use std::sync::mpsc;
    use std::time::Duration;

    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("input");
    assert!(
        std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .unwrap()
            .success()
    );
    let (sender, receiver) = mpsc::channel();
    let reader_path = path.clone();
    let reader = std::thread::spawn(move || {
        sender.send(read_secret_file(&reader_path)).unwrap();
    });
    let result = receiver.recv_timeout(Duration::from_secs(5));
    if result.is_err() {
        // Release a blocking reader so a failed assertion does not leave a thread behind.
        drop(std::fs::OpenOptions::new().write(true).open(&path).unwrap());
    }
    reader.join().unwrap();
    assert!(
        result
            .expect("special-file validation must not wait for a writer")
            .unwrap_err()
            .to_string()
            .contains("regular file")
    );
}
