#!/usr/bin/env python3
"""Normalize every recent transcript into one shape.

Sources, all read-only, one directory per host under $HERON_STATE/raw as
the agents uploaded them:

- Claude Code sessions: raw/<host>/claude/**/*.jsonl
- Codex sessions:       raw/<host>/codex/**/*.jsonl
- interactive shells:   raw/<host>/terminals/*.log

With `--local` (or HERON_LOCAL=1) this machine's own home directories are
read as well, as host = the local hostname, so a server that runs no agent
still has something to write about.

Each source becomes one *interaction* with a stable id and a list of numbered
*turns*. The id is a hash of `<host>/<kind>/<relative path>`, so links into a
session stay valid from one run to the next and two hosts with the same file
name do not collide. Only files touched within the window are read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Callable

from heron.common import (
    INDEX,
    INTERACTIONS,
    RAW,
    STATE,
    WINDOW_HOURS,
    log,
    now,
    parse_time,
    strip_ansi,
    write_json,
)

HOME = Path(os.environ.get("HOME", "/root"))

# A parser turns one raw file into an interaction record, or None when the
# file holds nothing worth showing (no prompt, too short).
Collector = Callable[[Path], dict[str, Any] | None]

# How much of one turn is kept in the normalized record. The site shows this
# much; the digest handed to the summarizer cuts further.
TURN_TEXT_LIMIT = 20_000


def interaction_id(host: str, kind: str, relative: str) -> str:
    return "S" + hashlib.sha1(f"{host}/{kind}/{relative}".encode()).hexdigest()[:8]


def recent(path: Path) -> bool:
    try:
        age = now().timestamp() - path.stat().st_mtime
    except OSError:
        return False
    return age <= WINDOW_HOURS * 3600


def clip(text: str, limit: int = TURN_TEXT_LIMIT) -> str:
    text = text.strip("\n")
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n… [{len(text) - limit} characters cut] …\n{tail}"


INJECTED_RE = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|command-message|command-args|command-name|"
    r"environment_context|user_instructions|recommended_plugins|permissions_instructions|collaboration_mode)>[\s\S]*?</\1>",
)


def clean_prompt(text: str) -> str:
    """What the person typed, without the blocks the harness injects."""
    text = INJECTED_RE.sub("", text)
    text = re.sub(r"<(?:command-name|command-message|command-args)>[^\n]*", "", text)
    return text.strip()


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def describe_tool_use(name: str, inputs: dict[str, Any]) -> str:
    if name == "Bash":
        description = inputs.get("description")
        command = inputs.get("command", "")
        return f"$ {command}" + (f"\n# {description}" if description else "")
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        path = inputs.get("file_path") or inputs.get("notebook_path") or ""
        detail = inputs.get("content") or inputs.get("new_string") or ""
        return f"{name} {path}\n{clip(str(detail), 2000)}"
    if name in {"Read", "Glob", "Grep"}:
        return f"{name} " + json.dumps({k: v for k, v in inputs.items()}, ensure_ascii=False)[:500]
    return f"{name} " + clip(json.dumps(inputs, ensure_ascii=False), 3000)


def touched_paths(name: str, inputs: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "notebook_path"):
        value = inputs.get(key)
        if isinstance(value, str):
            paths.append(value)
    if name == "Bash":
        command = str(inputs.get("command", ""))
        paths += re.findall(r"(?<![\w-])(/[\w./-]+\.(?:rs|ts|tsx|js|json|toml|md|py|sh|yml|yaml|html|css))\b", command)
    return paths


def collect_claude(path: Path) -> dict[str, Any] | None:
    turns: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"cwd": None, "branch": None, "version": None}
    first_prompt = None
    started = ended = None
    with path.open() as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind not in {"user", "assistant"}:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            stamp = entry.get("timestamp")
            started = started or stamp
            ended = stamp or ended
            meta["cwd"] = entry.get("cwd") or meta["cwd"]
            meta["branch"] = entry.get("gitBranch") or meta["branch"]
            meta["version"] = entry.get("version") or meta["version"]
            content = message.get("content")
            if kind == "user":
                if isinstance(content, str):
                    text = clean_prompt(content)
                    if text:
                        turns.append({"role": "user", "kind": "prompt", "time": stamp, "text": clip(text)})
                        first_prompt = first_prompt or text
                    continue
                if not isinstance(content, list):
                    continue
                text = clean_prompt(content_text(content))
                if text:
                    turns.append({"role": "user", "kind": "prompt", "time": stamp, "text": clip(text)})
                    first_prompt = first_prompt or text
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        result = content_text(part.get("content"))
                        if result.strip():
                            turns.append({
                                "role": "tool",
                                "kind": "tool_result",
                                "time": stamp,
                                "text": clip(result),
                                "tool_use_id": part.get("tool_use_id"),
                            })
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    turns.append({"role": "assistant", "kind": "text", "time": stamp, "text": clip(str(part["text"]))})
                elif part.get("type") == "tool_use":
                    name = str(part.get("name", "tool"))
                    inputs = part.get("input") if isinstance(part.get("input"), dict) else {}
                    turns.append({
                        "role": "assistant",
                        "kind": "tool_use",
                        "time": stamp,
                        "text": clip(describe_tool_use(name, inputs), 6000),
                        "tool": name,
                        "paths": touched_paths(name, inputs),
                        "tool_use_id": part.get("id"),
                    })
    if first_prompt is None:
        return None
    return {
        "source": "claude",
        "path": str(path),
        "title": clip(first_prompt.strip().splitlines()[0] if first_prompt.strip() else "(untitled)", 140),
        "started": started,
        "ended": ended,
        "meta": meta,
        "turns": turns,
    }


def collect_codex(path: Path) -> dict[str, Any] | None:
    turns: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"cwd": None, "origin": None, "model": None}
    first_prompt = None
    started = ended = None
    with path.open() as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("timestamp")
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if entry.get("type") == "session_meta":
                meta["cwd"] = payload.get("cwd")
                meta["origin"] = payload.get("originator") or payload.get("source")
                meta["model"] = payload.get("model")
                started = started or payload.get("timestamp") or stamp
                continue
            if entry.get("type") != "response_item":
                continue
            ptype = payload.get("type")
            ended = stamp or ended
            started = started or stamp
            if ptype == "message":
                role = payload.get("role")
                text = "\n".join(
                    str(part.get("text", ""))
                    for part in payload.get("content", [])
                    if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
                )
                if not text.strip():
                    continue
                if role == "user":
                    text = clean_prompt(text)
                    if not text or text.startswith("<"):
                        continue
                    turns.append({"role": "user", "kind": "prompt", "time": stamp, "text": clip(text)})
                    first_prompt = first_prompt or text
                else:
                    turns.append({"role": "assistant", "kind": "text", "time": stamp, "text": clip(text)})
            elif ptype == "function_call":
                name = str(payload.get("name", "tool"))
                arguments = payload.get("arguments")
                try:
                    inputs = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
                except json.JSONDecodeError:
                    inputs = {"arguments": arguments}
                if not isinstance(inputs, dict):
                    inputs = {"arguments": inputs}
                command = inputs.get("cmd") or inputs.get("command")
                if isinstance(command, list):
                    command = " ".join(str(c) for c in command)
                text = f"$ {command}" if command else f"{name} {json.dumps(inputs)[:2000]}"
                turns.append({
                    "role": "assistant",
                    "kind": "tool_use",
                    "time": stamp,
                    "text": clip(text, 6000),
                    "tool": name,
                    "paths": touched_paths("Bash", {"command": str(command or "")}),
                    "tool_use_id": payload.get("call_id"),
                })
            elif ptype == "function_call_output":
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("output") or json.dumps(output)
                text = str(output or "")
                if text.strip():
                    turns.append({
                        "role": "tool",
                        "kind": "tool_result",
                        "time": stamp,
                        "text": clip(text),
                        "tool_use_id": payload.get("call_id"),
                    })
    if first_prompt is None:
        return None
    return {
        "source": "codex",
        "path": str(path),
        "title": clip(first_prompt.strip().splitlines()[0], 140),
        "started": started,
        "ended": ended,
        "meta": meta,
        "turns": turns,
    }


def collect_terminal(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return None
    text = strip_ansi(raw)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 3:
        return None
    stat = path.stat()
    turns = []
    block: list[str] = []
    prompt_re = re.compile(r"^\S*[$#] ")
    for line in lines:
        if prompt_re.match(line) and block:
            turns.append({"role": "terminal", "kind": "terminal", "time": None, "text": clip("\n".join(block))})
            block = []
        block.append(line)
    if block:
        turns.append({"role": "terminal", "kind": "terminal", "time": None, "text": clip("\n".join(block))})
    started = None
    match = re.match(r"(\d{8}T\d{6}Z)", path.name)
    if match:
        started = f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:8]}T{match[1][9:11]}:{match[1][11:13]}:{match[1][13:15]}Z"
    import datetime as dt

    ended = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    first = next((line for line in lines if prompt_re.match(line)), lines[0])
    return {
        "source": "terminal",
        "path": str(path),
        "title": clip(first, 140),
        "started": started,
        "ended": ended,
        "meta": {"cwd": None},
        "turns": turns,
    }


def source_dirs(host_dir: Path) -> list[tuple[str, Path, str, Collector]]:
    """(kind, directory, glob, parser) for one host's raw tree."""
    return [
        ("claude", host_dir / "claude", "**/*.jsonl", collect_claude),
        ("codex", host_dir / "codex", "**/*.jsonl", collect_codex),
        ("terminals", host_dir / "terminals", "*.log", collect_terminal),
    ]


