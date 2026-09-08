#!/usr/bin/env python3
"""The heron server: takes uploads from the agents and serves the site.

One ThreadingHTTPServer, no framework:

    POST /api/v1/upload    append (or replace) a raw transcript file
    GET  /api/v1/status    per host: files, bytes, last upload
    GET  /healthz          `ok`, no auth, for the reverse proxy's checks
    GET  /…                the static site under $HERON_STATE/site

`/api/v1/*` wants `Authorization: Bearer <ingest_token>`; the site wants
HTTP Basic auth with `site_user` / `site_password`. Transcripts hold
secrets, so the server refuses to start with the example config's
placeholders, and nothing outside site/ is ever served.

Uploads land in raw/<host>/<kind>/<path>; kind `terminal` is stored under
`terminals/` so the raw tree reads like the design document. Appends are
serialized with one lock: the agent re-sends from the size the server
reports, so an interleaved write would corrupt a file for good.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hmac
import json
import posixpath
import re
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from http.cookies import CookieError, SimpleCookie
from urllib.parse import unquote, urlsplit

from heron.common import STATE, fail, load_config, log

MAX_BODY = 8 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
KIND_DIRS = {"claude": "claude", "codex": "codex", "terminal": "terminals"}
PLACEHOLDER = "CHANGE-ME"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class UploadError(Exception):
    """A request the server understood and rejected; carries the status."""

    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def validate_path(path: Any) -> str:
    """The relative path of an uploaded file, or an UploadError.

    Every segment must be a plain name: no `..`, no empty segment (which
    also covers a leading `/`), no backslash or control character that a
    different file system might read as a separator.
    """
    if not isinstance(path, str) or not path:
        raise UploadError(HTTPStatus.BAD_REQUEST, "path must be a non-empty string")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise UploadError(HTTPStatus.BAD_REQUEST, "path may not start with / or contain backslashes")
    for segment in path.split("/"):
        if segment in {"", ".", ".."}:
            raise UploadError(HTTPStatus.BAD_REQUEST, "path may not contain empty, . or .. segments")
        if any(ord(c) < 32 for c in segment):
            raise UploadError(HTTPStatus.BAD_REQUEST, "path contains control characters")
    return path


def validate_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise UploadError(HTTPStatus.BAD_REQUEST, f"{what} must match [A-Za-z0-9._-]+")
    return value


class HeronServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: dict[str, Any], state: Path):
        self.config = config
        self.state = state
        self.raw = state / "raw"
        self.site = state / "site"
        self.ingest_token = str(config["ingest_token"])
        self.site_user = str(config["site_user"])
        self.site_password = str(config["site_password"])
        self.base_path = str(config.get("base_path", "")).rstrip("/")
        self.write_lock = threading.Lock()
        super().__init__(address, Handler)

    def store(self, host: str, kind: str, path: str, data: bytes, offset: int, truncate: bool) -> tuple[HTTPStatus, int]:
        """Append `data` at `offset`, or replace the file; returns (status, size).

        The final containment check is belt and braces on top of
        validate_path: a symlink planted inside raw/ could otherwise lead
        an append out of the tree.
        """
        target = self.raw / host / KIND_DIRS[kind] / path
        with self.write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.resolve().is_relative_to(self.raw.resolve()):
                raise UploadError(HTTPStatus.BAD_REQUEST, "path escapes the raw tree")
            size = target.stat().st_size if target.exists() else 0
            if truncate:
                target.write_bytes(data)
                return HTTPStatus.OK, len(data)
            if offset != size:
                return HTTPStatus.CONFLICT, size
            with target.open("ab") as handle:
                handle.write(data)
            return HTTPStatus.OK, size + len(data)

    def status(self) -> dict[str, Any]:
        hosts: dict[str, Any] = {}
        if self.raw.is_dir():
            for host_dir in sorted(self.raw.iterdir()):
                if not host_dir.is_dir():
                    continue
                files = 0
                total = 0
                latest = 0.0
                for file in host_dir.rglob("*"):
                    if not file.is_file():
                        continue
                    stat = file.stat()
                    files += 1
                    total += stat.st_size
                    latest = max(latest, stat.st_mtime)
                hosts[host_dir.name] = {
                    "files": files,
                    "bytes": total,
                    "last_upload": dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat().replace("+00:00", "Z") if files else None,
                }
        return {"hosts": hosts}

    def site_file(self, url_path: str) -> Path | None:
        """The file under site/ that a URL path names, or None.

        Only regular files inside site/ are served; `..`, symlinks out of
        the directory and directories themselves all answer 404.
        """
        clean = posixpath.normpath(unquote(url_path))
        if clean in {"/", "."}:
            clean = "/index.html"
        if not clean.startswith("/") or "\x00" in clean:
            return None
        relative = clean.lstrip("/")
        if any(segment in {"", ".", ".."} for segment in relative.split("/")):
            return None
        site = self.site.resolve()
        target = (self.site / relative).resolve()
        if not target.is_relative_to(site) or not target.is_file():
            return None
        return target


def sign_in_page(base_path: str) -> str:
    cookie_path = base_path or "/"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>heron</title>
<style>html{{background:#fff;color:#000}}body{{font:15px/1.6 ui-monospace,Menlo,Consolas,monospace;max-width:40rem;margin:4rem auto;padding:0 1rem}}
input{{font:inherit;border:1px solid #000;padding:.3em .5em;width:22rem}}button{{font:inherit;border:1px solid #000;background:#fff;padding:.3em .8em}}</style></head>
<body><h1>heron</h1><p id="msg">This page needs the key. Open the link that ends in <code>#key=…</code>, or paste the key:</p>
<form id="f"><input id="k" type="password" autocomplete="off" placeholder="key"> <button>open</button></form>
<script>
(function () {{
  function setKey(key) {{
    var secure = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = 'heron_key=' + encodeURIComponent(key) + '; Path={cookie_path}; Max-Age=31536000; SameSite=Strict' + secure;
    location.replace(location.pathname + location.search + location.hash);
  }}
  var m = /(?:^#|[#&])key=([^&]+)/.exec(location.hash);
  if (m) {{ setKey(decodeURIComponent(m[1])); return; }}
  document.getElementById('msg').textContent = 'This page needs the key. Open a link that carries #key=…, or paste the key:';
  document.getElementById('f').addEventListener('submit', function (e) {{ e.preventDefault(); var v = document.getElementById('k').value.trim(); if (v) setKey(v); }});
}})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server: HeronServer
    protocol_version = "HTTP/1.1"
    server_version = "heron/1"
    sys_version = ""

    # One line per request, written by respond(); the default per-response
    # logging would double it.
    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.address_string()} {format % args}")

    def respond(self, status: HTTPStatus, body: bytes, content_type: str, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        log(f"{self.address_string()} {self.command} {self.path} {int(status)} {len(body)}")

    def respond_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self.respond(status, json.dumps(value).encode(), "application/json")

    def bearer_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), self.server.ingest_token)

    def cookie_ok(self) -> bool:
        """The site password as a cookie, set by the sign-in page from the
        URL fragment: `/#key=<site_password>` never reaches the server, so a
        link with the key in it can be pasted around without logging it."""
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get("heron_key")
        return morsel is not None and hmac.compare_digest(morsel.value, self.server.site_password)

    def site_ok(self) -> bool:
        return self.basic_ok() or self.cookie_ok()

    def basic_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        user_ok = hmac.compare_digest(user, self.server.site_user)
        password_ok = hmac.compare_digest(password, self.server.site_password)
        return user_ok and password_ok

    def read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            raise UploadError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        try:
            size = int(length)
        except ValueError:
            raise UploadError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from None
        if size < 0 or size > MAX_BODY:
            raise UploadError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body over {MAX_BODY} bytes")
        return self.rfile.read(size)

    def route_path(self) -> str:
        """The request path with the configured base path stripped.

        Behind a proxy that keeps its mount prefix (a Tailscale funnel path,
        an nginx location without a trailing slash rewrite) every request
        arrives as `/heron/...`; the routes below are written without it.
        """
        path = urlsplit(self.path).path
        base = self.server.base_path
        if base and (path == base or path.startswith(base + "/")):
            path = path[len(base):] or "/"
        return path

    def do_GET(self) -> None:
        path = self.route_path()
        if path == "/healthz":
            self.respond(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")
            return
        if path.startswith("/api/v1/"):
            if not self.bearer_ok():
                self.respond_json(HTTPStatus.UNAUTHORIZED, {"error": "bearer token required"})
                return
            if path == "/api/v1/status":
                self.respond_json(HTTPStatus.OK, self.server.status())
                return
            if path == "/api/v1/upload":
                self.respond_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "POST to upload"})
                return
            self.respond_json(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})
            return
        if not self.site_ok():
            if path == "/" or path.endswith(".html"):
                # The sign-in page carries no secret: it only moves a key from
                # the fragment into a cookie and reloads.
                self.respond(HTTPStatus.OK, sign_in_page(self.server.base_path).encode(), "text/html; charset=utf-8")
                return
            self.respond(
                HTTPStatus.UNAUTHORIZED,
                b"heron: sign in\n",
                "text/plain; charset=utf-8",
                headers={"WWW-Authenticate": 'Basic realm="heron", charset="UTF-8"'},
            )
            return
        target = self.server.site_file(path)
        if target is None:
            self.respond(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.respond(HTTPStatus.OK, target.read_bytes(), content_type)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = self.route_path()
        # A rejected POST leaves its body unread; the connection must close
        # or the next request would be parsed out of that body.
        self.close_connection = True
        try:
            if path != "/api/v1/upload":
                if path.startswith("/api/v1/") and not self.bearer_ok():
                    raise UploadError(HTTPStatus.UNAUTHORIZED, "bearer token required")
                raise UploadError(HTTPStatus.NOT_FOUND, "no such endpoint")
            if not self.bearer_ok():
                raise UploadError(HTTPStatus.UNAUTHORIZED, "bearer token required")
            body = self.read_body()
            self.close_connection = False
            status, size = self.upload(body)
            self.respond_json(status, {"size": size})
        except UploadError as error:
            self.respond_json(error.status, {"error": str(error)})

    def upload(self, body: bytes) -> tuple[HTTPStatus, int]:
        try:
            request = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise UploadError(HTTPStatus.BAD_REQUEST, "body is not JSON") from None
        if not isinstance(request, dict):
            raise UploadError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        host = validate_name(request.get("host"), "host")
        kind = validate_name(request.get("kind"), "kind")
        if kind not in KIND_DIRS:
            raise UploadError(HTTPStatus.BAD_REQUEST, f"kind must be one of {', '.join(KIND_DIRS)}")
        path = validate_path(request.get("path"))
        offset = request.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise UploadError(HTTPStatus.BAD_REQUEST, "offset must be a non-negative integer")
        truncate = request.get("truncate", False)
        if not isinstance(truncate, bool):
            raise UploadError(HTTPStatus.BAD_REQUEST, "truncate must be a boolean")
        encoded = request.get("data")
        if not isinstance(encoded, str):
            raise UploadError(HTTPStatus.BAD_REQUEST, "data must be a base64 string")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise UploadError(HTTPStatus.BAD_REQUEST, "data is not valid base64") from None
        return self.server.store(host, kind, path, data, offset, truncate)


def check_config(config: dict[str, Any]) -> None:
    """Refuse to serve with a missing or placeholder secret."""
    for key in ("ingest_token", "site_user", "site_password"):
        value = config.get(key)
        if not isinstance(value, str) or not value:
            fail(f"config: {key} is missing; edit /etc/heron/config.json (or HERON_CONFIG)")
        if value == PLACEHOLDER:
            fail(f"config: {key} still holds the example placeholder; edit /etc/heron/config.json (or HERON_CONFIG)")


def make_server(config: dict[str, Any], state: Path, *, bind: str | None = None, port: int | None = None) -> HeronServer:
    check_config(config)
    address = (bind if bind is not None else str(config.get("bind", "127.0.0.1")), port if port is not None else int(config.get("port", 8097)))
    state.mkdir(parents=True, exist_ok=True)
    return HeronServer(address, config, state)


def main() -> None:
    server = make_server(load_config(), STATE)
    host, port = server.server_address[:2]
    log(f"heron server on http://{host}:{port}, state {server.state}")

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("heron server stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
