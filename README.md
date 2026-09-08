# Heron

A heron stands in the water and watches. This one watches your agent
sessions (Claude Code, Codex) and your terminals on every machine you work
from, ships them to one server, and every two hours writes up what is being
worked on: one plain page, one tab per topic, a milestone bar per topic, and
every claim cited with a link into the exact transcript turn it came from.
The server can also commit and push the work the transcripts show passing
its tests.

Two programs, see [DESIGN.md](DESIGN.md) for the contract between them:

- `client/` — **heron-agent**, a Rust binary. Runs in the background on a
  laptop or a server, tails the transcript files, records interactive shells
  with `script`, ships new bytes. Releases for Linux and macOS are built by
  GitHub Actions on every push to `master`; the macOS zip is a
  double-clickable `Heron.app`.
- `heron/` — the server, Python 3 with no dependencies beyond the `claude`
  CLI it uses for summaries. `bin/heron serve` receives uploads and serves
  the site behind HTTP Basic auth; `bin/heron site` collects, fetches the
  maintainers' pull requests, summarizes and renders; `bin/heron commit`
  runs the commit agent.

## How the pieces fit

```mermaid
flowchart LR
    subgraph laptop["laptop (macOS or Linux)"]
        L1["~/.claude/projects/**.jsonl"]
        L2["~/.codex/sessions/**.jsonl"]
        L3["~/.local/share/heron/terminals/*.log"]
        LH["shell hook (script)"] --> L3
        LA["heron-agent run (Rust)"]
        L1 --> LA
        L2 --> LA
        L3 --> LA
    end

    subgraph box["dev server (Linux)"]
        B1["~/.claude/projects/**.jsonl"]
        B2["~/.codex/sessions/**.jsonl"]
        B3["~/.local/share/heron/terminals/*.log"]
        BH["shell hook (script)"] --> B3
        BA["heron-agent run (Rust)"]
        B1 --> BA
        B2 --> BA
        B3 --> BA
    end

    subgraph server["heron server (VM, Python)"]
        S["heron serve<br/>POST /api/v1/upload"]
        RAW[("raw/&lt;host&gt;/{claude,codex,terminals}")]
        C["collect"]
        P["prs (gh)"]
        I[("state/interactions/*.json")]
        SUM["summarize"]
        CL["claude -p --json-schema"]
        J[("state/summary.json")]
        R["render"]
        SITE[("site/*.html")]
        CA["commit agent (hourly)"]
        S --> RAW --> C --> I
        GH["GitHub pull requests<br/>by the maintainers"] --> P --> I
        I --> SUM --> CL --> J --> R --> SITE
        I --> CA
        J -. previous summary .-> SUM
        SITE --> S2["heron serve<br/>GET / (key cookie or Basic auth)"]
    end

    LA -- "bearer token, base64 chunks, offsets" --> S
    BA -- "same protocol" --> S
    CA -- "commit + push verified work" --> REPO["git remote"]
    S2 --> BR["your browser"]
```

