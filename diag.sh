#!/usr/bin/env bash
# On-Pi diagnostics for the farm-monitor hardware bring-up.
# Run on the Raspberry Pi:  bash diag.sh
# Bundles everything needed to see what's wired and how config resolves.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv-hw/bin/python"
[ -x "$PY" ] || PY="python3"

echo "===== system ====="
uname -a
echo -n "python: "; python3 --version
echo -n "hw venv: "; [ -x "$DIR/.venv-hw/bin/python" ] && echo ".venv-hw present" || echo "MISSING (run: bash setup_hw.sh)"

echo
echo "===== I2C bus 1 (i2cdetect -y 1) ====="
if command -v i2cdetect >/dev/null 2>&1; then
    i2cdetect -y 1 || echo "(i2cdetect failed — is I2C enabled? sudo raspi-config nonint do_i2c 0; sudo reboot)"
else
    echo "i2c-tools not installed (run: bash setup_hw.sh)"
fi
echo "Expected mux addresses: 0x70 (floor 1), 0x71 (floor 2), 0x72 (floor 3)."

echo
echo "===== per-channel scan (i2c_scan.py) ====="
echo "Expected devices: 0x38 AHT10/AHT20 · 0x40 HDC1080 · 0x76/0x77 BMP280/BME280"
"$PY" "$DIR/i2c_scan.py" 2>&1 || echo "(i2c_scan failed — check Blinka install and wiring)"

echo
echo "===== resolved config (check_config.py) ====="
python3 "$DIR/check_config.py" 2>&1

echo
echo "===== alerts.log (last 10 lines) ====="
if [ -f "$DIR/alerts.log" ]; then tail -n 10 "$DIR/alerts.log"; else echo "(no alerts.log yet)"; fi
