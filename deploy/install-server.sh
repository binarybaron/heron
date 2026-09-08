#!/usr/bin/env bash
# Install the heron server on this machine (systemd, run as root).
#
# Copies the units from this directory with the checkout's path filled in,
# creates the state directory and, if /etc/heron/config.json does not exist
# yet, writes one from config.example.json with a fresh ingest token and
# site password, which it prints once. Then enables the server and timers.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/etc/heron/config.json
STATE=/var/lib/heron
UNITS="heron-server.service heron-site.service heron-site.timer heron-commit.service heron-commit.timer"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-server.sh: run as root" >&2
    exit 1
fi

mkdir -p "$STATE" /etc/heron
chmod 700 "$STATE"

if [ ! -e "$CONFIG" ]; then
    python3 - "$REPO/config.example.json" "$CONFIG" "$STATE" <<'PY'
import json, secrets, sys
example, target, state = sys.argv[1:4]
config = json.load(open(example))
config["ingest_token"] = secrets.token_urlsafe(32)
config["site_password"] = secrets.token_urlsafe(18)
config["state"] = state
with open(target, "w") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
print(f"wrote {target}")
print(f"  ingest_token:  {config['ingest_token']}")
print(f"  site_user:     {config['site_user']}")
print(f"  site_password: {config['site_password']}")
PY
    chmod 600 "$CONFIG"
else
    echo "keeping the existing $CONFIG"
fi

for unit in $UNITS; do
    sed "s|@REPO@|$REPO|g" "$REPO/deploy/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now heron-server.service heron-site.timer heron-commit.timer
systemctl --no-pager --lines=0 status heron-server.service heron-site.timer heron-commit.timer || true
echo "installed from $REPO; the site pipeline runs every two hours, the commit agent hourly"
