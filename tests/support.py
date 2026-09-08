"""Test fixtures: point the heron package at a temporary state directory.

heron.common reads the config and the state path once, at import, so this
module must be imported before any heron module. The state directory is
shared by every test in the process; scratch() clears it between tests.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_DIR = Path(tempfile.mkdtemp(prefix="heron-test-"))
CONFIG = {
    "bind": "127.0.0.1",
    "port": 0,
    "ingest_token": "test-ingest-token",
    "site_user": "reader",
    "site_password": "test-site-password",
    "state": str(STATE_DIR / "state-from-config"),
    "window_hours": 24,
}
CONFIG_PATH = STATE_DIR / "config.json"
CONFIG_PATH.write_text(json.dumps(CONFIG))

# The env wins over the config's `state`; the tests use the env value and
# test_collect checks that the config value is what the env replaced.
os.environ["HERON_CONFIG"] = str(CONFIG_PATH)
os.environ["HERON_STATE"] = str(STATE_DIR / "state")
os.environ.pop("HERON_WINDOW_HOURS", None)
os.environ.pop("HERON_LOCAL", None)

import heron.common as common  # noqa: E402  (after the environment is set)


def scratch() -> Path:
    """An empty state directory for one test."""
    if common.STATE.exists():
        shutil.rmtree(common.STATE)
    common.STATE.mkdir(parents=True)
    return common.STATE


def claude_jsonl(entries: list[dict]) -> str:
    return "".join(json.dumps(entry) + "\n" for entry in entries)


def sample_claude_session() -> str:
    """A Claude Code transcript with one prompt, one tool call and its result."""
    base = {"cwd": "/home/me/project", "gitBranch": "main", "version": "1.0.0"}
    return claude_jsonl([
        {**base, "type": "user", "timestamp": "2026-09-08T01:00:00Z",
         "message": {"role": "user", "content": "Fix the failing parser test"}},
        {**base, "type": "assistant", "timestamp": "2026-09-08T01:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "I will run the tests first."},
             {"type": "tool_use", "id": "tu1", "name": "Bash",
              "input": {"command": "python3 -m unittest", "description": "Run tests"}},
         ]}},
        {**base, "type": "user", "timestamp": "2026-09-08T01:00:09Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1", "content": "Ran 3 tests\n\nOK"},
         ]}},
        {**base, "type": "assistant", "timestamp": "2026-09-08T01:00:20Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "The parser test passes now."},
         ]}},
    ])


def sample_terminal_log() -> str:
    return (
        "Script started on 2026-09-08 01:05:00+00:00\n"
        "me@laptop:~/project$ git status\n"
        "On branch main\nnothing to commit, working tree clean\n"
        "me@laptop:~/project$ cargo test\n"
        "test result: ok. 4 passed; 0 failed\n"
        "me@laptop:~/project$ exit\n"
    )
