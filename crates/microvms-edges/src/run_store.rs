// SPDX-License-Identifier: Apache-2.0
//! File storage compatible with the CLI's existing run and history documents.

use std::io::{self, Write};
use std::path::{Path, PathBuf};

use microvms_app::run_store::{RunStore, StoredLedger};

/// The shared state directory: run JSON at its root and history JSON Lines below it.
#[derive(Clone, Debug)]
pub struct FileRunStore {
    root: PathBuf,
}

impl FileRunStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    fn ledger_path(&self, key: &str) -> io::Result<PathBuf> {
        Self::check_component(key)?;
        Ok(self.root.join(format!("{key}.json")))
    }

    fn history_path(&self, vm_id: &str) -> io::Result<PathBuf> {
        Self::check_component(vm_id)?;
        Ok(self.root.join("history").join(format!("{vm_id}.jsonl")))
    }

    fn check_component(value: &str) -> io::Result<()> {
        // Check both platforms' separators and drive prefixes regardless of the host.
        // The public store must not let a ledger key address the names or history namespace.
        if value.is_empty() || matches!(value, "." | "..") || value.contains(['/', '\\', ':']) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "record key must be one file name component",
            ));
        }
        Ok(())
    }

    fn prepare(path: &Path) -> io::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        Ok(())
    }
}

impl RunStore for FileRunStore {
    fn ledgers(&self) -> Vec<StoredLedger> {
        let Ok(entries) = std::fs::read_dir(&self.root) else {
            return Vec::new();
        };
        entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
            .map(|path| StoredLedger {
                key: path
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default(),
                location: path.to_string_lossy().into_owned(),
                text: std::fs::read_to_string(&path).ok(),
            })
            .collect()
    }

    fn write_ledger(&self, key: &str, text: &str) -> io::Result<()> {
        let path = self.ledger_path(key)?;
        Self::prepare(&path)?;
        std::fs::write(path, text)
    }

    fn remove_ledger(&self, key: &str) -> io::Result<()> {
        std::fs::remove_file(self.ledger_path(key)?)
    }

    fn read_history(&self, vm_id: &str) -> io::Result<String> {
        std::fs::read_to_string(self.history_path(vm_id)?)
    }

    fn append_history(&self, vm_id: &str, line: &str) -> io::Result<()> {
        let path = self.history_path(vm_id)?;
        Self::prepare(&path)?;
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(path)?;
        writeln!(file, "{line}")
    }
}
