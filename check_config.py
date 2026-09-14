#!/usr/bin/env python3
"""Print the resolved sensor map from config.json — no Qt, no I2C needed.

Handy on any machine to check that your wiring / overrides came out the way you
meant before running the kiosk. Run:  python3 check_config.py [config.json]
"""
import sys
from pathlib import Path

import farm_config as fc

HERE = Path(__file__).resolve().parent


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.json"
    try:
        cfg = fc.load_config(path)
    except fc.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    where = path if path.exists() else "(defaults — no config.json found)"
    smap = fc.build_sensor_map(cfg)
    names = fc.floor_names(cfg)
    th = cfg["thresholds"]

    print(f"config:   {where}")
    print(f"layout:   {cfg['layout']['racks']} racks x {cfg['layout']['floors']} "
          f"floors = {len(smap)} sensor positions")
    print(f"thresholds: temp warn/alert {th['temp_warn']}/{th['temp_alert']} °C, "
          f"hum warn/alert {th['hum_warn']}/{th['hum_alert']} %")
    csv = cfg["logging"].get("readings_csv", "")
    print(f"csv log:  {'off' if not csv else csv}")
    tg = cfg.get("telegram", {})
    print(f"telegram: {'on' if tg.get('token') and tg.get('chat_id') else 'off'}")
    print()
    print(f"{'rack':>4} {'floor':>5}  {'mux':>5} {'ch':>3}  {'type':<8} "
          f"{'t_off':>6} {'h_off':>6}  floor name")
    print("-" * 62)
    for p in smap:
        mux = f"0x{p['mux']:02x}" if p["mux"] is not None else "  —"
        print(f"{p['rack']:>4} {p['floor']:>5}  {mux:>5} {p['channel']:>3}  "
              f"{p['type']:<8} {p['t_offset']:>6.2f} {p['h_offset']:>6.2f}  "
              f"{names.get(p['floor'], '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
