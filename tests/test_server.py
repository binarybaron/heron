"""The upload protocol, the status endpoint and the site's Basic auth."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (sets HERON_STATE before heron is imported)
from heron import common, server

TOKEN = support.CONFIG["ingest_token"]


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = Path(tempfile.mkdtemp(prefix="heron-server-"))
        # The one-line-per-request log would drown the test output.
        cls.quiet = mock.patch.object(server, "log", lambda message: None)
        cls.quiet.start()
        cls.server = server.make_server(dict(support.CONFIG), cls.state, bind="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.quiet.stop()
        shutil.rmtree(cls.state)

    def request(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None) -> tuple[int, bytes, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), dict(error.headers)

    def upload(self, body: dict, *, token: str = TOKEN) -> tuple[int, dict]:
        status, raw, _ = self.request("POST", "/api/v1/upload", body, headers={"Authorization": f"Bearer {token}"})
        return status, json.loads(raw)

    @staticmethod
    def encoded(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    def test_healthz_needs_no_auth(self) -> None:
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok"))

    def test_append_conflict_and_truncate(self) -> None:
        stored = self.state / "raw" / "laptop" / "claude" / "projects" / "-Users-me" / "abc.jsonl"
        status, reply = self.upload({"host": "laptop", "kind": "claude", "path": "projects/-Users-me/abc.jsonl",
                                     "offset": 0, "data": self.encoded("hello "), "truncate": False})
        self.assertEqual((status, reply), (200, {"size": 6}))
        self.assertEqual(stored.read_bytes(), b"hello ")

        status, reply = self.upload({"host": "laptop", "kind": "claude", "path": "projects/-Users-me/abc.jsonl",
                                     "offset": 6, "data": self.encoded("world"), "truncate": False})
        self.assertEqual((status, reply), (200, {"size": 11}))
        self.assertEqual(stored.read_bytes(), b"hello world")

        # Offset ahead of the file, and behind it without truncate: 409 with the real size.
        for offset in (20, 3):
            status, reply = self.upload({"host": "laptop", "kind": "claude", "path": "projects/-Users-me/abc.jsonl",
                                         "offset": offset, "data": self.encoded("x"), "truncate": False})
            self.assertEqual((status, reply), (409, {"size": 11}), offset)
        self.assertEqual(stored.read_bytes(), b"hello world")

        status, reply = self.upload({"host": "laptop", "kind": "claude", "path": "projects/-Users-me/abc.jsonl",
                                     "offset": 0, "data": self.encoded("new"), "truncate": True})
        self.assertEqual((status, reply), (200, {"size": 3}))
        self.assertEqual(stored.read_bytes(), b"new")

    def test_terminal_kind_lands_in_terminals_dir(self) -> None:
        status, reply = self.upload({"host": "laptop", "kind": "terminal", "path": "20260908T010000Z-42.log",
                                     "offset": 0, "data": self.encoded("$ ls\n"), "truncate": False})
        self.assertEqual(status, 200)
        self.assertTrue((self.state / "raw" / "laptop" / "terminals" / "20260908T010000Z-42.log").is_file())

    def test_path_traversal_rejected(self) -> None:
        bad = ["../etc/passwd", "a/../../b", "/etc/passwd", "a//b", "a/./b", "", "a\\b"]
        for path in bad:
            status, reply = self.upload({"host": "laptop", "kind": "codex", "path": path,
                                         "offset": 0, "data": self.encoded("x"), "truncate": False})
            self.assertEqual(status, 400, path)
            self.assertIn("error", reply)
        for host in ("../x", "a b", "", "x/y"):
            status, _ = self.upload({"host": host, "kind": "codex", "path": "ok.jsonl",
                                     "offset": 0, "data": self.encoded("x"), "truncate": False})
            self.assertEqual(status, 400, host)
        status, _ = self.upload({"host": "laptop", "kind": "shell", "path": "ok.log",
                                 "offset": 0, "data": self.encoded("x"), "truncate": False})
        self.assertEqual(status, 400)
        self.assertFalse((self.state / "raw" / "etc").exists())
        self.assertFalse((self.state / "etc").exists())

    def test_bad_token_401(self) -> None:
        status, reply = self.upload({"host": "laptop", "kind": "claude", "path": "x.jsonl",
                                     "offset": 0, "data": self.encoded("x"), "truncate": False}, token="wrong")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/v1/status")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/v1/status", headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_bad_json_and_oversize_body(self) -> None:
        req = urllib.request.Request(self.base + "/api/v1/upload", data=b"{not json", method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        with caught.exception:
            self.assertEqual(caught.exception.code, 400)
        req = urllib.request.Request(self.base + "/api/v1/upload", data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": str(server.MAX_BODY + 1)})
        with self.assertRaises((urllib.error.HTTPError, ConnectionError)) as caught:
            urllib.request.urlopen(req, timeout=5)
        if isinstance(caught.exception, urllib.error.HTTPError):
            with caught.exception:
                self.assertEqual(caught.exception.code, 413)

    def test_status_counts_per_host(self) -> None:
        self.upload({"host": "desk", "kind": "codex", "path": "2026/a.jsonl", "offset": 0, "data": self.encoded("12345"), "truncate": True})
        self.upload({"host": "desk", "kind": "terminal", "path": "t.log", "offset": 0, "data": self.encoded("12"), "truncate": True})
        status, raw, _ = self.request("GET", "/api/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        hosts = json.loads(raw)["hosts"]
        self.assertEqual(hosts["desk"]["files"], 2)
        self.assertEqual(hosts["desk"]["bytes"], 7)
        self.assertRegex(hosts["desk"]["last_upload"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_site_basic_auth(self) -> None:
        site = self.state / "site"
        (site / "sessions").mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text("<h1>heron</h1>")
        (site / "style.css").write_text("html{}")
        (site / "sessions" / "S00000001.html").write_text("<p>session</p>")
        (self.state / "secret.txt").write_text("not served")

        # The front page without credentials is the sign-in shim, which
        # carries no content; everything else stays a 401.
        status, body, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"heron_key", body)
        self.assertIn(b'id="k"', body)
        status, _, headers = self.request("GET", "/style.css")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        wrong = base64.b64encode(b"reader:bad").decode()
        status, _, _ = self.request("GET", "/style.css", headers={"Authorization": f"Basic {wrong}"})
        self.assertEqual(status, 401)
        status, body, _ = self.request("GET", "/", headers={"Cookie": f"heron_key={support.CONFIG['site_password']}"})
        self.assertEqual((status, body), (200, b"<h1>heron</h1>"))
        status, _, _ = self.request("GET", "/style.css", headers={"Cookie": "heron_key=wrong"})
        self.assertEqual(status, 401)

        good = base64.b64encode(f"{support.CONFIG['site_user']}:{support.CONFIG['site_password']}".encode()).decode()
        auth = {"Authorization": f"Basic {good}"}
        status, body, headers = self.request("GET", "/", headers=auth)
        self.assertEqual((status, body), (200, b"<h1>heron</h1>"))
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        status, body, headers = self.request("GET", "/style.css", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/css"))
        (site / "diagrams").mkdir(exist_ok=True)
        (site / "diagrams" / "a.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        status, _, headers = self.request("GET", "/diagrams/a.svg", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        status, body, _ = self.request("GET", "/sessions/S00000001.html", headers=auth)
        self.assertEqual((status, body), (200, b"<p>session</p>"))
        for path in ("/missing.html", "/../secret.txt", "/sessions/", "/sessions", "/%2e%2e/secret.txt"):
            status, _, _ = self.request("GET", path, headers=auth)
            self.assertEqual(status, 404, path)
        # http.client folds `..` itself; send the raw request line to be sure.
        import socket
        host, port = self.server.server_address[:2]
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(f"GET /sessions/../../secret.txt HTTP/1.0\r\nAuthorization: Basic {good}\r\n\r\n".encode())
            reply = sock.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.1 404") or reply.startswith(b"HTTP/1.0 404"), reply[:40])

    def test_unknown_api_path_404(self) -> None:
        status, _, _ = self.request("GET", "/api/v1/nothing", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 404)

    def test_placeholder_config_refused(self) -> None:
        bad = dict(support.CONFIG, ingest_token="CHANGE-ME")
        with mock.patch.object(common, "log", lambda message: None):
            with self.assertRaises(SystemExit):
                server.make_server(bad, self.state, port=0)
            with self.assertRaises(SystemExit):
                server.make_server({k: v for k, v in support.CONFIG.items() if k != "site_password"}, self.state, port=0)


if __name__ == "__main__":
    unittest.main()
