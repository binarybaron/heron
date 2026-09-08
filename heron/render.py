#!/usr/bin/env python3
"""Render the heron as a static site: one page of topics, one page per
interaction.

Plain HTML, one stylesheet, a few lines of script for the tabs. Black text on
white, monospace, no images. Citations in a post link to the turn they cite
on the interaction's page, where the whole conversation or terminal is shown.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from heron.common import COMMIT_RUNS, INDEX, INTERACTIONS, SITE, SUMMARY, log, read_json

REF_RE = re.compile(r"\[\[([SP][0-9a-f]{8})#(\d+)\]\]")
BARE_REF_RE = re.compile(r"^([SP][0-9a-f]{8})#(\d+)$")

STYLE = """
html { background: #fff; color: #000; }
body { max-width: 88ch; margin: 0 auto; padding: 2rem 1rem 6rem; font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
a { color: #000; }
h1, h2, h3 { font-weight: 700; font-size: 1em; margin: 2rem 0 0.5rem; }
h1 { font-size: 1.15em; margin-top: 0; }
h1 small, .muted { font-weight: 400; color: #555; }
hr { border: 0; border-top: 1px solid #000; margin: 1.5rem 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0.6rem 0; padding: 0.6rem 0.8rem; border-left: 3px solid #000; background: #f6f6f6; }
code { background: #f0f0f0; padding: 0 0.2em; }
pre code { background: none; padding: 0; }
nav.tabs { display: flex; flex-wrap: wrap; gap: 0 1.2rem; margin: 1rem 0; padding: 0.5rem 0; border-top: 1px solid #000; border-bottom: 1px solid #000; }
nav.tabs a { text-decoration: none; }
nav.tabs a.current { font-weight: 700; text-decoration: underline; }
section.topic { display: none; }
section.topic.current { display: block; }
.status { display: inline-block; border: 1px solid #000; padding: 0 0.4em; margin-left: 0.5em; font-size: 0.85em; }
.bar { margin: 0.8rem 0 0.3rem; letter-spacing: 0.05em; }
ul.milestones { list-style: none; padding: 0; margin: 0 0 1rem; }
ul.milestones li { margin: 0.15rem 0; }
ul.milestones .done { color: #555; text-decoration: line-through; }
a.ref { text-decoration: none; border-bottom: 1px dotted #000; color: #333; font-size: 0.9em; }
a.ref:hover { background: #eee; }
a.ref::before { content: "↗ "; }
.turn { margin: 0.9rem 0; padding-left: 0.8rem; border-left: 3px solid #ddd; }
.turn:target { border-left-color: #000; background: #f4f4f4; }
.turn .who { font-weight: 700; }
.turn .n { color: #777; margin-right: 0.5em; }
.turn.user { border-left-color: #000; }
.turn pre { margin: 0.3rem 0; background: none; border: 0; padding: 0; }
table { border-collapse: collapse; }
td, th { text-align: left; padding: 0.1rem 1rem 0.1rem 0; vertical-align: top; }
footer { margin-top: 3rem; color: #555; }
@media (max-width: 40rem) { body { padding: 1rem 0.7rem 4rem; font-size: 14px; } }
"""

SCRIPT = """
(function () {
  var links = document.querySelectorAll('nav.tabs a[data-tab]');
  var sections = document.querySelectorAll('section.topic');
  function show(slug) {
    var found = false;
    sections.forEach(function (s) { var on = s.id === 'topic-' + slug; s.classList.toggle('current', on); found = found || on; });
    links.forEach(function (l) { l.classList.toggle('current', l.dataset.tab === slug); });
    return found;
  }
  function fromHash() {
    var slug = location.hash.replace(/^#/, '');
    if (!slug || !show(slug)) { if (links.length) show(links[0].dataset.tab); }
  }
  links.forEach(function (l) { l.addEventListener('click', function (e) { e.preventDefault(); history.replaceState(null, '', '#' + l.dataset.tab); fromHash(); window.scrollTo(0, 0); }); });
  window.addEventListener('hashchange', fromHash);
  fromHash();
})();
"""


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def turn_snippet(turns: dict[tuple[str, int], dict[str, Any]], sid: str, n: int) -> str:
    turn = turns.get((sid, n))
    if turn is None:
        return f"{sid}#{n}"
    text = " ".join(turn["text"].split())
    return text[:100] + ("…" if len(text) > 100 else "")


def link_ref(turns: dict[tuple[str, int], dict[str, Any]], sid: str, n: int, *, base: str = "") -> str:
    snippet = turn_snippet(turns, sid, n)
    title = f"{sid} #{n}"
    return f'<a class="ref" href="{base}sessions/{sid}.html#t-{n}" title="{esc(title)}">{esc(snippet)}</a>'


def inline(text: str, turns: dict[tuple[str, int], dict[str, Any]]) -> str:
    """Inline Markdown: code spans, bold, links, citations."""
    out: list[str] = []
    pos = 0
    for match in re.finditer(r"`([^`]+)`", text):
        out.append(inline_plain(text[pos : match.start()], turns))
        out.append(f"<code>{esc(match[1])}</code>")
        pos = match.end()
    out.append(inline_plain(text[pos:], turns))
    return "".join(out)


def inline_plain(text: str, turns: dict[tuple[str, int], dict[str, Any]]) -> str:
    parts: list[str] = []
    pos = 0
    for match in REF_RE.finditer(text):
        parts.append(esc_inline(text[pos : match.start()]))
        parts.append(link_ref(turns, match[1], int(match[2])))
        pos = match.end()
    parts.append(esc_inline(text[pos:]))
    return "".join(parts)


def esc_inline(text: str) -> str:
    escaped = esc(text)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def markdown(text: str, turns: dict[tuple[str, int], dict[str, Any]]) -> str:
    """The subset of Markdown the summarizer is asked to use."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + inline(" ".join(paragraph), turns) + "</p>")
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
            out.append(f"<h3>{inline(heading[2], turns)}</h3>")
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
            out.append(f"<{tag}>" + "".join(f"<li>{inline(item, turns)}</li>" for item in items) + f"</{tag}>")
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        paragraph.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


def progress_bar(milestones: list[dict[str, Any]]) -> str:
    total = len(milestones)
    done = sum(1 for m in milestones if m.get("done"))
    width = 30
    filled = round(width * done / total) if total else 0
    bar = "#" * filled + "-" * (width - filled)
    return f'<div class="bar">[{bar}] {done}/{total} milestones</div>'


def milestone_list(milestones: list[dict[str, Any]], turns: dict[tuple[str, int], dict[str, Any]]) -> str:
    items = []
    for m in milestones:
        mark = "[x]" if m.get("done") else "[ ]"
        ref = ""
        match = BARE_REF_RE.match(str(m.get("ref", "")).strip())
        if match:
            ref = " " + link_ref(turns, match[1], int(match[2]))
        items.append(f'<li class="{"done" if m.get("done") else "open"}">{mark} {esc(m.get("title", ""))}{ref}</li>')
    return '<ul class="milestones">' + "".join(items) + "</ul>"


def page(title: str, body: str, *, depth: int = 0) -> str:
    prefix = "../" * depth
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"{prefix}style.css\"></head>"
        f"<body>{body}</body></html>\n"
    )


def render_index(summary: dict[str, Any], index: dict[str, Any], turns: dict, commit_runs: list[dict[str, Any]]) -> str:
    topics = summary.get("topics", [])
    generated = str(summary.get("generated_at", ""))[:16].replace("T", " ")
    stale = summary.get("stale_reason")
    nav = "".join(
        f'<a href="#{esc(t["slug"])}" data-tab="{esc(t["slug"])}">{esc(t["title"])}</a>' for t in topics
    )
    nav += '<a href="#commits" data-tab="commits">commit agent</a><a href="#sessions" data-tab="sessions">sessions</a>'
    sections = []
    for t in topics:
        sections.append(
            f'<section class="topic" id="topic-{esc(t["slug"])}">'
            f'<h2>{esc(t["title"])}<span class="status">{esc(t["status"])}</span></h2>'
            f'<p class="muted">{esc(t.get("one_liner", ""))}</p>'
            + progress_bar(t.get("milestones", []))
            + milestone_list(t.get("milestones", []), turns)
            + "<hr>"
            + markdown(t.get("body_markdown", ""), turns)
            + "</section>"
        )
    sections.append('<section class="topic" id="topic-commits"><h2>commit agent</h2>' + render_commit_runs(commit_runs, turns) + "</section>")
    rows = "".join(
        f'<tr><td>{esc((e.get("ended") or "")[:16].replace("T", " "))}</td><td>{esc(e.get("host") or "?")}</td><td>{esc(e["source"])}</td>'
        f'<td><a href="sessions/{esc(e["id"])}.html">{esc(e["id"])}</a></td><td>{esc(e["title"])}</td><td>{e["turns"]}</td></tr>'
        for e in index.get("sessions", [])
    )
    sections.append(
        '<section class="topic" id="topic-sessions"><h2>sessions</h2>'
        f'<p class="muted">Every interaction in the last {int(index.get("window_hours", 0))} hours, newest first.</p>'
        f"<table><tr><th>last activity</th><th>host</th><th>source</th><th>id</th><th>first line</th><th>turns</th></tr>{rows}</table></section>"
    )
    # The hosts named in the index; the ones collect saw plus any a PR run added.
    hosts = sorted({str(h) for h in index.get("hosts", [])} | {str(e["host"]) for e in index.get("sessions", []) if e.get("host")})
    head = (
        f"<h1>heron <small>· {esc(', '.join(hosts) or 'no hosts')} · updated {esc(generated)} UTC</small></h1>"
        f"<p>{esc(summary.get('headline', ''))}</p>"
    )
    if stale:
        head += f'<p class="muted">The summary could not be refreshed since {esc(str(summary.get("stale_since", ""))[:16])}: {esc(stale)}</p>'
    body = head + f'<nav class="tabs">{nav}</nav>' + "\n".join(sections) + f"<script>{SCRIPT}</script>"
    return page("heron", body)


def render_commit_runs(runs: list[dict[str, Any]], turns: dict) -> str:
    if not runs:
        return "<p class=\"muted\">No runs yet.</p>"
    out = ["<p class=\"muted\">The commit agent runs hourly. It commits only files whose tests were seen passing in a transcript after their last edit, re-runs the checks, and pushes without force.</p>"]
    for run in runs[:30]:
        out.append(f'<h3>{esc(str(run.get("started", ""))[:16].replace("T", " "))} · {esc(run.get("repo", ""))} · {esc(run.get("outcome", ""))}</h3>')
        for commit in run.get("commits", []):
            refs = " ".join(
                link_ref(turns, m[1], int(m[2])) for m in (BARE_REF_RE.match(r) for r in commit.get("evidence", [])) if m
            )
            out.append(
                f'<p><b>{esc(commit.get("sha", "")[:9])}</b> {esc(commit.get("subject", ""))}<br>'
                f'<span class="muted">{esc(", ".join(commit.get("files", [])))}</span><br>{refs}</p>'
            )
        skipped = run.get("skipped", [])
        if skipped:
            out.append("<ul>" + "".join(f"<li>{esc(s.get('file', ''))}: {esc(s.get('reason', ''))}</li>" for s in skipped) + "</ul>")
        if run.get("notes"):
            out.append("<pre>" + esc("\n".join(run["notes"])) + "</pre>")
    return "\n".join(out)


def render_session(record: dict[str, Any]) -> str:
    meta = record.get("meta", {})
    head = (
        f'<p><a href="../index.html">← heron</a></p>'
        f'<h1>{esc(record["id"])} <small>· {esc(record["source"])} on {esc(record.get("host") or "?")} · {esc(str(record.get("started") or "")[:16].replace("T", " "))}'
        f' → {esc(str(record.get("ended") or "")[:16].replace("T", " "))} · cwd {esc(meta.get("cwd") or "?")}</small></h1>'
        f'<p class="muted">{esc(record.get("host") or "?")}: {esc(record["path"])}'
        + (f' — <a href="{esc(meta["url"])}">open on GitHub</a>' if meta.get("url") else "")
        + "</p><hr>"
    )
    turns = []
    for turn in record["turns"]:
        who = {"user": "user", "assistant": "agent", "tool": "result", "terminal": "terminal", "pr": "pull request"}.get(turn["role"], turn["role"])
        if turn["kind"] == "tool_use":
            who = f"agent → {turn.get('tool', 'tool')}"
        when = (turn.get("time") or "")[11:16]
        turns.append(
            f'<div class="turn {esc(turn["role"])}" id="t-{turn["n"]}">'
            f'<span class="n"><a href="#t-{turn["n"]}">#{turn["n"]}</a></span><span class="who">{esc(who)}</span> <span class="muted">{esc(when)}</span>'
            f'<pre>{esc(turn["text"])}</pre></div>'
        )
    return page(f"{record['id']} · heron", head + "\n".join(turns), depth=1)


def main() -> None:
    summary = read_json(SUMMARY, {"headline": "No summary has been generated yet.", "topics": []})
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

    # Written in place: the directory is bind-mounted into the container
    # that serves it, so it must keep its inode. Stale session pages go last.
    sessions_dir = SITE / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (SITE / "style.css").write_text(STYLE)
    tmp = SITE / "index.html.tmp"
    tmp.write_text(render_index(summary, index, turns, runs))
    tmp.replace(SITE / "index.html")
    keep = set()
    for record in records:
        target = sessions_dir / f"{record['id']}.html"
        keep.add(target.name)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(render_session(record))
        tmp.replace(target)
    for stale in sessions_dir.glob("*.html"):
        if stale.name not in keep:
            stale.unlink()
    log(f"rendered {len(records)} interaction pages and {len(summary.get('topics', []))} topics into {SITE}")


if __name__ == "__main__":
    main()
