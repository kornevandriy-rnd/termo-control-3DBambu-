#!/usr/bin/env python3
"""Wiring check: scan each TCA9548A (0x70..0x77) channel and list devices.

Run on the Pi (after setup_hw.sh):  .venv-hw/bin/python i2c_scan.py
Expected per wired sensor:  AHT20 at 0x38, BMP280 at 0x76 or 0x77.
"""
import time

import board
import busio

MUX_ADDRESSES = range(0x70, 0x78)
KNOWN = {0x38: "AHT20", 0x76: "BMP280", 0x77: "BMP280"}


def scan(bus):
    while not bus.try_lock():
        time.sleep(0.01)
    try:
        return bus.scan()
    finally:
        bus.unlock()


def main():
    i2c = busio.I2C(board.SCL, board.SDA)
    root = scan(i2c)
    print("Root bus:", [hex(a) for a in root])

    muxes = [a for a in root if a in MUX_ADDRESSES]
    if not muxes:
        print("\nTCA9548A не знайдено (очікував 0x70). Перевір живлення/проводку.")
        return

    import adafruit_tca9548a

    found_total = 0
    for addr in muxes:
        print(f"\n=== MUX 0x{addr:02x} ===")
        mux = adafruit_tca9548a.TCA9548A(i2c, address=addr)
        for ch in range(8):
            devs = [a for a in scan(mux[ch]) if a not in MUX_ADDRESSES]
            if devs:
                found_total += 1
                print(f"  ch{ch}: " + ", ".join(f"0x{a:02x}({KNOWN.get(a, '?')})" for a in devs))
            else:
                print(f"  ch{ch}: -")
    print(f"\nКаналів із датчиками: {found_total}")


if __name__ == "__main__":
    main()
