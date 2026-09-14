#!/usr/bin/env python3
"""Headless tests for farm_config (no Qt / no hardware needed).

Run:  python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import farm_config as fc  # noqa: E402


class ParseAddrTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(fc.parse_addr(0x70), 112)
        self.assertEqual(fc.parse_addr("0x70"), 112)
        self.assertEqual(fc.parse_addr("112"), 112)
        self.assertEqual(fc.parse_addr(112), 112)

    def test_rejects_junk(self):
        for bad in ("nope", True, None, 3.5):
            with self.assertRaises(fc.ConfigError):
                fc.parse_addr(bad)


class DefaultMapTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fc.load_config()  # no path -> pure defaults
        self.smap = fc.build_sensor_map(self.cfg)

    def test_21_positions(self):
        self.assertEqual(len(self.smap), 21)

    def test_default_wiring(self):
        by = {(p["rack"], p["floor"]): p for p in self.smap}
        # floor 1 -> 0x70, channel = rack - 1
        self.assertEqual(by[(1, 1)]["mux"], 0x70)
        self.assertEqual(by[(1, 1)]["channel"], 0)
        self.assertEqual(by[(7, 1)]["channel"], 6)
        # floor 2/3 -> 0x71/0x72
        self.assertEqual(by[(1, 2)]["mux"], 0x71)
        self.assertEqual(by[(1, 3)]["mux"], 0x72)
        # everything auto-detects its type by default
        self.assertTrue(all(p["type"] == "auto" for p in self.smap))
        self.assertTrue(all(p["t_offset"] == 0.0 for p in self.smap))

    def test_floor_names_int_keys(self):
        names = fc.floor_names(self.cfg)
        self.assertEqual(names[1], "Нижній поверх")
        self.assertEqual(set(names), {1, 2, 3})


class OverrideMergeTests(unittest.TestCase):
    def _write(self, obj) -> str:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(obj, tmp)
        tmp.close()
        return tmp.name

    def test_partial_merge_keeps_defaults(self):
        path = self._write({"thresholds": {"temp_alert": 41.0}})
        cfg = fc.load_config(path)
        self.assertEqual(cfg["thresholds"]["temp_alert"], 41.0)
        # untouched defaults survive
        self.assertEqual(cfg["thresholds"]["temp_warn"], 32.0)
        self.assertEqual(cfg["layout"]["racks"], 7)

    def test_sensor_override(self):
        path = self._write({
            "layout": {"racks": 2, "floors": 1},
            "sensors": [
                {"rack": 1, "floor": 1, "type": "hdc1080",
                 "mux": "0x71", "channel": 3, "t_offset": -0.5, "h_offset": 2.0},
            ],
            "calibration": {"2,1": {"t_offset": 1.0}},
        })
        cfg = fc.load_config(path)
        smap = fc.build_sensor_map(cfg)
        self.assertEqual(len(smap), 2)
        by = {(p["rack"], p["floor"]): p for p in smap}
        self.assertEqual(by[(1, 1)]["type"], "hdc1080")
        self.assertEqual(by[(1, 1)]["mux"], 0x71)
        self.assertEqual(by[(1, 1)]["channel"], 3)
        self.assertEqual(by[(1, 1)]["t_offset"], -0.5)
        # rack 2 keeps default wiring but picks up calibration block
        self.assertEqual(by[(2, 1)]["channel"], 1)
        self.assertEqual(by[(2, 1)]["t_offset"], 1.0)

    def test_broken_json_raises(self):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write("{ not json ")
        tmp.close()
        with self.assertRaises(fc.ConfigError):
            fc.load_config(tmp.name)

    def test_missing_file_is_defaults(self):
        cfg = fc.load_config("/no/such/config-xyz.json")
        self.assertEqual(cfg["layout"]["racks"], 7)


class HDC1080Tests(unittest.TestCase):
    def test_endpoints(self):
        t, h = fc.hdc1080_convert(0, 0)
        self.assertAlmostEqual(t, -40.0, places=3)
        self.assertAlmostEqual(h, 0.0, places=3)
        t, h = fc.hdc1080_convert(0xFFFF, 0xFFFF)
        self.assertAlmostEqual(t, 125.0, places=1)
        self.assertAlmostEqual(h, 100.0, places=1)

    def test_roundtrip_25c_50rh(self):
        raw_t = round((25.0 + 40.0) / 165.0 * 65536)
        raw_h = round(50.0 / 100.0 * 65536)
        t, h = fc.hdc1080_convert(raw_t, raw_h)
        self.assertAlmostEqual(t, 25.0, places=1)
        self.assertAlmostEqual(h, 50.0, places=1)


if __name__ == "__main__":
    unittest.main()
