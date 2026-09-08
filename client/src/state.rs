//! shipped.json: what the server already has, per `<kind>:<path>`.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    /// Bytes the server has acknowledged.
    pub offset: u64,
    /// Local size and mtime (unix seconds) when we last looked, for `status` and debugging.
    pub size: u64,
    pub mtime: i64,
}

pub struct State {
    pub files: BTreeMap<String, Entry>,
    path: PathBuf,
}

impl State {
    pub fn load(path: PathBuf) -> Result<State> {
        let files = match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes).with_context(|| format!("parsing {}", path.display()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(e) => return Err(e).with_context(|| format!("reading {}", path.display())),
        };
        Ok(State { files, path })
    }

    /// Write-then-rename so a crash mid-write never leaves a half file that would
    /// make the next run re-ship everything.
    pub fn save(&self) -> Result<()> {
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir)?;
        }
        let tmp = self.path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_vec_pretty(&self.files)?)?;
        fs::rename(&tmp, &self.path).with_context(|| format!("writing {}", self.path.display()))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn bytes_shipped(&self) -> u64 {
        self.files.values().map(|e| e.offset).sum()
    }
}
