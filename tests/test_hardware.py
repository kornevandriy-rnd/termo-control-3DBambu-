#!/usr/bin/env python3
"""Headless tests for the hardware layer, using a fake I2C bus (no Pi needed).

Emulates an HDC1080 at 0x40 through the TCA9548A channel interface, so the
detection / read / calibration / hot-plug paths of HardwareDataSource are all
exercised without any real sensor or Blinka install.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # Qt import needs a platform

import farm_config as fc          # noqa: E402
import farm_monitor as fm         # noqa: E402


class _FakeHDC1080Channel:
    """One TCA9548A channel with an HDC1080 wired at 0x40 (or nothing)."""

    def __init__(self, present):
        self.present = set(present)   # I2C addresses answering on this channel
        self._ptr = None
        self.raw_t = round((25.0 + 40.0) / 165.0 * 65536)  # ~25.0 C
        self.raw_h = round(48.0 / 100.0 * 65536)           # ~48 %RH

    def try_lock(self):
        return True

    def unlock(self):
        pass

    def scan(self):
        return list(self.present)

    def writeto(self, addr, buf):
        if addr not in self.present:
            raise OSError("no device")
        if len(buf) >= 1:
            self._ptr = buf[0]

    def readfrom_into(self, addr, buf):
        if addr not in self.present:
            raise OSError("no device")
        # HDC1080: after pointing at 0x00, return 4 bytes T[hi,lo] H[hi,lo]
        vals = [self.raw_t >> 8, self.raw_t & 0xFF,
                self.raw_h >> 8, self.raw_h & 0xFF]
        for i in range(len(buf)):
            buf[i] = vals[i] if i < len(vals) else 0

    def writeto_then_readfrom(self, addr, wbuf, rbuf):
        if addr not in self.present:
            raise OSError("no device")
        for i in range(len(rbuf)):
            rbuf[i] = 0x58  # pretend BMP280 chip-id if ever probed


class _FakeMux:
    def __init__(self, channels):
        self._channels = channels

    def __getitem__(self, ch):
        return self._channels[ch]


class _FakeI2C:
    """Root bus: reports which mux addresses are present."""

    def __init__(self, present_muxes):
        self.present = set(present_muxes)

    def try_lock(self):
        return True

    def unlock(self):
        pass

    def scan(self):
        return list(self.present)


def _small_config():
    # 2 racks x 1 floor = 2 positions, both on mux 0x70 (channels 0 and 1).
    cfg = fc.load_config(None)
    cfg["layout"]["racks"] = 2
    cfg["layout"]["floors"] = 1
    cfg["i2c"]["floor_mux"] = {"1": "0x70"}
    cfg["i2c"]["mux_rescan_seconds"] = 0   # allow immediate rescan in test
    cfg["i2c"]["redetect_seconds"] = 0
    return cfg


class HardwareLayerTests(unittest.TestCase):
    def setUp(self):
        self._saved = fm.CONFIG
        fm._apply_config(_small_config())

    def tearDown(self):
        fm._apply_config(self._saved)

    def _make_source(self, present_muxes, channel_map, offsets=None):
        i2c = _FakeI2C(present_muxes)
        mux = _FakeMux(channel_map)
        src = fm.HardwareDataSource(
            i2c=i2c, tca_factory=lambda bus, address: mux, start_thread=False)
        if offsets:
            for p in src._pos:
                p["t_off"], p["h_off"] = offsets
        return src

    def test_hdc1080_read_and_offline(self):
        # channel 0 has an HDC1080, channel 1 is empty
        ch0 = _FakeHDC1080Channel(present={0x40})
        ch1 = _FakeHDC1080Channel(present=set())
        src = self._make_source({0x70}, {0: ch0, 1: ch1})
        src._poll_once()
        r1 = src.reading(1, 1)
        r2 = src.reading(2, 1)
        self.assertIsNotNone(r1)
        self.assertAlmostEqual(r1.temperature, 25.0, delta=0.2)
        self.assertAlmostEqual(r1.humidity, 48.0, delta=0.5)
        self.assertIsNone(r2)   # empty channel -> offline

    def test_calibration_offsets_applied(self):
        ch0 = _FakeHDC1080Channel(present={0x40})
        ch1 = _FakeHDC1080Channel(present=set())
        src = self._make_source({0x70}, {0: ch0, 1: ch1}, offsets=(-1.5, 3.0))
        src._poll_once()
        r1 = src.reading(1, 1)
        self.assertAlmostEqual(r1.temperature, 25.0 - 1.5, delta=0.2)
        self.assertAlmostEqual(r1.humidity, 48.0 + 3.0, delta=0.5)

    def test_hot_plug_channel_comes_online(self):
        # start with an empty channel, then a sensor appears -> next poll online
        ch0 = _FakeHDC1080Channel(present=set())
        ch1 = _FakeHDC1080Channel(present=set())
        src = self._make_source({0x70}, {0: ch0, 1: ch1})
        src._poll_once()
        self.assertIsNone(src.reading(1, 1))
        ch0.present = {0x40}          # sensor wired in later
        src._poll_once()
        self.assertIsNotNone(src.reading(1, 1))

    def test_absent_mux_all_offline(self):
        ch0 = _FakeHDC1080Channel(present={0x40})
        src = self._make_source(set(), {0: ch0, 1: ch0})  # root reports no mux
        src._poll_once()
        self.assertIsNone(src.reading(1, 1))
        self.assertIsNone(src.reading(2, 1))

    def test_history_accumulates(self):
        ch0 = _FakeHDC1080Channel(present={0x40})
        ch1 = _FakeHDC1080Channel(present=set())
        src = self._make_source({0x70}, {0: ch0, 1: ch1})
        for _ in range(3):
            src._poll_once()
        temps, hums = src.history(1, 1)
        self.assertEqual(len(temps), 3)
        self.assertEqual(len(hums), 3)


class SimulationSmokeTests(unittest.TestCase):
    def test_sim_source_shapes(self):
        src = fm.SimulatedDataSource()
        src.step()
        r = src.reading(1, 1)
        self.assertIsNotNone(r)
        self.assertTrue(hasattr(r, "temperature") and hasattr(r, "humidity"))
        t, h = src.history(1, 1)
        self.assertTrue(len(t) >= 1)


if __name__ == "__main__":
    unittest.main()
