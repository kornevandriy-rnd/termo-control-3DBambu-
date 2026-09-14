#!/usr/bin/env bash
# Deploy the farm monitor FROM this laptop TO the Raspberry Pi over SSH/rsync.
# Meant to be run on the laptop (Linux/macOS). Raspberry stays on the LAN only —
# never expose it to the internet; SSH over Ethernet/Wi-Fi is all you need.
#
# Usage:
#   PI_HOST=pi@raspberrypi.local bash deploy_to_pi.sh           # just sync files
#   bash deploy_to_pi.sh pi@192.168.1.50                        # host as argument
#   RUN=diag     PI_HOST=pi@raspberrypi.local bash deploy_to_pi.sh   # sync + diagnose
#   RUN=setup    PI_HOST=... bash deploy_to_pi.sh               # sync + install deps
#   RUN=hardware PI_HOST=... bash deploy_to_pi.sh               # sync + run on sensors
#
# Env options:
#   PI_HOST  user@host of the Pi (or pass as first argument)
#   DEST     remote folder (default: ~/farm-ui)
#   RUN      after sync also run: setup | diag | hardware | sim   (default: none)
#   MIRROR   set to 1 to delete remote files not present locally (default: off)
set -euo pipefail

PI_HOST="${1:-${PI_HOST:-}}"
DEST="${DEST:-~/farm-ui}"
RUN="${RUN:-}"
DELETE=""
[ "${MIRROR:-0}" = "1" ] && DELETE="--delete"

if [ -z "$PI_HOST" ]; then
    echo "Set PI_HOST=pi@raspberrypi.local (or pass it as the first argument)." >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)/"

echo "==> Syncing $SRC -> $PI_HOST:$DEST"
# Never ship local-only state: git, caches, venvs, the operator's config & logs.
rsync -az $DELETE \
    --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.venv' --exclude '.venv-hw' --exclude 'venv' \
    --exclude 'config.json' --exclude 'alerts.log' --exclude '*.csv' \
    "$SRC" "$PI_HOST:$DEST/"

case "$RUN" in
    setup)    ssh -t "$PI_HOST" "cd $DEST && bash setup_hw.sh" ;;
    diag)     ssh -t "$PI_HOST" "cd $DEST && bash diag.sh" ;;
    hardware) ssh -t "$PI_HOST" "cd $DEST && .venv-hw/bin/python farm_monitor.py --hardware" ;;
    sim)      ssh -t "$PI_HOST" "cd $DEST && python3 farm_monitor.py --sim" ;;
    "")       echo "==> Done. (RUN=setup|diag|hardware to also run a step)" ;;
    *)        echo "Unknown RUN=$RUN (use setup|diag|hardware|sim)" >&2; exit 1 ;;
esac
