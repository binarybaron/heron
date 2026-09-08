//! Finding the files to tail: Claude Code and Codex transcripts, terminal recordings.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::config::Paths;

pub struct Source {
    /// `kind` as the upload protocol names it.
    pub kind: &'static str,
    /// Path relative to the source root, always '/'-separated; this is the server's key.
    pub path: String,
    pub file: PathBuf,
    pub size: u64,
    pub mtime: SystemTime,
}

impl Source {
    /// Key in shipped.json.
    pub fn key(&self) -> String {
        format!("{}:{}", self.kind, self.path)
    }
}

pub fn scan(paths: &Paths) -> Vec<Source> {
    let mut out = Vec::new();
    walk("claude", &paths.claude, &paths.claude, "jsonl", true, &mut out);
    walk("codex", &paths.codex, &paths.codex, "jsonl", true, &mut out);
    walk("terminal", &paths.terminals, &paths.terminals, "log", false, &mut out);
    // Oldest first, so a long first sync ships history in the order it happened.
    out.sort_by_key(|s| s.mtime);
    out
}

fn walk(kind: &'static str, root: &Path, dir: &Path, ext: &str, recursive: bool, out: &mut Vec<Source>) {
    // A source that does not exist on this machine is simply empty.
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let file = entry.path();
        // Follow symlinks: Claude project dirs are sometimes linked elsewhere.
        let Ok(meta) = fs::metadata(&file) else { continue };
        if meta.is_dir() {
            if recursive {
                walk(kind, root, &file, ext, true, out);
            }
            continue;
        }
        if file.extension().and_then(|e| e.to_str()) != Some(ext) {
            continue;
        }
        let Ok(rel) = file.strip_prefix(root) else { continue };
        let path = rel
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join("/");
        out.push(Source {
            kind,
            path,
            size: meta.len(),
            mtime: meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
            file,
        });
    }
}