Every two hours `heron site` runs the collect, prs, summarize and render
steps; the summarizer receives its previous output so the posts change
incrementally. GitHub renders this Mermaid diagram in place; outside GitHub, the same
source rendered remotely: [diagram on mermaid.ink](https://mermaid.ink/svg/Zmxvd2NoYXJ0IExSCiAgICBzdWJncmFwaCBsYXB0b3BbImxhcHRvcCAobWFjT1Mgb3IgTGludXgpIl0KICAgICAgICBMMVsifi8uY2xhdWRlL3Byb2plY3RzLyoqLmpzb25sIl0KICAgICAgICBMMlsifi8uY29kZXgvc2Vzc2lvbnMvKiouanNvbmwiXQogICAgICAgIEwzWyJ-Ly5sb2NhbC9zaGFyZS9oZXJvbi90ZXJtaW5hbHMvKi5sb2ciXQogICAgICAgIExIWyJzaGVsbCBob29rIChzY3JpcHQpIl0gLS0-IEwzCiAgICAgICAgTEFbImhlcm9uLWFnZW50IHJ1biAoUnVzdCkiXQogICAgICAgIEwxIC0tPiBMQQogICAgICAgIEwyIC0tPiBMQQogICAgICAgIEwzIC0tPiBMQQogICAgZW5kCgogICAgc3ViZ3JhcGggYm94WyJkZXYgc2VydmVyIChMaW51eCkiXQogICAgICAgIEIxWyJ-Ly5jbGF1ZGUvcHJvamVjdHMvKiouanNvbmwiXQogICAgICAgIEIyWyJ-Ly5jb2RleC9zZXNzaW9ucy8qKi5qc29ubCJdCiAgICAgICAgQjNbIn4vLmxvY2FsL3NoYXJlL2hlcm9uL3Rlcm1pbmFscy8qLmxvZyJdCiAgICAgICAgQkhbInNoZWxsIGhvb2sgKHNjcmlwdCkiXSAtLT4gQjMKICAgICAgICBCQVsiaGVyb24tYWdlbnQgcnVuIChSdXN0KSJdCiAgICAgICAgQjEgLS0-IEJBCiAgICAgICAgQjIgLS0-IEJBCiAgICAgICAgQjMgLS0-IEJBCiAgICBlbmQKCiAgICBzdWJncmFwaCBzZXJ2ZXJbImhlcm9uIHNlcnZlciAoVk0sIFB5dGhvbikiXQogICAgICAgIFNbImhlcm9uIHNlcnZlPGJyLz5QT1NUIC9hcGkvdjEvdXBsb2FkIl0KICAgICAgICBSQVdbKCJyYXcvJmx0O2hvc3QmZ3Q7L3tjbGF1ZGUsY29kZXgsdGVybWluYWxzfSIpXQogICAgICAgIENbImNvbGxlY3QiXQogICAgICAgIFBbInBycyAoZ2gpIl0KICAgICAgICBJWygic3RhdGUvaW50ZXJhY3Rpb25zLyouanNvbiIpXQogICAgICAgIFNVTVsic3VtbWFyaXplIl0KICAgICAgICBDTFsiY2xhdWRlIC1wIC0tanNvbi1zY2hlbWEiXQogICAgICAgIEpbKCJzdGF0ZS9zdW1tYXJ5Lmpzb24iKV0KICAgICAgICBSWyJyZW5kZXIiXQogICAgICAgIFNJVEVbKCJzaXRlLyouaHRtbCIpXQogICAgICAgIENBWyJjb21taXQgYWdlbnQgKGhvdXJseSkiXQogICAgICAgIFMgLS0-IFJBVyAtLT4gQyAtLT4gSQogICAgICAgIEdIWyJHaXRIdWIgcHVsbCByZXF1ZXN0czxici8-YnkgdGhlIG1haW50YWluZXJzIl0gLS0-IFAgLS0-IEkKICAgICAgICBJIC0tPiBTVU0gLS0-IENMIC0tPiBKIC0tPiBSIC0tPiBTSVRFCiAgICAgICAgSSAtLT4gQ0EKICAgICAgICBKIC0uIHByZXZpb3VzIHN1bW1hcnkgLi0-IFNVTQogICAgICAgIFNJVEUgLS0-IFMyWyJoZXJvbiBzZXJ2ZTxici8-R0VUIC8gKGtleSBjb29raWUgb3IgQmFzaWMgYXV0aCkiXQogICAgZW5kCgogICAgTEEgLS0gImJlYXJlciB0b2tlbiwgYmFzZTY0IGNodW5rcywgb2Zmc2V0cyIgLS0-IFMKICAgIEJBIC0tICJzYW1lIHByb3RvY29sIiAtLT4gUwogICAgQ0EgLS0gImNvbW1pdCArIHB1c2ggdmVyaWZpZWQgd29yayIgLS0-IFJFUE9bImdpdCByZW1vdGUiXQogICAgUzIgLS0-IEJSWyJ5b3VyIGJyb3dzZXIiXQo=).

## Server

    git clone <this repo> /opt/heron
    /opt/heron/deploy/install-server.sh      # writes /etc/heron/config.json, prints the tokens
    systemctl status heron-server heron-site.timer heron-commit.timer

Put the server behind TLS (a reverse proxy, a Tailscale funnel path, …).
The site is never served without the Basic auth in the config: the
transcripts hold whatever your shells printed.

## A machine you work on

Download `heron-agent` for your platform from the latest release (or
`Heron.app` on macOS), then:

    heron-agent install --server https://your.server/heron --token <ingest_token>

That writes `~/.config/heron/agent.toml`, adds the shell hook to
`~/.bashrc` and `~/.zshrc`, and registers a launchd agent or a systemd user
service so it runs at login. Open a new shell; from then on it is recorded.
`HERON_NO_RECORD=1` before opening a shell skips that one.

## The page

Topics come from `claude -p` with a JSON schema: title, status, milestones
with done/not done, and a post in Markdown whose `[[S1a2b3c4#12]]` citations
become links to `sessions/S1a2b3c4.html#t-12`. Pull requests by the
configured maintainers on the configured repositories are read with `gh`
and cited the same way (`P…` ids). The commit agent's runs get a tab of
their own.
