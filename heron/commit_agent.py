#!/usr/bin/env python3
"""Commit and push the work that has been shown to pass its tests.

For each configured checkout:

1. List uncommitted changes to files, ignoring the configured paths and
   anything edited in the last `quiet_minutes` (someone is still on it).
2. For every changed file, look through the collected transcripts for a
   turn that edited it, followed later in the same session by a check
   passing. That later turn is the file's evidence. No evidence, no commit.
3. Ask Claude to organize the eligible files into commits with messages.
4. Stage each commit's files, run the checks that cover them again, and
   commit only if they pass. Skipped groups are unstaged.
5. Push the branch to its remote, never with force. A rejected push is
   reported and left for a person.

Every run writes a record under state/commit-runs that the site shows.
Set HERON_DRY_RUN=1 to plan without staging, committing or pushing.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from heron.common import (
    COMMIT_RUNS,
    INDEX,
    INTERACTIONS,
    claude_structured,
    load_config,
    load_prompt,
    load_schema,
    log,
    now,
    parse_time,
    process_lock,
    read_json,
    run,
    write_json,
)

DRY_RUN = os.environ.get("HERON_DRY_RUN") == "1"
PUSH = os.environ.get("HERON_PUSH", "1") != "0"
DEV_ENV = Path("/root/agent-tooling/dev-env.sh")

# What "the checks passed" looks like in a tool result.
SUCCESS_RE = re.compile(
    r"Tests\s+\d+ passed|Test Files\s+\d+ passed|test result: ok\b|"
    r"Finished `(?:dev|release|test)` profile|check exit: 0\b|wasm check exit: 0|"
    r"host check exit: 0|build exit: 0\b|✓ built in|\bexit 0\b|All checks passed",
    re.IGNORECASE,
)
FAILURE_RE = re.compile(r"\berror\[E\d+\]|FAIL\b|Tests\s+\d+ failed|failed to compile|exit code: [1-9]|✖ \d+ problems? \(\d*[1-9]\d* errors?", re.IGNORECASE)


def git(repo: Path, *args: str, check: bool = True) -> str:
    completed = run(["git", *args], cwd=repo)
    if check and completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def changed_files(repo: Path, ignore: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (candidate files, (file, reason) pairs that are left alone)."""
    candidates: list[str] = []
    left: list[tuple[str, str]] = []
    for line in git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines():
        status, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(path == ig.rstrip("/") or path.startswith(ig) for ig in ignore):
            continue
        if status.strip() in {"m"}:
            left.append((path, "submodule change"))
            continue
        if "D" in status:
            left.append((path, "deletion; the agent never commits deletions"))
            continue
        candidates.append(path)
    return candidates, left


def recently_edited(repo: Path, path: str, quiet_minutes: int) -> bool:
    try:
        mtime = (repo / path).stat().st_mtime
    except OSError:
        return True
    return now().timestamp() - mtime < quiet_minutes * 60


def evidence_for(repo: Path, files: list[str], evidence_hours: float) -> dict[str, list[dict[str, Any]]]:
    """Per file: turns that show a check passing after the file's last edit in that session."""
    index = read_json(INDEX, {"sessions": []})
    cutoff = now() - dt.timedelta(hours=evidence_hours)
    absolute = {str((repo / f).resolve()): f for f in files}
    found: dict[str, list[dict[str, Any]]] = {f: [] for f in files}
    for entry in index.get("sessions", []):
        record = read_json(INTERACTIONS / f"{entry['id']}.json")
        if not record:
            continue
        sid = record["id"]
        last_touch: dict[str, int] = {}
        for turn in record["turns"]:
            when = parse_time(turn.get("time"))
            if when is not None and when < cutoff:
                continue
            if turn["kind"] == "tool_use":
                for p in turn.get("paths", []):
                    rel = absolute.get(str(Path(p).resolve())) if p.startswith("/") else (p if p in found else None)
                    if rel is not None:
                        last_touch[rel] = turn["n"]
                continue
            if turn["kind"] not in {"tool_result", "terminal"}:
                continue
            text = turn["text"]
            if not SUCCESS_RE.search(text) or FAILURE_RE.search(text):
                continue
            for rel, touched_at in last_touch.items():
                if turn["n"] > touched_at:
                    found[rel].append({
                        "ref": f"{sid}#{turn['n']}",
                        "time": turn.get("time"),
                        "excerpt": " ".join(text.split())[:240],
                    })
    return found


