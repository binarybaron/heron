"""render writes the topic tabs and links citations to the turn they cite."""

from __future__ import annotations

import json
import re
import sys
import unittest
from unittest import mock

import os
os.environ["HERON_DIAGRAM_RENDERER"] = "none"
import support
from heron import collect, common, render


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = support.scratch()
        sys.argv = ["heron collect"]
        self.enterContext(mock.patch.object(collect, "log", lambda message: None))
        self.enterContext(mock.patch.object(render, "log", lambda message: None))
        claude = self.state / "raw" / "laptop" / "claude" / "-Users-me" / "abc.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text(support.sample_claude_session())
        collect.main()
        self.index = json.loads(common.INDEX.read_text())
        self.sid = self.index["sessions"][0]["id"]

    def test_site(self) -> None:
        common.write_json(common.SUMMARY, {
            "generated_at": "2026-09-08T02:00:00+00:00",
            "headline": "One parser fix.",
            "topics": [
                {"slug": "parser-fix", "title": "Parser fix", "status": "shipped", "one_liner": "The test passes.",
                 "milestones": [{"title": "Make the parser test pass", "done": True, "ref": f"{self.sid}#4"}],
                 "body_markdown": f"The agent ran the tests [[{self.sid}#3]] and they passed [[{self.sid}#4]].\n\n- `unittest` output quoted\n",
                 "diagrams": [{"title": "Parser flow", "mermaid": "flowchart LR\n  A[input] --> B[parser]", "caption": f"From [[{self.sid}#3]]."},
                              {"title": "Test run", "mermaid": "sequenceDiagram\n  agent->>tests: run", "caption": "The run."}]},
                {"slug": "other", "title": "Other work", "status": "parked", "one_liner": "", "milestones": [],
                 "body_markdown": "Fixed in eigenwallet/core-wasm@5edabdde4 and 950b9696a, see eigenwallet/core#1198."},
            ],
        })
        sys.argv = ["heron render"]
        render.main()

        index_html = (common.SITE / "index.html").read_text()
        self.assertIn('href="posts/parser-fix.html"', index_html)
        self.assertIn('href="posts/other.html"', index_html)
        self.assertIn('href="sessions.html"', index_html)
        self.assertIn("laptop", index_html)
        self.assertIn("background: #fff; color: #000", (common.SITE / "style.css").read_text())

        sessions_html = (common.SITE / "sessions.html").read_text()
        self.assertIn("<th>host</th>", sessions_html)
        self.assertIn("<td>laptop</td>", sessions_html)

        post_html = (common.SITE / "posts" / "parser-fix.html").read_text()
        self.assertIn("Parser fix", post_html)
        self.assertIn("1/1", post_html)
        self.assertIn("Figure 1.", post_html)
        self.assertIn("key=", post_html)
        self.assertIn("Figure 2.", post_html)
        self.assertIn("flowchart LR", post_html)
        links = re.findall(r'href="\.\./(sessions/S[0-9a-f]{8}\.html#t-\d+)"', post_html)
        self.assertIn(f"sessions/{self.sid}.html#t-3", links)
        self.assertIn(f"sessions/{self.sid}.html#t-4", links)
        other_html = (common.SITE / "posts" / "other.html").read_text()
        self.assertIn('href="https://github.com/eigenwallet/core-wasm/commit/5edabdde4"', other_html)
        self.assertIn('href="https://github.com/eigenwallet/core/pull/1198"', other_html)
        page = common.SITE / "sessions" / f"{self.sid}.html"
        self.assertTrue(page.is_file())
        page_html = page.read_text()
        self.assertIn('id="t-3"', page_html)
        self.assertIn('id="t-4"', page_html)
        self.assertIn("claude on laptop", page_html)
        self.assertIn("Ran 3 tests", page_html)

    def test_stale_session_pages_removed(self) -> None:
        sessions = common.SITE / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "S0badbad0.html").write_text("old")
        sys.argv = ["heron render"]
        render.main()
        self.assertFalse((sessions / "S0badbad0.html").exists())
        # Without a summary nothing cites the session, so it gets no page:
        # uncited transcripts stay private.
        self.assertFalse((sessions / f"{self.sid}.html").exists())
        self.assertIn("No summary has been generated yet.", (common.SITE / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
