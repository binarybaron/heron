# Heron — design

Heron watches the agent sessions (Claude Code, Codex) and the terminals on
every machine you work from, ships them to one server, and every two hours
writes up what is being worked on as a plain static site with citations
into the exact transcript turns. A separate commit agent on the server can
commit and push work that the transcripts show passing its tests.

Two programs:

- **heron-agent** (Rust, `client/`): one static binary per platform. Runs in
  the background on a laptop or server, tails the transcript files, ships
  new bytes to the server. Also installs the shell hook that records
  terminals with `script`, and registers itself with launchd / systemd so it
  starts at login. Double-clickable `Heron.app` on macOS is the same binary
  in a bundle.
- **heron server** (Python 3, `heron/`, stdlib only): receives uploads,
  stores raw files per host, normalizes them (`collect`), fetches the
  maintainers' pull requests (`prs`), asks Claude for the topics
  (`summarize`), renders the site (`render`), serves it (`serve`), and runs
  the commit agent (`commit`). `bin/heron <command>`.

## Data on the server

    $HERON_STATE (default /var/lib/heron)
      raw/<host>/claude/<path>       as uploaded, path relative to ~/.claude/projects
      raw/<host>/codex/<path>        relative to ~/.codex/sessions
      raw/<host>/terminals/<file>    the `script` recordings
      state/interactions/<id>.json   normalized; id = S<8 hex> (session) or P<8 hex> (pull request)
      state/index.json               list of interactions, newest first
      state/summary.json             the latest topics from Claude
      state/commit-runs/*.json       one per commit-agent run
      site/                          index.html, style.css, sessions/<id>.html

Interaction ids are hashes of `<host>/<kind>/<path>` so links stay stable.
Every interaction carries `host`.

## Upload protocol (v1)

All requests to `/api/v1/*` carry `Authorization: Bearer <ingest_token>`.
Bodies are JSON, at most 8 MiB.

`POST /api/v1/upload`

    {"host": "laptop", "kind": "claude" | "codex" | "terminal",
     "path": "-Users-me/abc.jsonl", "offset": 4096,
     "data": "<base64 of the bytes starting at offset>", "truncate": false}

- If `offset` equals the stored file's current size, the server appends and
  answers `200 {"size": <new size>}`.
- If `offset` is larger than the stored size, or smaller and `truncate` is
  false, the server answers `409 {"size": <stored size>}`; the client
  re-sends from that size (or from 0 with `truncate: true` if the source
  file shrank, i.e. was rotated).
- `truncate: true` replaces the stored file with `data`.
- `path` may not contain `..` or start with `/`; `host` and `kind` are
  `[A-Za-z0-9._-]+`.

`GET /api/v1/status` → `{"hosts": {"laptop": {"files": 12, "bytes": 123456, "last_upload": "…"}}}`

`GET /healthz` → `200 ok`, no auth.

The site (`/`, `/index.html`, `/style.css`, `/sessions/<id>.html`) is served
with HTTP Basic auth (`site_user` / `site_password` from the config).
Transcripts hold secrets; the site is never served without auth.

## Server config — /etc/heron/config.json (HERON_CONFIG overrides the path)

    {"bind": "127.0.0.1", "port": 8097, "base_path": "",   # "/heron" when mounted under a prefix
     "ingest_token": "…", "site_user": "…", "site_password": "…",
     "state": "/var/lib/heron",
     "window_hours": 48,
     "repos": [ {"path": "/root/core", "remote": "core-wasm", "branch": "master", "ignore": ["artifacts/"]} ],
     "quiet_minutes": 20, "evidence_hours": 48,
     "github": {"repos": ["eigenwallet/core"], "maintainers": ["binarybaron"],
                "exclude_branch_prefixes": ["codex/"], "exclude_labels": ["visual-swarm"],
                "token_files": {}, "days": 21, "limit": 60}}

## Agent config — ~/.config/heron/agent.toml

    server = "https://example.net/heron"   # the server's base URL
    token = "…"                            # ingest_token
    host = "laptop"                        # defaults to the hostname
    poll_seconds = 30

State: `~/.local/state/heron/shipped.json` — per `<kind>:<path>`: the offset
shipped and the file's size/mtime last seen. Terminal recordings written by
the shell hook: `~/.local/share/heron/terminals/<utc stamp>-<pid>.log`.

Sources the agent tails:

- Claude Code: `~/.claude/projects/**/*.jsonl`
- Codex: `~/.codex/sessions/**/*.jsonl`
- terminals: `~/.local/share/heron/terminals/*.log`

## Shell hook

`heron-agent shell-hook` prints a snippet for bash and zsh that wraps an
interactive shell in `script` writing to the terminals directory. It skips
shells that are already recorded (`HERON_RECORDING` set), have no tty, or
have `HERON_NO_RECORD=1`. util-linux `script` takes `-q -f -O <file>`; BSD
(macOS) `script` takes `-q -F <file>`; the snippet detects which.

## Timers

Server: `heron-server.service` (always on, `heron serve`), `heron-site.timer`
every 2 hours (`heron site`), `heron-commit.timer` hourly (`heron commit`).
Agent: `heron-agent run` under a launchd agent (macOS) or a systemd user
service (Linux), installed by `heron-agent install`.

## Releases

GitHub Actions on every push to `master`: build `heron-agent` for
x86_64-unknown-linux-musl, aarch64-apple-darwin, x86_64-apple-darwin, wrap
the macOS binaries in `Heron.app`, and publish a release
`v0.<run number>` with those assets.
