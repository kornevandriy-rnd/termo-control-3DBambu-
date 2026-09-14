#!/usr/bin/env bash
# Prepare the Pi to read the real sensor mix via TCA9548A:
#   AHT10 / AHT20 (0x38), HDC1080 (0x40), BMP280 / BME280 (0x76/0x77).
# HDC1080 is read directly (no extra library); the others use Adafruit libs.
# Creates a venv that ALSO sees the apt PyQt6, and installs Adafruit libs.
# Run on the Pi:  bash setup_hw.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Enabling I2C + tools"
sudo raspi-config nonint do_i2c 0 || true
sudo apt-get update
sudo apt-get install -y python3-venv i2c-tools

echo "==> Creating venv (.venv-hw) with access to system PyQt6"
python3 -m venv --system-site-packages "$DIR/.venv-hw"
"$DIR/.venv-hw/bin/pip" install --upgrade pip
"$DIR/.venv-hw/bin/pip" install \
    adafruit-blinka \
    adafruit-circuitpython-ahtx0 \
    adafruit-circuitpython-bmp280 \
    adafruit-circuitpython-bme280 \
    adafruit-circuitpython-tca9548a

cat <<EOF

==> Done.
 1. (Якщо I2C щойно увімкнули) перезавантаж:   sudo reboot
 2. Швидка перевірка, що мультиплексор видно:   i2cdetect -y 1   (має бути 0x70)
 3. Перевірка датчиків по каналах:              .venv-hw/bin/python i2c_scan.py
 4. (Опційно) виправ розкладку/пороги у config.json (див. config.example.json)
    і перевір її:                               python3 check_config.py
 5. Запуск із реальними датчиками:              .venv-hw/bin/python farm_monitor.py --hardware
EOF