def file_diff(repo: Path, path: str) -> str:
    tracked = run(["git", "ls-files", "--error-unmatch", path], cwd=repo).returncode == 0
    if tracked:
        text = git(repo, "diff", "--", path)
    else:
        try:
            text = "new file: " + path + "\n" + (repo / path).read_text(errors="replace")
        except OSError:
            text = f"new file: {path} (unreadable)"
    lines = text.splitlines()
    if len(lines) > 400:
        text = "\n".join(lines[:300] + [f"… [{len(lines) - 360} lines cut] …"] + lines[-60:])
    return text


def crate_of(repo: Path, path: str) -> str | None:
    current = (repo / path).parent
    while current != repo and current != current.parent:
        manifest = current / "Cargo.toml"
        if manifest.exists():
            match = re.search(r'^\s*name\s*=\s*"([^"]+)"', manifest.read_text(), re.M)
            return match[1] if match else None
        current = current.parent
    return None


def shell(command: str, cwd: Path, timeout: int) -> tuple[bool, str]:
    """Run through bash with the build environment sourced."""
    prelude = f"source {DEV_ENV} >/dev/null 2>&1; " if DEV_ENV.exists() else ""
    completed = run(["bash", "-lc", prelude + command], cwd=cwd, timeout=timeout)
    tail = (completed.stdout + completed.stderr)[-3000:]
    return completed.returncode == 0, tail


def checks_for(repo: Path, files: list[str]) -> list[tuple[str, str, Path, int]]:
    """(label, command, cwd, timeout) for the checks that cover these files."""
    checks: list[tuple[str, str, Path, int]] = []
    web = any(f.startswith(("moksha/web/", "src-gui-shared/")) for f in files)
    if web:
        checks.append(("tsc", "cd moksha/web && yarn tsc", repo, 900))
        checks.append(("eslint", "npx eslint moksha/web/src src-gui-shared/src", repo, 900))
        checks.append(("vitest", "cd moksha/web && yarn test", repo, 1200))
    wasm_crates = {"moksha/engine", "moksha/esplora-balancer", "moksha/monero-balancer"}
    host_crates: set[str] = set()
    for f in files:
        if not f.endswith(".rs") and not f.endswith("Cargo.toml"):
            continue
        wasm = next((c for c in wasm_crates if f.startswith(c + "/")), None)
        if wasm:
            checks.append((
                f"wasm check {wasm}",
                f"cd {shlex.quote(wasm)} && env -u CC -u CXX cargo check --locked --all-features --tests --target wasm32-unknown-unknown",
                repo,
                2400,
            ))
            continue
        crate = crate_of(repo, f)
        if crate:
            host_crates.add(crate)
    for crate in sorted(host_crates):
        checks.append((f"cargo check {crate}", f"cargo check -p {shlex.quote(crate)} --all-features --tests", repo, 3600))
    # One entry per distinct command.
    seen: set[str] = set()
    unique = []
    for check in checks:
        if check[1] in seen:
            continue
        seen.add(check[1])
        unique.append(check)
    return unique


