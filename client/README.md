# heron-agent

The Heron client. It runs in the background on every machine you work from and
ships new bytes of your Claude Code and Codex transcripts, and recordings of your
interactive shells, to one Heron server. See `../DESIGN.md` for the whole system.

## Install

1. Get the binary from the latest GitHub release:
   - Linux: `heron-agent-x86_64-unknown-linux-musl` (static, runs anywhere)
   - macOS: `Heron-aarch64-apple-darwin.app.zip` (Apple silicon) or
     `Heron-x86_64-apple-darwin.app.zip` (Intel). The plain
     `heron-agent-<target>` binaries work on macOS too.
2. Put it somewhere permanent; the background service points at that path.

       # Linux
       install -m 755 heron-agent-x86_64-unknown-linux-musl ~/.local/bin/heron-agent

       # macOS
       unzip Heron-aarch64-apple-darwin.app.zip -d /Applications
       xattr -dr com.apple.quarantine /Applications/Heron.app   # unsigned download
       alias heron-agent=/Applications/Heron.app/Contents/MacOS/heron-agent

3. Register it. This writes `~/.config/heron/agent.toml`, installs the shell
   hook, and starts the agent at login (launchd on macOS, a systemd user unit
   on Linux):

       heron-agent install --server https://example.net/heron --token <ingest_token>

   Add `--host NAME` if the machine's hostname is not what you want to see on
   the site.

4. Open a new terminal. Shells from now on are recorded; transcripts start
   shipping right away.

Check on it with `heron-agent status`. On macOS, double-clicking `Heron.app`
also starts the agent (it has no Dock icon); a second copy notices the first
and exits.

## What gets shipped

Only new bytes, only from these places:

| kind       | files                                    | path sent to the server            |
|------------|------------------------------------------|------------------------------------|
| `claude`   | `~/.claude/projects/**/*.jsonl`          | relative to `~/.claude/projects`   |
| `codex`    | `~/.codex/sessions/**/*.jsonl`           | relative to `~/.codex/sessions`    |
| `terminal` | `~/.local/share/heron/terminals/*.log`   | the file name                      |

Every `poll_seconds` (default 30) the agent compares each file's size with
what it last shipped and POSTs the difference to `<server>/api/v1/upload`.
If the server's copy is out of step it answers `409` with its size and the
agent resends from there; a file that shrank (rotated) is resent from the
start with `truncate: true`. Uploads are cut into 4 MiB pieces.

Files the agent has never shipped and whose mtime is older than
`max_age_days` (default 14) are ignored for good: they are history, not
work in progress.

The terminal recordings are made by `script(1)`, started from a snippet that
`heron-agent shell-hook` prints and `install` sources from `~/.bashrc` and
`~/.zshrc`. It skips shells with no tty, shells already inside a recording,
and shells started with `HERON_NO_RECORD=1`. Transcripts and recordings hold
secrets: ship them only to a server you control.

## Files

    ~/.config/heron/agent.toml              server, token, host, poll_seconds, max_age_days
    ~/.config/heron/shell-hook.sh           the snippet sourced by ~/.bashrc and ~/.zshrc
    ~/.local/state/heron/shipped.json       per file: offset shipped, size and mtime last seen
    ~/.local/state/heron/agent.lock         held by the running agent
    ~/.local/share/heron/terminals/         the recordings
    ~/Library/LaunchAgents/net.heron.agent.plist   (macOS) logs to ~/Library/Logs/heron-agent.log
    ~/.config/systemd/user/heron-agent.service     (Linux) logs: journalctl --user -u heron-agent

## Commands

    heron-agent run                    poll forever (what the service runs)
    heron-agent once                   one scan, then exit; exit code 1 if anything failed
    heron-agent status                 config path, server, host, files tracked, bytes shipped
    heron-agent shell-hook [--install] print the snippet, or install it
    heron-agent install --server URL --token TOKEN [--host NAME]
    heron-agent uninstall

`run` and `once` accept `--server`, `--token` and `--host` to override the
config file.

## Environment overrides

    HERON_AGENT_CONFIG     path of agent.toml
    HERON_CLAUDE_DIR       instead of ~/.claude/projects
    HERON_CODEX_DIR        instead of ~/.codex/sessions
    HERON_TERMINALS_DIR    instead of ~/.local/share/heron/terminals (the shell hook honours it too)
    HERON_STATE_DIR        instead of ~/.local/state/heron
    HERON_NO_RECORD=1      do not record this shell

The tests use the first five to work in temporary directories.

## Stop it

`heron-agent uninstall` stops and removes the service, takes the hook out of
`~/.bashrc` and `~/.zshrc`, and deletes `agent.toml`. It keeps `shipped.json`
so a later install does not resend what the server already has; delete it
if you want a clean slate. Already open shells keep recording until you
close them.

To pause without uninstalling: `systemctl --user stop heron-agent` (Linux)
or `launchctl unload ~/Library/LaunchAgents/net.heron.agent.plist` (macOS).

## Build

    cd client && cargo build --release      # target/release/heron-agent
    cargo test

Releases are built by `.github/workflows/release.yml` on every push to
`master`: `heron-agent-<target>` for Linux (musl) and both macOS targets,
plus `Heron-<target>.app.zip`, published as release `v0.<run number>`.
