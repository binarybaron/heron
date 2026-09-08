//! One scan over the sources, and the forever loop around it.

use std::fs::{File, TryLockError};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};

use crate::config::{Config, Paths};
use crate::ship::{self, Server};
use crate::sources;
use crate::state::{Entry, State};

#[derive(Default)]
pub struct Outcome {
    pub files: usize,
    pub bytes: u64,
    pub errors: usize,
}

/// With the server down every file fails the same way; stop the round early
/// rather than log once per file every poll.
const MAX_CONSECUTIVE_ERRORS: usize = 3;

pub fn scan_once(cfg: &Config, paths: &Paths, server: &dyn Server) -> Result<Outcome> {
    let mut state = State::load(paths.state_file())?;
    let host = cfg.host();
    let cutoff = SystemTime::now() - Duration::from_secs(cfg.max_age_days * 86_400);
    let mut out = Outcome::default();
    let mut consecutive_errors = 0;
    for src in sources::scan(paths) {
        let key = src.key();
        let shipped = match state.files.get(&key) {
            Some(e) => e.offset,
            // Files we have never shipped and that stopped changing long ago are old
            // history, not work in progress; they are left alone for good.
            None if src.mtime < cutoff => continue,
            None => 0,
        };
        if src.size == shipped {
            continue;
        }
        let result = File::open(&src.file)
            .with_context(|| format!("opening {}", src.file.display()))
            .and_then(|mut f| ship::sync(server, &host, src.kind, &src.path, &mut f, src.size, shipped));
        match result {
            Ok(synced) => {
                consecutive_errors = 0;
                out.files += 1;
                out.bytes += synced.sent;
                let mtime = src.mtime.duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
                state.files.insert(key.clone(), Entry { offset: synced.offset, size: src.size, mtime });
                // Persist per file so a crash mid-scan never re-ships what the server has.
                state.save()?;
                log!("{key}: shipped {} bytes (now {})", synced.sent, synced.offset);
            }
            Err(e) => {
                out.errors += 1;
                consecutive_errors += 1;
                log!("{key}: {e:#}");
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS {
                    log!("giving up this round after {consecutive_errors} errors in a row");
                    break;
                }
            }
        }
    }
    Ok(out)
}

/// One scan from the command line, refusing to race a running agent.
///
/// `install` starts the background service; a `once` typed right after it
/// would otherwise write the same state file at the same moment, and one
/// of the two renames would fail with a missing temp file.
pub fn once(cfg: &Config, paths: &Paths) -> Result<Outcome> {
    let _lock = match lock(paths)? {
        Some(lock) => lock,
        None => anyhow::bail!("another heron-agent is running here (the background service?); it ships on its own"),
    };
    let server = ship::Http::new(&cfg.server, &cfg.token);
    scan_once(cfg, paths, &server)
}

pub fn run(cfg: &Config, paths: &Paths) -> Result<()> {
    // Two agents on one machine would race over shipped.json; the second one leaves.
    let _lock = match lock(paths)? {
        Some(lock) => lock,
        None => {
            log!("another heron-agent is already running here; exiting");
            return Ok(());
        }
    };
    let server = ship::Http::new(&cfg.server, &cfg.token);
    log!("heron-agent {} shipping to {} as {}", env!("CARGO_PKG_VERSION"), cfg.server, cfg.host());
    loop {
        match scan_once(cfg, paths, &server) {
            Ok(_) => {}
            // Anything that escapes a scan (state file trouble, mostly) is retried next poll.
            Err(e) => log!("scan failed: {e:#}"),
        }
        std::thread::sleep(Duration::from_secs(cfg.poll_seconds.max(1)));
    }
}

fn lock(paths: &Paths) -> Result<Option<File>> {
    std::fs::create_dir_all(&paths.state_dir)?;
    let file = File::create(paths.state_dir.join("agent.lock"))?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(TryLockError::WouldBlock) => Ok(None),
        Err(TryLockError::Error(e)) => Err(e.into()),
    }
}
