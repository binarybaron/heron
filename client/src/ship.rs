//! Bringing the server's copy of one file up to date: the offset/409/truncate dance
//! from the upload protocol. `Server` is a trait so the logic is tested without a network.

use std::io::{Read, Seek, SeekFrom};

use anyhow::{bail, Context, Result};
use base64::Engine;

/// Bodies may be 8 MiB; 4 MiB of raw bytes is ~5.6 MiB once base64-encoded.
pub const CHUNK: usize = 4 << 20;

/// How many 409s in a row we tolerate before deciding the server and we disagree
/// in a way that resending will not fix.
const MAX_CONFLICTS: usize = 3;

pub struct Upload<'a> {
    pub host: &'a str,
    pub kind: &'a str,
    pub path: &'a str,
    pub offset: u64,
    pub data: &'a [u8],
    pub truncate: bool,
}

pub enum Reply {
    /// 200: the server's file is now `size` bytes.
    Stored { size: u64 },
    /// 409: the server's file is `size` bytes; our offset was wrong.
    Conflict { size: u64 },
}

pub trait Server {
    fn upload(&self, u: &Upload) -> Result<Reply>;
}

#[derive(Debug)]
pub struct Synced {
    /// Offset the server acknowledged; store it as shipped.
    pub offset: u64,
    pub sent: u64,
}

/// Ship `file` (which is `local_size` bytes long) starting from `shipped`, the
/// offset we believe the server already has.
pub fn sync<R: Read + Seek>(
    server: &dyn Server,
    host: &str,
    kind: &str,
    path: &str,
    file: &mut R,
    local_size: u64,
    shipped: u64,
) -> Result<Synced> {
    sync_chunked(server, host, kind, path, file, local_size, shipped, CHUNK)
}

#[allow(clippy::too_many_arguments)]
fn sync_chunked<R: Read + Seek>(
    server: &dyn Server,
    host: &str,
    kind: &str,
    path: &str,
    file: &mut R,
    local_size: u64,
    shipped: u64,
    chunk: usize,
) -> Result<Synced> {
    let mut offset = shipped;
    // A file shorter than what we shipped was rotated or rewritten: start over.
    let mut truncate = local_size < shipped;
    if truncate {
        offset = 0;
    }
    let mut sent = 0;
    let mut conflicts = 0;
    let mut buf = vec![0u8; chunk];
    // `|| truncate` so an emptied file still tells the server to drop its copy.
    while offset < local_size || truncate {
        let n = ((local_size - offset) as usize).min(chunk);
        file.seek(SeekFrom::Start(offset))?;
        file.read_exact(&mut buf[..n]).with_context(|| format!("reading {kind}:{path} at {offset}"))?;
        let up = Upload { host, kind, path, offset, data: &buf[..n], truncate };
        match server.upload(&up)? {
            Reply::Stored { size } => {
                sent += n as u64;
                offset = size;
                truncate = false;
            }
            Reply::Conflict { size } => {
                conflicts += 1;
                if conflicts > MAX_CONFLICTS {
                    bail!("{kind}:{path}: server keeps answering 409 (its size {size}, ours {local_size})");
                }
                if size <= local_size && !truncate {
                    // The server is behind (or ahead of our record but within the file): resume from there.
                    offset = size;
                } else {
                    // The server has more than we do, or refused a truncate: replace its copy.
                    offset = 0;
                    truncate = true;
                }
            }
        }
    }
    Ok(Synced { offset, sent })
}

/// The real thing: POSTs to `<server>/api/v1/upload`.
pub struct Http {
    agent: ureq::Agent,
    url: String,
    auth: String,
}

impl Http {
    pub fn new(server: &str, token: &str) -> Http {
        // Non-2xx must come back as a response, not an error, so we can read the 409 body.
        let config = ureq::config::Config::builder()
            .http_status_as_error(false)
            .timeout_global(Some(std::time::Duration::from_secs(120)))
            .build();
        Http {
            agent: config.new_agent(),
            url: format!("{}/api/v1/upload", server.trim_end_matches('/')),
            auth: format!("Bearer {token}"),
        }
    }
}

