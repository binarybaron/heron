//! End to end: `heron-agent once` against an in-process HTTP server that speaks
//! the upload protocol. Only the standard library on the server side.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;
use serde_json::{json, Value};

const TOKEN: &str = "s3cret";

#[derive(Default)]
struct Store {
    /// `<kind>/<path>` -> bytes, as the server would keep them under raw/<host>/.
    files: BTreeMap<String, Vec<u8>>,
    /// Every upload body seen, in order, for asserting what the client sent.
    requests: Vec<Value>,
}

fn start_server() -> (String, Arc<Mutex<Store>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", listener.local_addr().unwrap());
    let store = Arc::new(Mutex::new(Store::default()));
    let s = store.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            handle(stream, &s);
        }
    });
    (addr, store)
}

fn handle(mut stream: TcpStream, store: &Mutex<Store>) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let mut parts = line.split_whitespace();
    let (method, path) = (parts.next().unwrap_or("").to_string(), parts.next().unwrap_or("").to_string());
    let mut headers = HashMap::new();
    loop {
        let mut h = String::new();
        reader.read_line(&mut h).unwrap();
        let h = h.trim_end();
        if h.is_empty() {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let len: usize = headers.get("content-length").and_then(|v| v.parse().ok()).unwrap_or(0);
    let mut body = vec![0u8; len];
    reader.read_exact(&mut body).unwrap();

    let (status, reply) = respond(&method, &path, &headers, &body, store);
    let reply = reply.to_string();
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
        reply.len()
    )
    .unwrap();
}

fn respond(method: &str, path: &str, headers: &HashMap<String, String>, body: &[u8], store: &Mutex<Store>) -> (&'static str, Value) {
    if headers.get("authorization").map(String::as_str) != Some(&format!("Bearer {TOKEN}")) {
        return ("401 Unauthorized", json!({"error": "unauthorized"}));
    }
    if method != "POST" || path != "/api/v1/upload" {
        return ("404 Not Found", json!({"error": "not found"}));
    }
    let req: Value = serde_json::from_slice(body).unwrap();
    let mut store = store.lock().unwrap();
    store.requests.push(req.clone());
    let key = format!("{}/{}", req["kind"].as_str().unwrap(), req["path"].as_str().unwrap());
    let offset = req["offset"].as_u64().unwrap();
    let data = base64::engine::general_purpose::STANDARD.decode(req["data"].as_str().unwrap()).unwrap();
    let file = store.files.entry(key).or_default();
    if req["truncate"].as_bool().unwrap() {
        file.clear();
        file.extend_from_slice(&data);
    } else if offset != file.len() as u64 {
        return ("409 Conflict", json!({"size": file.len()}));
    } else {
        file.extend_from_slice(&data);
    }
    ("200 OK", json!({"size": file.len()}))
}

struct Sandbox {
    root: PathBuf,
    server: String,
    store: Arc<Mutex<Store>>,
}

