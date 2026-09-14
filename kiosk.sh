#!/usr/bin/env bash
# Single-instance fullscreen launcher for the Farm climate monitor.
# Used by autostart. Safe to call more than once: flock keeps just one running.
DIR="$(cd "$(dirname "$0")" && pwd)"
exec 9>/tmp/farm-monitor.lock
flock -n 9 || exit 0          # another instance already starting/running
cd "$DIR"
# Prefer the hardware venv (Blinka + system PyQt6 via --system-site-packages),
# then legacy venvs, then system python.
PY="$DIR/.venv-hw/bin/python"
[ -x "$PY" ] || PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$HOME/farm-venv/bin/python"
[ -x "$PY" ] || PY="python3"
# --hardware = real sensors only, never fake/simulated data on the Pi.
exec "$PY" farm_monitor.py --hardware --fullscreen
