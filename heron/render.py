#!/usr/bin/env python3
"""Render the site: an index of posts, one article per topic, one page per
interaction, and pages for the commit agent's runs and the session list.

Plain HTML and one stylesheet. Black text on white, monospace, no images.
Citations in a post link to the turn they cite on the interaction's page,
where the whole conversation or terminal is shown.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from heron.common import COMMIT_RUNS, INDEX, INTERACTIONS, SITE, SUMMARY, log, read_json, redact

REF_RE = re.compile(r"\[\[([SP][0-9a-f]{8})#(\d+)\]\]")
BARE_REF_RE = re.compile(r"^([SP][0-9a-f]{8})#(\d+)$")

STYLE = """
html { background: #fff; color: #000; }
body { max-width: 76ch; margin: 0 auto; padding: 2.5rem 1.2rem 6rem; font: 15.5px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
a { color: #000; }
header.site { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem 1.4rem; padding-bottom: 0.8rem; border-bottom: 2px solid #000; margin-bottom: 1.6rem; }
header.site .name { font-weight: 700; font-size: 1.3em; text-decoration: none; letter-spacing: 0.04em; }
header.site nav a { margin-right: 1rem; }
.muted { color: #555; }
h1 { font-size: 1.55em; line-height: 1.25; margin: 0 0 0.4rem; }
h2 { font-size: 1.1em; margin: 2rem 0 0.4rem; }
h3 { font-size: 1em; margin: 1.6rem 0 0.4rem; }
p.lede { font-size: 1.05em; margin: 0 0 2rem; }
article.entry { padding: 1.1rem 0; border-bottom: 1px solid #ccc; }
article.entry h2 { margin: 0 0 0.25rem; font-size: 1.15em; }
article.entry h2 a { text-decoration: none; }
article.entry h2 a:hover { text-decoration: underline; }
article.entry .meta { margin: 0.3rem 0 0; }
.status { display: inline-block; border: 1px solid #000; padding: 0 0.45em; font-size: 0.8em; vertical-align: middle; margin-left: 0.4em; }
.status.shipped { background: #000; color: #fff; }
.status.blocked { border-style: dashed; }
.bar { letter-spacing: 0.05em; white-space: nowrap; }
.bar .fill { font-weight: 700; }
.post .bar { margin: 1.2rem 0 0.4rem; }
ul.milestones { list-style: none; padding: 0; margin: 0 0 1.6rem; }
ul.milestones li { margin: 0.2rem 0; }
ul.milestones .done { color: #555; text-decoration: line-through; }
.post hr { border: 0; border-top: 1px solid #000; margin: 1.6rem 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0.8rem 0; padding: 0.7rem 0.9rem; border-left: 3px solid #000; background: #f6f6f6; }
code { background: #f0f0f0; padding: 0 0.2em; }
pre code { background: none; padding: 0; }
a.ref { text-decoration: none; border-bottom: 1px dotted #000; color: #333; font-size: 0.88em; }
a.ref:hover { background: #eee; }
a.ref::before { content: "↗ "; }
.turn { margin: 1rem 0; padding-left: 0.8rem; border-left: 3px solid #ddd; }
.turn:target { border-left-color: #000; background: #f4f4f4; }
.turn .who { font-weight: 700; }
.turn .n { color: #777; margin-right: 0.5em; }
.turn.user { border-left-color: #000; }
.turn pre { margin: 0.3rem 0; background: none; border: 0; padding: 0; }
details.fold summary { cursor: pointer; list-style: none; }
details.fold summary::-webkit-details-marker { display: none; }
details.fold summary::before { content: "▸ "; color: #777; }
details.fold[open] summary::before { content: "▾ "; }
details.fold summary .muted { font-size: 0.9em; }
.fold-all { margin: 0 0 1rem; }
.fold-all a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; }
td, th { text-align: left; padding: 0.2rem 1rem 0.2rem 0; vertical-align: top; border-bottom: 1px solid #eee; }
footer.site { margin-top: 3rem; padding-top: 0.8rem; border-top: 1px solid #000; color: #555; }
@media (max-width: 40rem) { body { padding: 1.2rem 0.8rem 4rem; font-size: 14.5px; } }
"""


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def when(value: Any, length: int = 16) -> str:
    return str(value or "")[:length].replace("T", " ")


def turn_snippet(turns: dict[tuple[str, int], dict[str, Any]], sid: str, n: int) -> str:
    turn = turns.get((sid, n))
    if turn is None:
        return f"{sid}#{n}"
    text = " ".join(redact(turn["text"]).split())
    return text[:100] + ("…" if len(text) > 100 else "")


def link_ref(turns: dict[tuple[str, int], dict[str, Any]], sid: str, n: int, *, base: str = "") -> str:
    snippet = turn_snippet(turns, sid, n)
    return f'<a class="ref" href="{base}sessions/{sid}.html#t-{n}" title="{esc(f"{sid} #{n}")}">{esc(snippet)}</a>'


def inline(text: str, turns: dict, base: str) -> str:
    """Inline Markdown: code spans, bold, links, citations."""
    out: list[str] = []
    pos = 0
    for match in re.finditer(r"`([^`]+)`", text):
        out.append(inline_plain(text[pos : match.start()], turns, base))
        out.append(f"<code>{esc(match[1])}</code>")
        pos = match.end()
    out.append(inline_plain(text[pos:], turns, base))
    return "".join(out)


def inline_plain(text: str, turns: dict, base: str) -> str:
    parts: list[str] = []
    pos = 0
    for match in REF_RE.finditer(text):
        parts.append(esc_inline(text[pos : match.start()]))
        parts.append(link_ref(turns, match[1], int(match[2]), base=base))
        pos = match.end()
    parts.append(esc_inline(text[pos:]))
    return "".join(parts)


def esc_inline(text: str) -> str:
    escaped = esc(text)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def markdown(text: str, turns: dict, base: str) -> str:
    """The subset of Markdown the summarizer is asked to use."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + inline(" ".join(paragraph), turns, base) + "</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            flush()
            out.append(f"<h3>{inline(heading[2], turns, base)}</h3>")
            i += 1
            continue
        if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            flush()
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items: list[str] = []
            while i < len(lines) and (re.match(r"^\s*[-*]\s+", lines[i]) or re.match(r"^\s*\d+\.\s+", lines[i])):
                items.append(re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(item, turns, base)}</li>" for item in items) + f"</{tag}>")
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        paragraph.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


def progress_bar(milestones: list[dict[str, Any]], width: int = 30) -> str:
    total = len(milestones)
    done = sum(1 for m in milestones if m.get("done"))
    filled = round(width * done / total) if total else 0
    return (
        f'<span class="bar">[<span class="fill">{"#" * filled}</span>{"-" * (width - filled)}]'
        f" {done}/{total}</span>"
    )


def milestone_list(milestones: list[dict[str, Any]], turns: dict, base: str) -> str:
    items = []
    for m in milestones:
        mark = "[x]" if m.get("done") else "[ ]"
        ref = ""
        match = BARE_REF_RE.match(str(m.get("ref", "")).strip())
        if match:
            ref = " " + link_ref(turns, match[1], int(match[2]), base=base)
        items.append(f'<li class="{"done" if m.get("done") else "open"}">{mark} {esc(m.get("title", ""))}{ref}</li>')
    return '<ul class="milestones">' + "".join(items) + "</ul>"


def status_badge(status: str) -> str:
    return f'<span class="status {esc(status)}">{esc(status)}</span>'


def page(title: str, body: str, *, base: str, updated: str, hosts: list[str]) -> str:
    header = (
        f'<header class="site"><a class="name" href="{base}index.html">heron</a>'
        f'<nav><a href="{base}index.html">posts</a><a href="{base}commits.html">commit agent</a><a href="{base}sessions.html">sessions</a></nav>'
        f'<span class="muted">{esc(" · ".join(hosts))}</span></header>'
    )
    footer = f'<footer class="site">updated {esc(updated)} UTC · written from the transcripts by heron</footer>'
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"{base}style.css\"></head>"
        f"<body>{header}{body}{footer}</body></html>\n"
    )


def render_index(summary: dict[str, Any], ctx: dict[str, Any]) -> str:
    topics = summary.get("topics", [])
    body = f'<h1>What is being worked on</h1><p class="lede">{esc(summary.get("headline", ""))}</p>'
    if summary.get("stale_reason"):
        body += f'<p class="muted">The summary could not be refreshed since {esc(when(summary.get("stale_since")))}: {esc(summary["stale_reason"])}</p>'
    if not topics:
        body += '<p class="muted">No summary has been generated yet.</p>'
    for t in topics:
        milestones = t.get("milestones", [])
        body += (
            f'<article class="entry"><h2><a href="posts/{esc(t["slug"])}.html">{esc(t["title"])}</a>{status_badge(t["status"])}</h2>'
            f'<p class="meta">{esc(t.get("one_liner", ""))}</p>'
            f'<p class="meta muted">{progress_bar(milestones, 20)} milestones · <a href="posts/{esc(t["slug"])}.html">read</a></p></article>'
        )
    return page("heron", body, base="", **ctx)


def render_post(t: dict[str, Any], ctx: dict[str, Any], turns: dict) -> str:
    base = "../"
    milestones = t.get("milestones", [])
    body = (
        f'<article class="post"><p class="muted"><a href="../index.html">← all posts</a></p>'
        f'<h1>{esc(t["title"])}{status_badge(t["status"])}</h1>'
        f'<p class="lede">{esc(t.get("one_liner", ""))}</p>'
        f'<div class="bar-row">{progress_bar(milestones)} milestones</div>'
        + milestone_list(milestones, turns, base)
        + "<hr>"
        + markdown(t.get("body_markdown", ""), turns, base)
        + "</article>"
    )
    return page(f'{t["title"]} · heron', body, base=base, **ctx)


def render_commits(runs: list[dict[str, Any]], ctx: dict[str, Any], turns: dict) -> str:
    body = ["<h1>Commit agent</h1>", '<p class="lede">Runs hourly. Commits only files whose checks were seen passing in a transcript after their last edit, re-runs the checks, and pushes without force.</p>']
    if not runs:
        body.append('<p class="muted">No runs yet.</p>')
    for run in runs[:40]:
        body.append(f'<h2>{esc(when(run.get("started")))} · {esc(run.get("repo", ""))} · {esc(run.get("outcome", ""))}</h2>')
        for commit in run.get("commits", []):
            refs = " ".join(
                link_ref(turns, m[1], int(m[2])) for m in (BARE_REF_RE.match(r) for r in commit.get("evidence", [])) if m
            )
            body.append(
                f'<p><b>{esc(str(commit.get("sha", ""))[:9])}</b> {esc(commit.get("subject", ""))}<br>'
                f'<span class="muted">{esc(", ".join(commit.get("files", [])))}</span><br>{refs}</p>'
            )
        skipped = run.get("skipped", [])
        if skipped:
            body.append("<ul>" + "".join(f"<li>{esc(s.get('file', ''))}: {esc(s.get('reason', ''))}</li>" for s in skipped) + "</ul>")
        if run.get("notes"):
            body.append("<pre>" + esc("\n".join(run["notes"])) + "</pre>")
    return page("commit agent · heron", "\n".join(body), base="", **ctx)


def render_sessions(index: dict[str, Any], ctx: dict[str, Any]) -> str:
    rows = "".join(
        f'<tr><td>{esc(when(e.get("ended")))}</td><td>{esc(e.get("host", ""))}</td><td>{esc(e["source"])}</td>'
        f'<td><a href="sessions/{esc(e["id"])}.html">{esc(e["id"])}</a></td><td>{esc(e["title"])}</td><td>{e["turns"]}</td></tr>'
        for e in index.get("sessions", [])
    )
    body = (
        "<h1>Sessions</h1>"
        f'<p class="lede">Every interaction in the last {int(index.get("window_hours", 0) or 0)} hours, newest first.</p>'
        f"<table><tr><th>last activity</th><th>host</th><th>source</th><th>id</th><th>first line</th><th>turns</th></tr>{rows}</table>"
    )
    return page("sessions · heron", body, base="", **ctx)


def render_session(record: dict[str, Any], ctx: dict[str, Any]) -> str:
    meta = record.get("meta", {})
    head = (
        f'<p class="muted"><a href="../sessions.html">← sessions</a></p>'
        f'<h1>{esc(record["id"])}</h1>'
        f'<p class="muted">{esc(record["source"])} on {esc(record.get("host", "?"))} · {esc(when(record.get("started")))} → {esc(when(record.get("ended")))}'
        + (f' · cwd {esc(meta["cwd"])}' if meta.get("cwd") else "")
        + f'<br>{esc(record["path"])}'
        + (f' — <a href="{esc(meta["url"])}">open on GitHub</a>' if meta.get("url") else "")
        + "</p>"
        + '<p class="fold-all"><a href="#" data-fold="open">expand tool calls</a><a href="#" data-fold="close">collapse tool calls</a></p><hr>'
    )
    # Tool calls and their results are folded: the conversation reads as
    # prompts and answers, and a citation into a folded turn opens it.
    parts: list[str] = []
    for turn in record["turns"]:
        who = {"user": "user", "assistant": "agent", "tool": "result", "terminal": "terminal", "pr": "pull request"}.get(turn["role"], turn["role"])
        if turn["kind"] == "tool_use":
            who = f"agent → {turn.get('tool', 'tool')}"
        text = redact(turn["text"])
        stamp = esc((turn.get("time") or "")[11:16])
        head_line = f'<span class="n"><a href="#t-{turn["n"]}">#{turn["n"]}</a></span><span class="who">{esc(who)}</span> <span class="muted">{stamp}</span>'
        if turn["kind"] in {"tool_use", "tool_result"}:
            first = " ".join(text.split())[:120]
            parts.append(
                f'<details class="turn fold {esc(turn["role"])}" id="t-{turn["n"]}">'
                f'<summary>{head_line} <span class="muted">{esc(first)}</span></summary>'
                f"<pre>{esc(text)}</pre></details>"
            )
            continue
        parts.append(f'<div class="turn {esc(turn["role"])}" id="t-{turn["n"]}">{head_line}<pre>{esc(text)}</pre></div>')
    turns = parts
    script = """<script>
(function () {
  function openTarget() { var t = location.hash && document.querySelector(location.hash); if (t && t.tagName === 'DETAILS') { t.open = true; t.scrollIntoView(); } }
  document.querySelectorAll('.fold-all a').forEach(function (a) { a.addEventListener('click', function (e) { e.preventDefault(); document.querySelectorAll('details.fold').forEach(function (d) { d.open = a.dataset.fold === 'open'; }); }); });
  window.addEventListener('hashchange', openTarget); openTarget();
})();
</script>"""
    return page(f"{record['id']} · heron", head + "\n".join(turns) + script, base="../", **ctx)


def main() -> None:
    summary = read_json(SUMMARY, {"headline": "", "topics": []})
    index = read_json(INDEX, {"sessions": []})
    turns: dict[tuple[str, int], dict[str, Any]] = {}
    records = []
    for entry in index.get("sessions", []):
        record = read_json(INTERACTIONS / f"{entry['id']}.json")
        if not record:
            continue
        records.append(record)
        for turn in record["turns"]:
            turns[(record["id"], turn["n"])] = turn
    runs = sorted(
        (r for r in (read_json(p) for p in COMMIT_RUNS.glob("*.json")) if r),
        key=lambda r: str(r.get("started", "")),
        reverse=True,
    )
    hosts = sorted({str(e.get("host")) for e in index.get("sessions", []) if e.get("host")}) or list(index.get("hosts", []))
    ctx = {"updated": when(summary.get("generated_at") or index.get("generated_at")), "hosts": hosts}

    # Written in place: the directory may be bind-mounted into a container,
    # so it must keep its inode. Stale pages are removed last.
    posts_dir = SITE / "posts"
    sessions_dir = SITE / "sessions"
    posts_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (SITE / "style.css").write_text(STYLE)
    write(SITE / "index.html", render_index(summary, ctx))
    write(SITE / "commits.html", render_commits(runs, ctx, turns))
    write(SITE / "sessions.html", render_sessions(index, ctx))
    keep_posts = set()
    for t in summary.get("topics", []):
        name = f"{t['slug']}.html"
        keep_posts.add(name)
        write(posts_dir / name, render_post(t, ctx, turns))
    for stale in posts_dir.glob("*.html"):
        if stale.name not in keep_posts:
            stale.unlink()
    keep = set()
    for record in records:
        name = f"{record['id']}.html"
        keep.add(name)
        write(sessions_dir / name, render_session(record, ctx))
    for stale in sessions_dir.glob("*.html"):
        if stale.name not in keep:
            stale.unlink()
    log(f"rendered {len(summary.get('topics', []))} posts and {len(records)} interaction pages into {SITE}")


def write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


if __name__ == "__main__":
    main()