impl Server for Http {
    fn upload(&self, u: &Upload) -> Result<Reply> {
        let body = serde_json::json!({
            "host": u.host,
            "kind": u.kind,
            "path": u.path,
            "offset": u.offset,
            "data": base64::engine::general_purpose::STANDARD.encode(u.data),
            "truncate": u.truncate,
        });
        let body = serde_json::to_vec(&body)?;
        let mut resp = self
            .agent
            .post(&self.url)
            .header("Authorization", &self.auth)
            .content_type("application/json")
            .send(&body[..])
            .with_context(|| format!("POST {}", self.url))?;
        let status = resp.status().as_u16();
        let text = resp.body_mut().read_to_string().unwrap_or_default();
        let size = || -> Result<u64> {
            let v: serde_json::Value = serde_json::from_str(&text).context("upload reply is not JSON")?;
            v["size"].as_u64().context("upload reply has no size")
        };
        match status {
            200 => Ok(Reply::Stored { size: size()? }),
            409 => Ok(Reply::Conflict { size: size()? }),
            _ => bail!("HTTP {status} from {}: {}", self.url, text.trim()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::io::Cursor;

    /// In-memory server that behaves like the protocol says, remembering every request.
    struct Fake {
        stored: RefCell<Vec<u8>>,
        calls: RefCell<Vec<(u64, usize, bool)>>,
        /// Answer 409 with this size regardless of the request (a server that will not agree).
        stuck: Option<u64>,
    }

    impl Fake {
        fn with(stored: &[u8]) -> Fake {
            Fake { stored: RefCell::new(stored.to_vec()), calls: RefCell::new(vec![]), stuck: None }
        }
    }

    impl Server for Fake {
        fn upload(&self, u: &Upload) -> Result<Reply> {
            self.calls.borrow_mut().push((u.offset, u.data.len(), u.truncate));
            if let Some(size) = self.stuck {
                return Ok(Reply::Conflict { size });
            }
            let mut stored = self.stored.borrow_mut();
            if u.truncate {
                stored.clear();
                stored.extend_from_slice(u.data);
            } else if u.offset != stored.len() as u64 {
                return Ok(Reply::Conflict { size: stored.len() as u64 });
            } else {
                stored.extend_from_slice(u.data);
            }
            Ok(Reply::Stored { size: stored.len() as u64 })
        }
    }

    fn run(server: &Fake, local: &[u8], shipped: u64, chunk: usize) -> Synced {
        let mut file = Cursor::new(local.to_vec());
        sync_chunked(server, "h", "terminal", "a.log", &mut file, local.len() as u64, shipped, chunk).unwrap()
    }

    #[test]
    fn ships_a_new_file_from_zero() {
        let s = Fake::with(b"");
        let r = run(&s, b"hello", 0, CHUNK);
        assert_eq!((r.offset, r.sent), (5, 5));
        assert_eq!(*s.stored.borrow(), b"hello");
        assert_eq!(*s.calls.borrow(), vec![(0, 5, false)]);
    }

    #[test]
    fn ships_only_the_appended_bytes() {
        let s = Fake::with(b"hello");
        let r = run(&s, b"hello world", 5, CHUNK);
        assert_eq!((r.offset, r.sent), (11, 6));
        assert_eq!(*s.calls.borrow(), vec![(5, 6, false)]);
    }

    #[test]
    fn nothing_to_do_when_in_sync() {
        let s = Fake::with(b"hello");
        let r = run(&s, b"hello", 5, CHUNK);
        assert_eq!((r.offset, r.sent), (5, 0));
        assert!(s.calls.borrow().is_empty());
    }

    #[test]
    fn resumes_from_the_size_the_server_reports() {
        // We think 5 bytes are shipped; the server only has 2.
        let s = Fake::with(b"he");
        let r = run(&s, b"hello world", 5, CHUNK);
        assert_eq!(r.offset, 11);
        assert_eq!(*s.stored.borrow(), b"hello world");
        assert_eq!(*s.calls.borrow(), vec![(5, 6, false), (2, 9, false)]);
    }

    #[test]
    fn replaces_when_the_server_has_more_than_we_do() {
        // The server holds an older, longer version of a rotated file.
        let s = Fake::with(b"old and very long contents");
        let r = run(&s, b"new", 0, CHUNK);
        assert_eq!(r.offset, 3);
        assert_eq!(*s.stored.borrow(), b"new");
        assert_eq!(*s.calls.borrow(), vec![(0, 3, false), (0, 3, true)]);
    }

    #[test]
    fn truncates_when_the_local_file_shrank() {
        let s = Fake::with(b"hello world");
        let r = run(&s, b"hi", 11, CHUNK);
        assert_eq!(r.offset, 2);
        assert_eq!(*s.stored.borrow(), b"hi");
        assert_eq!(*s.calls.borrow(), vec![(0, 2, true)]);
    }

    #[test]
    fn an_emptied_file_still_clears_the_server_copy() {
        let s = Fake::with(b"hello");
        let r = run(&s, b"", 5, CHUNK);
        assert_eq!(r.offset, 0);
        assert!(s.stored.borrow().is_empty());
        assert_eq!(*s.calls.borrow(), vec![(0, 0, true)]);
    }

    #[test]
    fn splits_large_uploads_into_chunks() {
        let s = Fake::with(b"");
        let data: Vec<u8> = (0..10u8).collect();
        let r = run(&s, &data, 0, 4);
        assert_eq!(r.offset, 10);
        assert_eq!(*s.stored.borrow(), data);
        assert_eq!(*s.calls.borrow(), vec![(0, 4, false), (4, 4, false), (8, 2, false)]);
    }

    #[test]
    fn truncate_flag_only_on_the_first_chunk() {
        let s = Fake::with(b"much longer than before");
        let r = run(&s, b"abcdefg", 100, 4);
        assert_eq!(r.offset, 7);
        assert_eq!(*s.stored.borrow(), b"abcdefg");
        assert_eq!(*s.calls.borrow(), vec![(0, 4, true), (4, 3, false)]);
    }

    #[test]
    fn gives_up_on_a_server_that_keeps_conflicting() {
        let mut s = Fake::with(b"");
        s.stuck = Some(1);
        let mut file = Cursor::new(b"hello".to_vec());
        let err = sync_chunked(&s, "h", "terminal", "a.log", &mut file, 5, 0, CHUNK).unwrap_err();
        assert!(err.to_string().contains("409"), "{err}");
        assert_eq!(s.calls.borrow().len(), MAX_CONFLICTS + 1);
    }
}