def local_dirs() -> list[tuple[str, Path, str, Collector]]:
    """This machine's own transcripts, laid out the way the agent would upload them.

    The legacy $HERON_STATE/terminals of deploy/record-shell.sh is read too,
    since a server shell may still source that hook.
    """
    return [
        ("claude", HOME / ".claude" / "projects", "**/*.jsonl", collect_claude),
        ("codex", HOME / ".codex" / "sessions", "**/*.jsonl", collect_codex),
        ("terminals", HOME / ".local" / "share" / "heron" / "terminals", "*.log", collect_terminal),
        ("terminals", STATE / "terminals", "*.log", collect_terminal),
    ]


def hosts() -> list[tuple[str, list[tuple[str, Path, str, Collector]]]]:
    found: list[tuple[str, list[tuple[str, Path, str, Collector]]]] = []
    if RAW.is_dir():
        for host_dir in sorted(RAW.iterdir()):
            if host_dir.is_dir():
                found.append((host_dir.name, source_dirs(host_dir)))
    return found


def collect_one(host: str, kind: str, root: Path, path: Path, collector: Collector) -> dict[str, Any] | None:
    record = collector(path)
    if record is None or not record["turns"]:
        return None
    relative = path.relative_to(root).as_posix()
    record["id"] = interaction_id(host, kind, relative)
    record["host"] = host
    record["path"] = f"{kind}/{relative}"
    for number, turn in enumerate(record["turns"], start=1):
        turn["n"] = number
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="normalize the raw transcripts into interactions")
    parser.add_argument("--local", action="store_true", help="also read this machine's own ~/.claude, ~/.codex and terminals")
    args = parser.parse_args()
    local = args.local or os.environ.get("HERON_LOCAL") == "1"

    INTERACTIONS.mkdir(parents=True, exist_ok=True)
    sources = hosts()
    if local:
        sources.append((socket.gethostname(), local_dirs()))

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for host, dirs in sources:
        for kind, root, pattern, collector in dirs:
            if not root.is_dir():
                continue
            for path in sorted(root.glob(pattern)):
                if not path.is_file() or not recent(path):
                    continue
                record = collect_one(host, kind, root, path, collector)
                if record is None or record["id"] in seen:
                    continue
                seen.add(record["id"])
                write_json(INTERACTIONS / f"{record['id']}.json", record)
                found.append({
                    "id": record["id"],
                    "host": host,
                    "source": record["source"],
                    "path": record["path"],
                    "title": record["title"],
                    "started": record["started"],
                    "ended": record["ended"],
                    "cwd": record["meta"].get("cwd"),
                    "turns": len(record["turns"]),
                })

    # Drop records of sources that fell out of the window.
    for stale in INTERACTIONS.glob("S*.json"):
        if stale.stem not in seen:
            stale.unlink()

    found.sort(key=lambda e: (parse_time(e["ended"]) or now()), reverse=True)
    write_json(INDEX, {"generated_at": now().isoformat(), "window_hours": WINDOW_HOURS, "hosts": [h for h, _ in sources], "sessions": found})
    log(f"collected {len(found)} interactions ({sum(e['turns'] for e in found)} turns) from {len(sources)} hosts")


if __name__ == "__main__":
    main()
