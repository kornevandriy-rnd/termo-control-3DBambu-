#!/usr/bin/env bash
# Install Qt (PyQt6 from apt — works on Raspberry Pi ARM) and run the UI.
# Usage on the Pi:  bash run_on_pi.sh
set -e

if ! python3 -c "import PyQt6" 2>/dev/null && ! python3 -c "import PySide6" 2>/dev/null; then
  sudo apt update
  sudo apt install -y python3-pyqt6
fi

cd "$(dirname "$0")"
exec python3 farm_monitor.py
