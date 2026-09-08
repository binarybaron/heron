"""collect reads raw/<host>/… and tags every interaction with its host."""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

import support
from heron import collect, common


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = support.scratch()
        sys.argv = ["heron collect"]
        self.enterContext(mock.patch.object(collect, "log", lambda message: None))

    def test_env_wins_over_config_state(self) -> None:
        self.assertEqual(str(common.STATE), support.CONFIG["state"].replace("state-from-config", "state"))
        self.assertEqual(common.WINDOW_HOURS, support.CONFIG["window_hours"])

    def test_raw_tree(self) -> None:
        raw = self.state / "raw"
        claude = raw / "laptop" / "claude" / "projects" / "-Users-me" / "abc.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text(support.sample_claude_session())
        terminal = raw / "laptop" / "terminals" / "20260908T010500Z-4242.log"
        terminal.parent.mkdir(parents=True)
        terminal.write_text(support.sample_terminal_log())
        # A second host with an identically named file must get its own id.
        other = raw / "desk" / "claude" / "projects" / "-Users-me" / "abc.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(support.sample_claude_session())
        # Something that fell out of the window earlier is pruned.
        stale = common.INTERACTIONS / "S0badbad0.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")

        collect.main()

        index = json.loads(common.INDEX.read_text())
        self.assertEqual(sorted(index["hosts"]), ["desk", "laptop"])
        by_path = {(e["host"], e["path"]): e for e in index["sessions"]}
        self.assertIn(("laptop", "claude/projects/-Users-me/abc.jsonl"), by_path)
        self.assertIn(("desk", "claude/projects/-Users-me/abc.jsonl"), by_path)
        self.assertIn(("laptop", "terminals/20260908T010500Z-4242.log"), by_path)
        self.assertEqual(len(index["sessions"]), 3)
        ids = {e["id"] for e in index["sessions"]}
        self.assertEqual(len(ids), 3)
        self.assertEqual(
            by_path[("laptop", "claude/projects/-Users-me/abc.jsonl")]["id"],
            collect.interaction_id("laptop", "claude", "projects/-Users-me/abc.jsonl"),
        )
        self.assertFalse(stale.exists())

        session = by_path[("laptop", "claude/projects/-Users-me/abc.jsonl")]
        self.assertEqual(session["source"], "claude")
        self.assertEqual(session["title"], "Fix the failing parser test")
        self.assertEqual(session["cwd"], "/home/me/project")
        record = json.loads((common.INTERACTIONS / f"{session['id']}.json").read_text())
        self.assertEqual(record["host"], "laptop")
        kinds = [t["kind"] for t in record["turns"]]
        self.assertEqual(kinds, ["prompt", "text", "tool_use", "tool_result", "text"])
        self.assertEqual([t["n"] for t in record["turns"]], [1, 2, 3, 4, 5])
        self.assertEqual(record["turns"][2]["text"], "$ python3 -m unittest\n# Run tests")

        term = by_path[("laptop", "terminals/20260908T010500Z-4242.log")]
        self.assertEqual(term["source"], "terminal")
        self.assertEqual(term["started"], "2026-09-08T01:05:00Z")
        term_record = json.loads((common.INTERACTIONS / f"{term['id']}.json").read_text())
        self.assertEqual(term_record["host"], "laptop")
        self.assertTrue(any("cargo test" in t["text"] for t in term_record["turns"]))
        self.assertEqual(index["window_hours"], support.CONFIG["window_hours"])

    def test_empty_raw_tree(self) -> None:
        collect.main()
        index = json.loads(common.INDEX.read_text())
        self.assertEqual(index["sessions"], [])
        self.assertEqual(index["hosts"], [])


if __name__ == "__main__":
    unittest.main()