impl Sandbox {
    fn new(name: &str) -> Sandbox {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("heron-test-{name}-{}-{nanos}", std::process::id()));
        for d in ["claude", "codex", "terminals", "state"] {
            fs::create_dir_all(root.join(d)).unwrap();
        }
        let (server, store) = start_server();
        fs::write(root.join("agent.toml"), format!("server = \"{server}/\"\ntoken = \"{TOKEN}\"\nhost = \"box\"\n")).unwrap();
        Sandbox { root, server, store }
    }

    fn dir(&self, name: &str) -> PathBuf {
        self.root.join(name)
    }

    fn once(&self, extra: &[&str]) -> String {
        let out = Command::new(env!("CARGO_BIN_EXE_heron-agent"))
            .arg("once")
            .args(extra)
            .env("HERON_AGENT_CONFIG", self.root.join("agent.toml"))
            .env("HERON_CLAUDE_DIR", self.dir("claude"))
            .env("HERON_CODEX_DIR", self.dir("codex"))
            .env("HERON_TERMINALS_DIR", self.dir("terminals"))
            .env("HERON_STATE_DIR", self.dir("state"))
            .output()
            .unwrap();
        assert!(out.status.success(), "once failed:\n{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    fn stored(&self, key: &str) -> Vec<u8> {
        self.store.lock().unwrap().files.get(key).cloned().unwrap_or_default()
    }

    fn take_requests(&self) -> Vec<Value> {
        std::mem::take(&mut self.store.lock().unwrap().requests)
    }

    fn state(&self) -> Value {
        serde_json::from_slice(&fs::read(self.dir("state").join("shipped.json")).unwrap()).unwrap()
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn append(path: &Path, bytes: &[u8]) {
    use std::io::Write;
    fs::OpenOptions::new().append(true).open(path).unwrap().write_all(bytes).unwrap();
}

#[test]
fn ships_new_bytes_then_appends_then_resyncs() {
    let sb = Sandbox::new("flow");
    let log = sb.dir("terminals").join("20260908T010203Z-42.log");
    fs::write(&log, b"hello\n").unwrap();
    fs::create_dir_all(sb.dir("claude").join("-Users-me")).unwrap();
    let jsonl = sb.dir("claude").join("-Users-me").join("abc.jsonl");
    fs::write(&jsonl, b"{\"a\":1}\n").unwrap();
    // Not a transcript: must be ignored.
    fs::write(sb.dir("claude").join("-Users-me").join("notes.txt"), b"x").unwrap();

    // 1. First run ships whole files, paths relative to the source roots.
    let out = sb.once(&[]);
    assert!(out.contains("shipped 14 bytes from 2 files"), "{out}");
    assert_eq!(sb.stored("terminal/20260908T010203Z-42.log"), b"hello\n");
    assert_eq!(sb.stored("claude/-Users-me/abc.jsonl"), b"{\"a\":1}\n");
    let reqs = sb.take_requests();
    assert_eq!(reqs.len(), 2);
    assert!(reqs.iter().all(|r| r["host"] == "box" && r["offset"] == 0 && r["truncate"] == false));
    assert_eq!(sb.state()["terminal:20260908T010203Z-42.log"]["offset"], 6);

    // 2. Nothing changed: nothing is sent.
    let out = sb.once(&[]);
    assert!(out.contains("shipped 0 bytes from 0 files"), "{out}");
    assert!(sb.take_requests().is_empty());

    // 3. Only the appended bytes go out, at the right offset.
    append(&log, b"world\n");
    let out = sb.once(&[]);
    assert!(out.contains("shipped 6 bytes from 1 files"), "{out}");
    let reqs = sb.take_requests();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0]["offset"], 6);
    assert_eq!(reqs[0]["path"], "20260908T010203Z-42.log");
    assert_eq!(reqs[0]["kind"], "terminal");
    assert_eq!(sb.stored("terminal/20260908T010203Z-42.log"), b"hello\nworld\n");

    // 4. The server lost the tail (its copy is 3 bytes): a 409 makes the client resend from 3.
    sb.store.lock().unwrap().files.get_mut("terminal/20260908T010203Z-42.log").unwrap().truncate(3);
    append(&log, b"!\n");
    sb.once(&[]);
    let reqs = sb.take_requests();
    assert_eq!(reqs.len(), 2, "{reqs:?}");
    assert_eq!(reqs[0]["offset"], 12);
    assert_eq!(reqs[1]["offset"], 3);
    assert_eq!(reqs[1]["truncate"], false);
    assert_eq!(sb.stored("terminal/20260908T010203Z-42.log"), b"hello\nworld\n!\n");

    // 5. The local file shrank (rotated): resend everything with truncate.
    fs::write(&log, b"fresh\n").unwrap();
    sb.once(&[]);
    let reqs = sb.take_requests();
    assert_eq!(reqs.len(), 1, "{reqs:?}");
    assert_eq!(reqs[0]["offset"], 0);
    assert_eq!(reqs[0]["truncate"], true);
    assert_eq!(sb.stored("terminal/20260908T010203Z-42.log"), b"fresh\n");
    assert_eq!(sb.state()["terminal:20260908T010203Z-42.log"]["offset"], 6);
}

#[test]
fn flags_override_the_config_file() {
    let sb = Sandbox::new("flags");
    fs::write(sb.dir("codex").join("s.jsonl"), b"{}\n").unwrap();
    sb.once(&["--host", "other"]);
    let reqs = sb.take_requests();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0]["host"], "other");
    assert_eq!(reqs[0]["kind"], "codex");
    assert_eq!(reqs[0]["path"], "s.jsonl");

    // A wrong token is an error, reported and not retried within the run.
    let out = Command::new(env!("CARGO_BIN_EXE_heron-agent"))
        .args(["once", "--token", "wrong", "--server", &sb.server])
        .env("HERON_AGENT_CONFIG", sb.root.join("missing.toml"))
        .env("HERON_CODEX_DIR", sb.dir("codex"))
        .env("HERON_CLAUDE_DIR", sb.dir("claude"))
        .env("HERON_TERMINALS_DIR", sb.dir("terminals"))
        .env("HERON_STATE_DIR", sb.dir("state2"))
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("HTTP 401"));
}
