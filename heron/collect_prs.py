#!/usr/bin/env python3
"""Add the maintainers' recent pull requests to the collected interactions.

Runs after collect.py. For every repository in config.json's `github`
section it lists pull requests updated in the last `days`, keeps those
opened by a listed maintainer, and drops the ones the swarm agents open
(branch prefixes and labels in the config). Each PR becomes one interaction
with id `P<hash>`: the description, the changed files, one turn per commit
message, then review comments. Citations work the same way as for sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from heron.common import INDEX, INTERACTIONS, load_config, log, now, parse_time, read_json, run, write_json

LIST_FIELDS = "number,title,author,createdAt,updatedAt,state,isDraft,headRefName,baseRefName,url,labels,mergedAt"
VIEW_FIELDS = "body,commits,files,comments,reviews,additions,deletions"
AI_TRAILER_RE = re.compile(r"Co-Authored-By:.*(Claude|Codex|ChatGPT)|Generated with \[Claude Code\]|Claude-Session:", re.IGNORECASE)


def gh(repo: str, args: list[str], token_file: str | None) -> Any:
    env = {}
    if token_file and os.path.exists(token_file):
        env["GH_TOKEN"] = open(token_file).read().strip()
    completed = run(["gh", *args, "-R", repo], env=env, timeout=120)
    if completed.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} on {repo}: {completed.stderr.strip()[-300:]}")
    return json.loads(completed.stdout or "null")


def wanted(pr: dict[str, Any], cfg: dict[str, Any], cutoff: float) -> bool:
    updated = parse_time(pr.get("updatedAt"))
    if updated is None or updated.timestamp() < cutoff:
        return False
    author = (pr.get("author") or {}).get("login", "")
    if author not in cfg["maintainers"]:
        return False
    branch = pr.get("headRefName", "")
    if any(branch.startswith(prefix) for prefix in cfg.get("exclude_branch_prefixes", [])):
        return False
    labels = {label.get("name", "") for label in pr.get("labels", [])}
    if labels & set(cfg.get("exclude_labels", [])):
        return False
    return True


def collect_pr(repo: str, pr: dict[str, Any], token_file: str | None) -> dict[str, Any]:
    number = pr["number"]
    detail = gh(repo, ["pr", "view", str(number), "--json", VIEW_FIELDS], token_file) or {}
    turns: list[dict[str, Any]] = []
    author = (pr.get("author") or {}).get("login", "?")
    created = pr.get("createdAt")
    body = (detail.get("body") or "").strip()
    turns.append({
        "role": "pr",
        "kind": "prompt",
        "time": created,
        "text": f"{pr['title']}\n\n{body}" if body else pr["title"],
        "author": author,
    })
    files = detail.get("files") or []
    if files:
        listing = "\n".join(f"{f.get('path')}  +{f.get('additions', 0)} -{f.get('deletions', 0)}" for f in files[:200])
        if len(files) > 200:
            listing += f"\n… {len(files) - 200} more files"
        turns.append({"role": "pr", "kind": "text", "time": created, "text": f"Changed files ({len(files)}):\n{listing}"})
    ai_assisted = bool(AI_TRAILER_RE.search(body))
    for commit in detail.get("commits") or []:
        message = commit.get("messageHeadline", "")
        if commit.get("messageBody"):
            message += "\n\n" + commit["messageBody"]
        if AI_TRAILER_RE.search(message):
            ai_assisted = True
        who = ", ".join(a.get("login") or a.get("name", "?") for a in commit.get("authors", [])) or author
        turns.append({
            "role": "pr",
            "kind": "text",
            "time": commit.get("committedDate"),
            "text": f"commit {str(commit.get('oid', ''))[:9]} by {who}\n{message}",
        })
    for comment in (detail.get("comments") or []) + (detail.get("reviews") or []):
        text = (comment.get("body") or "").strip()
        if not text:
            continue
        who = (comment.get("author") or {}).get("login", "?")
        state = comment.get("state")
        turns.append({
            "role": "pr",
            "kind": "text",
            "time": comment.get("createdAt") or comment.get("submittedAt"),
            "text": f"{who}{' (' + state.lower() + ')' if state else ''}: {text}",
        })
    for n, turn in enumerate(turns, start=1):
        turn["n"] = n
    state = "merged" if pr.get("mergedAt") else ("draft" if pr.get("isDraft") else str(pr.get("state", "")).lower())
    return {
        "id": "P" + hashlib.sha1(f"{repo}#{number}".encode()).hexdigest()[:8],
        "host": "github",
        "source": "pr",
        "path": pr.get("url", f"{repo}#{number}"),
        "title": f"{repo}#{number} ({state}, {author}): {pr['title']}",
        "started": created,
        "ended": pr.get("updatedAt"),
        "meta": {
            "cwd": None,
            "repo": repo,
            "number": number,
            "url": pr.get("url"),
            "author": author,
            "state": state,
            "branch": pr.get("headRefName"),
            "base": pr.get("baseRefName"),
            "ai_assisted": ai_assisted,
            "additions": detail.get("additions"),
            "deletions": detail.get("deletions"),
        },
        "turns": turns,
    }


def main() -> None:
    config = load_config()
    cfg = config.get("github")
    if not cfg:
        log("no github section in config.json; skipping pull requests")
        return
    cutoff = now().timestamp() - float(cfg.get("days", 14)) * 86400
    index = read_json(INDEX, {"sessions": []})
    entries = [e for e in index.get("sessions", []) if e.get("source") != "pr"]
    kept: set[str] = set()
    for repo in cfg["repos"]:
        token_file = (cfg.get("token_files") or {}).get(repo)
        try:
            listed = gh(repo, ["pr", "list", "--state", "all", "--limit", str(cfg.get("limit", 60)), "--json", LIST_FIELDS], token_file) or []
        except RuntimeError as error:
            log(str(error))
            continue
        count = 0
        for pr in listed:
            if not wanted(pr, cfg, cutoff):
                continue
            try:
                record = collect_pr(repo, pr, token_file)
            except RuntimeError as error:
                log(str(error))
                continue
            write_json(INTERACTIONS / f"{record['id']}.json", record)
            kept.add(record["id"])
            entries.append({
                "id": record["id"],
                "host": "github",
                "source": "pr",
                "path": record["path"],
                "title": record["title"],
                "started": record["started"],
                "ended": record["ended"],
                "cwd": None,
                "turns": len(record["turns"]),
            })
            count += 1
        log(f"{repo}: {count} maintainer pull requests in the last {cfg.get('days', 14)} days")
    for stale in INTERACTIONS.glob("P*.json"):
        if stale.stem not in kept:
            stale.unlink()
    entries.sort(key=lambda e: (parse_time(e.get("ended")) or now()), reverse=True)
    index["sessions"] = entries
    index["generated_at"] = now().isoformat()
    write_json(INDEX, index)


if __name__ == "__main__":
    main()
