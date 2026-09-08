"""Shared bits of the heron tooling: paths, logging, subprocesses, Claude.

Everything lives under the state directory (default /var/lib/heron):

    raw/<host>/{claude,codex,terminals}/   files as the agents uploaded them
    state/       normalized interactions, the latest summary, commit-agent runs
    site/        the rendered static site

The config file (see load_config) is the one place that names the state
directory and the window; the HERON_STATE and HERON_WINDOW_HOURS environment
variables override it so one shell can point the tooling at another tree.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

TOOL_DIR = Path(__file__).resolve().parent.parent


def load_config() -> dict[str, Any]:
    """The server-side config: HERON_CONFIG, else /etc/heron/config.json, else the example.

    A file that exists but does not parse is an error, not an empty config;
    a wrong config must not silently run the server with the example's
    placeholders.
    """
    for candidate in (os.environ.get("HERON_CONFIG"), "/etc/heron/config.json", str(TOOL_DIR / "config.example.json")):
        if candidate and Path(candidate).exists():
            return json.loads(Path(candidate).read_text())
    return {}


CONFIG = load_config()
STATE = Path(os.environ.get("HERON_STATE") or CONFIG.get("state") or "/var/lib/heron")
RAW = STATE / "raw"
INTERACTIONS = STATE / "state" / "interactions"
INDEX = STATE / "state" / "index.json"
SUMMARY = STATE / "state" / "summary.json"
COMMIT_RUNS = STATE / "state" / "commit-runs"
SITE = STATE / "site"

WINDOW_HOURS = float(os.environ.get("HERON_WINDOW_HOURS") or CONFIG.get("window_hours") or 48)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def log(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def strip_ansi(text: str) -> str:
    text = ANSI_RE.sub("", text)
    # `script` records carriage returns and backspaces; keep the last write
    # of each line the way a terminal would show it.
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.split("\r")[-1] or line.split("\r")[-2]
        lines.append(line)
    return "\n".join(lines)


SECRET_RES = [
    # Well-known token shapes.
    re.compile(r"\b(sk|rk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # Anything presented as a bearer, or as the value of a key-like name.
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd|authorization|x-access-token)\s*[=:]\s*['\"]?)[A-Za-z0-9._~+/=-]{12,}"),
    # Basic-auth userinfo and tokens embedded in URLs.
    re.compile(r"(://[^/\s:@]+:)[^@\s/]{6,}(@)"),
]


def redact(text: str) -> str:
    """Blank out what looks like a credential before anything is shown or
    summarized. Best effort: the shapes above, not a proof. A transcript can
    still hold a secret in a shape this does not know."""
    for pattern in SECRET_RES:
        if pattern.groups == 0:
            text = pattern.sub("[redacted]", text)
        elif pattern.groups == 2:
            text = pattern.sub(lambda m: m.group(1) + "[redacted]" + m.group(2), text)
        else:
            text = pattern.sub(lambda m: m.group(1) + "[redacted]", text)
    return text


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False))
    tmp.replace(path)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=full_env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@contextmanager
def process_lock(name: str) -> Iterator[bool]:
    """Hold an exclusive lock, or yield False if another run holds it."""
    path = STATE / "state" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def claude_structured(prompt: str, schema: dict[str, Any], *, timeout: int = 1800) -> dict[str, Any]:
    """Ask Claude for one JSON object matching `schema`, with no tools.

    Runs the local `claude` CLI in print mode. The session is not persisted,
    so the heron never summarizes its own summaries.
    """
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("the claude executable is not on PATH")
    command = [
        claude,
        "--print",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        "Bash,Edit,Write,MultiEdit,NotebookEdit,Agent,WebFetch,WebSearch,Read,Glob,Grep",
    ]
    model = os.environ.get("HERON_CLAUDE_MODEL")
    if model:
        command += ["--model", model]
    completed = run(command, stdin=prompt, env={"IS_SANDBOX": "1", "NO_COLOR": "1"}, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"claude exited {completed.returncode}: {completed.stderr.strip()[-2000:]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"claude returned no JSON: {completed.stdout[-2000:]}") from error
    structured = result.get("structured_output") if isinstance(result, dict) else None
    if isinstance(structured, dict):
        return structured
    raw = result.get("result") if isinstance(result, dict) else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(f"claude completed without a structured result: {str(result)[:2000]}")


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((TOOL_DIR / "schemas" / name).read_text())


def load_prompt(name: str, **values: str) -> str:
    text = (TOOL_DIR / "prompts" / name).read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def fail(message: str) -> None:
    log(message)
    sys.exit(1)
