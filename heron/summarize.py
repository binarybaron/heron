#!/usr/bin/env python3
"""Turn the collected interactions into the heron's topics, with Claude.

The digest handed to the model is bounded: newest interactions first, each
turn cut to a few hundred characters, the whole thing capped at
HERON_DIGEST_CHARS. The model answers with JSON matching
schemas/summary.json; a failed call keeps the previous summary in place.
"""

from __future__ import annotations

import json
import os
from typing import Any

from heron.common import (
    INDEX,
    INTERACTIONS,
    SUMMARY,
    WINDOW_HOURS,
    claude_structured,
    fail,
    load_prompt,
    load_schema,
    log,
    now,
    read_json,
    write_json,
)

DIGEST_CHARS = int(os.environ.get("HERON_DIGEST_CHARS", "260000"))
SESSION_CHARS = int(os.environ.get("HERON_SESSION_CHARS", "45000"))
PREVIOUS_CHARS = int(os.environ.get("HERON_PREVIOUS_CHARS", "120000"))
LIMITS = {"prompt": 1500, "text": 1200, "tool_use": 320, "tool_result": 320, "terminal": 600}
PR_LIMITS = {"prompt": 2500, "text": 1500}


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def render_turn(sid: str, turn: dict[str, Any]) -> str:
    when = (turn.get("time") or "")[:16].replace("T", " ")
    who = {"user": "user", "assistant": "agent", "tool": "result", "terminal": "terminal", "pr": "pull-request"}.get(turn["role"], turn["role"])
    if turn["kind"] == "tool_use":
        who = "agent-runs"
    limit = PR_LIMITS.get(turn["kind"], 1200) if turn["role"] == "pr" else LIMITS[turn["kind"]]
    return f"[{sid}#{turn['n']} {who} {when}] {short(turn['text'], limit)}"


def session_digest(record: dict[str, Any]) -> str:
    sid = record["id"]
    meta = record.get("meta", {})
    where = f"cwd {meta.get('cwd')}" if meta.get("cwd") else (meta.get("url") or "")
    if record["source"] == "pr":
        where += f" — by {meta.get('author')} — {meta.get('state')} — branch {meta.get('branch')}"
        where += " — carries AI co-author trailers" if meta.get("ai_assisted") else " — written by the maintainer"
    # The host comes right after the source so the model can say which
    # machine did what; interactions from before the field existed have none.
    header = (
        f"## {sid} — {record['source']} on {record.get('host') or '?'} — {record.get('started') or '?'} to {record.get('ended') or '?'}"
        f" — {where} — {record['title']}"
    )
    lines = [render_turn(sid, turn) for turn in record["turns"]]
    body = "\n".join(lines)
    if len(body) > SESSION_CHARS:
        # Keep the beginning (what was asked) and the end (where it got to).
        head_budget = SESSION_CHARS // 3
        tail_budget = SESSION_CHARS - head_budget
        head, tail = [], []
        used = 0
        for line in lines:
            if used + len(line) > head_budget:
                break
            head.append(line)
            used += len(line) + 1
        used = 0
        for line in reversed(lines):
            if used + len(line) > tail_budget:
                break
            tail.append(line)
            used += len(line) + 1
        tail.reverse()
        cut = len(lines) - len(head) - len(tail)
        body = "\n".join(head + [f"… [{cut} turns not shown] …"] + tail)
    return header + "\n" + body


def build_digest(sessions: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    used = 0
    for entry in sessions:  # newest first
        record = read_json(INTERACTIONS / f"{entry['id']}.json")
        if not record:
            continue
        digest = session_digest(record)
        if used + len(digest) > DIGEST_CHARS:
            if used == 0:
                digest = digest[:DIGEST_CHARS]
            else:
                parts.append(f"## {entry['id']} — {entry['source']} on {entry.get('host') or '?'} — {entry['title']} — [not shown: digest budget spent]")
                continue
        parts.append(digest)
        used += len(digest) + 2
    return "\n\n".join(parts)


def main() -> None:
    index = read_json(INDEX)
    if not index or not index.get("sessions"):
        fail("nothing collected; run collect.py first")
    digest = build_digest(index["sessions"])
    log(f"digest of {len(digest)} characters from {len(index['sessions'])} interactions")
    previous = read_json(SUMMARY) or {}
    previous_text = "(none yet)"
    if previous.get("topics"):
        kept = {
            "headline": previous.get("headline"),
            "generated_at": previous.get("generated_at"),
            "topics": [
                {k: t.get(k) for k in ("slug", "title", "status", "one_liner", "milestones", "body_markdown")}
                for t in previous["topics"]
            ],
        }
        previous_text = json.dumps(kept, indent=1, ensure_ascii=False)
        if len(previous_text) > PREVIOUS_CHARS:
            previous_text = previous_text[:PREVIOUS_CHARS] + "\n… [previous summary cut] …"
    prompt = load_prompt("summary.md", window_hours=str(int(WINDOW_HOURS)), digest=digest, previous=previous_text)
    try:
        summary = claude_structured(prompt, load_schema("summary.json"))
    except RuntimeError as error:
        previous = read_json(SUMMARY)
        if previous:
            previous["stale_reason"] = str(error)
            previous["stale_since"] = now().isoformat()
            write_json(SUMMARY, previous)
            log(f"kept the previous summary: {error}")
            return
        fail(f"no summary: {error}")
    summary["generated_at"] = now().isoformat()
    summary["window_hours"] = WINDOW_HOURS
    summary["session_ids"] = [entry["id"] for entry in index["sessions"]]
    write_json(SUMMARY, summary)
    log(f"summary with {len(summary.get('topics', []))} topics: {summary.get('headline', '')}")


if __name__ == "__main__":
    main()
