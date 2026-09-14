#!/usr/bin/env python3
"""Standalone HDC1080 read loop over a TCA9548A channel — no Qt, no kiosk.

Isolates sensor / bus / wiring stability from the UI: it just reads one sensor
once a second and prints the value (or the error). If this runs clean for 30 s,
the hardware is solid and any dropout is the app; if it throws errors, it's the
wiring / contacts / bus.

IMPORTANT: stop the kiosk first so nothing else touches the bus:
    pkill -f farm_monitor

Run with the hardware venv:
    .venv-hw/bin/python sensor_probe.py            # mux 0x70, channel 0 (rack1/floor1)
    .venv-hw/bin/python sensor_probe.py 0x71 3     # mux 0x71, channel 3
Ctrl+C to stop.
"""
import sys
import time

import board
import busio
import adafruit_tca9548a

import farm_config as fc

MUX = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x70
CHANNEL = int(sys.argv[2]) if len(sys.argv) > 2 else 0
ADDR = fc.ADDR_HDC1080  # 0x40


def read(chan):
    while not chan.try_lock():
        time.sleep(0.005)
    try:
        chan.writeto(ADDR, bytes([0x02, 0x10, 0x00]))  # config: MODE=1, 14-bit
        time.sleep(0.02)
        chan.writeto(ADDR, bytes([0x00]))              # trigger measurement
        time.sleep(0.02)
        buf = bytearray(4)
        chan.readfrom_into(ADDR, buf)
    finally:
        chan.unlock()
    raw_t = (buf[0] << 8) | buf[1]
    raw_h = (buf[2] << 8) | buf[3]
    return fc.hdc1080_convert(raw_t, raw_h)


def main():
    i2c = busio.I2C(board.SCL, board.SDA)
    tca = adafruit_tca9548a.TCA9548A(i2c, address=MUX)
    chan = tca[CHANNEL]
    print(f"mux 0x{MUX:02x} · channel {CHANNEL} · addr 0x{ADDR:02x} — Ctrl+C to stop")
    ok = err = 0
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:
            t, h = read(chan)
            ok += 1
            print(f"{stamp}  T={t:5.1f}°C  RH={h:5.1f}%   ok={ok} err={err}")
        except Exception as exc:
            err += 1
            print(f"{stamp}  ERR: {exc!r}   ok={ok} err={err}")
        time.sleep(1)


if __name__ == "__main__":
    main()