def process_repo(cfg: dict[str, Any], quiet_minutes: int, evidence_hours: float) -> dict[str, Any]:
    repo = Path(cfg["path"])
    record: dict[str, Any] = {
        "repo": str(repo),
        "started": now().isoformat(),
        "dry_run": DRY_RUN,
        "commits": [],
        "skipped": [],
        "notes": [],
        "outcome": "nothing to do",
    }
    if (repo / ".git" / "index.lock").exists():
        record["outcome"] = "skipped: another git process holds the index"
        return record
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != cfg["branch"]:
        record["outcome"] = f"skipped: checkout is on {branch}, not {cfg['branch']}"
        return record

    candidates, left = changed_files(repo, cfg.get("ignore", []))
    record["skipped"] += [{"file": f, "reason": r} for f, r in left]
    eligible: dict[str, list[dict[str, Any]]] = {}
    ineligible: list[tuple[str, str]] = []
    evidence = evidence_for(repo, candidates, evidence_hours)
    for path in candidates:
        if recently_edited(repo, path, quiet_minutes):
            ineligible.append((path, f"edited in the last {quiet_minutes} minutes"))
        elif not evidence.get(path):
            ineligible.append((path, "no transcript shows a check passing after its last edit"))
        else:
            eligible[path] = evidence[path]
    record["skipped"] += [{"file": f, "reason": r} for f, r in ineligible]
    if not eligible:
        record["outcome"] = f"nothing eligible ({len(candidates)} changed files)"
        return record

    eligible_text = "\n".join(
        f"- {path}\n" + "\n".join(f"    {e['ref']} ({(e.get('time') or '')[:16]}): {e['excerpt']}" for e in refs[-4:])
        for path, refs in eligible.items()
    )
    ineligible_text = "\n".join(f"- {path}: {reason}" for path, reason in ineligible) or "(none)"
    diffs = "\n\n".join(f"=== {path}\n{file_diff(repo, path)}" for path in eligible)
    if len(diffs) > 200_000:
        diffs = diffs[:200_000] + "\n… [diff budget spent] …"
    prompt = load_prompt(
        "commits.md",
        repo=str(repo),
        branch=cfg["branch"],
        remote=cfg["remote"],
        eligible=eligible_text,
        ineligible=ineligible_text,
        diffs=diffs,
    )
    plan = claude_structured(prompt, load_schema("commits.json"))
    record["plan"] = plan
    record["skipped"] += [s for s in plan.get("skipped", []) if s.get("file") in eligible]

    made = 0
    for commit in plan.get("commits", []):
        files = [f for f in commit.get("files", []) if f in eligible]
        dropped = [f for f in commit.get("files", []) if f not in eligible]
        if dropped:
            record["notes"].append(f"dropped ineligible files from '{commit.get('subject')}': {', '.join(dropped)}")
        if not files:
            continue
        evidence_refs = sorted({r for r in commit.get("evidence", []) if any(r == e["ref"] for refs in eligible.values() for e in refs)})
        if not evidence_refs:
            evidence_refs = sorted({e["ref"] for f in files for e in eligible[f]})[:6]
        if DRY_RUN:
            record["commits"].append({"sha": "(dry run)", "subject": commit["subject"], "files": files, "evidence": evidence_refs})
            continue
        git(repo, "add", "--", *files)
        failed = None
        for label, command, cwd, timeout in checks_for(repo, files):
            ok, tail = shell(command, cwd, timeout)
            record["notes"].append(f"{label} for '{commit['subject']}': {'ok' if ok else 'FAILED'}")
            if not ok:
                failed = (label, tail)
                break
        if failed:
            git(repo, "reset", "-q", "--", *files)
            record["skipped"] += [{"file": f, "reason": f"{failed[0]} failed when re-run: {failed[1][-300:]}"} for f in files]
            continue
        message = commit["subject"].strip() + "\n\n" + commit.get("body", "").strip() + "\n\n" + "Evidence: " + ", ".join(evidence_refs) + "\nCommitted-By: heron commit agent\n"
        completed = run(["git", "commit", "-q", "-F", "-"], cwd=repo, stdin=message)
        if completed.returncode:
            git(repo, "reset", "-q", "--", *files)
            record["notes"].append(f"commit failed: {completed.stderr.strip()[-300:]}")
            continue
        sha = git(repo, "rev-parse", "HEAD").strip()
        record["commits"].append({"sha": sha, "subject": commit["subject"], "files": files, "evidence": evidence_refs})
        made += 1
        log(f"committed {sha[:9]} {commit['subject']}")

    if DRY_RUN:
        record["outcome"] = f"dry run: {len(record['commits'])} commits planned"
        return record
    if made == 0:
        record["outcome"] = "no commit passed its checks"
        return record
    if not PUSH:
        record["outcome"] = f"{made} commits, push disabled"
        return record
    completed = run(["git", "push", cfg["remote"], f"HEAD:{cfg['branch']}"], cwd=repo, timeout=300)
    if completed.returncode:
        record["outcome"] = f"{made} commits, push rejected: {completed.stderr.strip()[-300:]}"
        record["notes"].append("the push was not forced; rebase or merge by hand")
    else:
        record["outcome"] = f"{made} commits pushed to {cfg['remote']}/{cfg['branch']}"
    return record


def main() -> None:
    config = load_config()
    if not config.get("repos"):
        log("no repos configured; nothing to do")
        return
    with process_lock("commit-agent") as held:
        if not held:
            log("another commit-agent run is in progress")
            return
        run_id = now().strftime("%Y%m%dT%H%M%SZ")
        results = []
        for cfg in config["repos"]:
            try:
                result = process_repo(cfg, int(config.get("quiet_minutes", 20)), float(config.get("evidence_hours", 48)))
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                result = {"repo": cfg["path"], "started": now().isoformat(), "commits": [], "skipped": [], "notes": [str(error)], "outcome": "error"}
            result["finished"] = now().isoformat()
            results.append(result)
            log(f"{cfg['path']}: {result['outcome']}")
        for i, result in enumerate(results):
            write_json(COMMIT_RUNS / f"{run_id}-{i}.json", result)


if __name__ == "__main__":
    main()
