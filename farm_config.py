#!/usr/bin/env python3
"""Configuration & sensor-map layer for the farm climate monitor.

Kept free of any Qt / hardware imports so it can be loaded and unit-tested on
any machine (CI, a dev PC, the Pi). ``farm_monitor.py`` imports the resolved
config from here; ``check_config.py`` prints the resolved sensor map without
touching Qt or I2C.

The whole point of this file: the farm grows one sensor at a time from 1 to 21
(7 racks x 3 floors). None of that should need a code edit — topology, I2C
wiring, thresholds and per-sensor calibration all come from ``config.json``,
merged over the defaults below. Anything you leave out keeps the default.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults. A missing / partial config.json falls back to exactly this, which
# reproduces the original 7x3 = 21 layout with one TCA9548A per floor.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "layout": {
        "racks": 7,
        "floors": 3,
        "columns": 2,  # tiles per row on the overview grid
        "floor_names": {
            "1": "Нижній поверх",
            "2": "Середній поверх",
            "3": "Верхній поверх",
        },
    },
    "thresholds": {
        "temp_warn": 32.0,
        "temp_alert": 38.0,
        "hum_warn": 55.0,
        "hum_alert": 65.0,
    },
    "debounce": {"enter_ticks": 2, "clear_ticks": 3},
    "ui": {"tick_ms": 2000, "history": 60},
    "i2c": {
        # One multiplexer per floor; channel = rack - 1 + channel_base.
        "floor_mux": {"1": "0x70", "2": "0x71", "3": "0x72"},
        "channel_base": 0,
        "auto_detect": True,      # identify sensor type from its I2C address
        "redetect_seconds": 5,    # how often to retry a not-yet-answering channel
        "mux_rescan_seconds": 30, # how often to look for a mux plugged in later
    },
    # Optional per-position overrides. Each entry may set any of:
    #   rack, floor (required to target a position),
    #   mux ("0x70" or 112), channel (int 0..7),
    #   type ("auto" | "aht" | "aht10" | "aht20" | "hdc1080"),
    #   t_offset, h_offset (calibration, degrees C / %RH).
    "sensors": [],
    # Alternative place to put calibration only, keyed "rack,floor":
    #   "calibration": {"4,2": {"t_offset": -0.6, "h_offset": 2.0}}
    "calibration": {},
    "logging": {
        # Empty = off. A path (e.g. "readings.csv") appends one row per tick.
        # Kept OFF by default: on a read-only overlay FS / to spare the SD card.
        "readings_csv": "",
        "log_every_ticks": 30,
    },
    "telegram": {"token": "", "chat_id": ""},
}

# I2C addresses we know how to talk to.
ADDR_AHT = 0x38               # AHT10 / AHT20 (temperature + humidity)
ADDR_HDC1080 = 0x40           # HDC1080 (temperature + humidity)
ADDR_BMP_PRIMARY = 0x76       # BMP280 / BME280 (pressure [+ humidity on BME])
ADDR_BMP_SECONDARY = 0x77
MUX_ADDRESSES = range(0x70, 0x78)


class ConfigError(ValueError):
    """Raised for a config.json that is present but malformed."""


def parse_addr(value) -> int:
    """Accept 112, "112" or "0x70" and return an int address."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ConfigError(f"invalid I2C address: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text, 0)
        except ValueError as exc:
            raise ConfigError(f"invalid I2C address: {value!r}") from exc
    raise ConfigError(f"invalid I2C address: {value!r}")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge dicts. Non-dict values (incl. lists) replace wholesale."""
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Return DEFAULTS deep-merged with config.json (if it exists & parses).

    A missing file is fine (returns defaults). A present-but-broken file raises
    ConfigError so the operator hears about the typo instead of silently
    running the wrong layout.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if path is None:
        return cfg
    p = Path(path)
    if not p.exists():
        return cfg
    try:
        with open(p, encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"{p}: top level must be a JSON object")
    return _deep_merge(cfg, user)


def floor_names(cfg: dict) -> dict[int, str]:
    """Return {floor_number: label} with int keys, filled in for any gaps."""
    names = {}
    raw = cfg["layout"].get("floor_names", {})
    for k, v in raw.items():
        try:
            names[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    for f in range(1, int(cfg["layout"]["floors"]) + 1):
        names.setdefault(f, f"Поверх {f}")
    return names


def _calibration_for(cfg: dict, rack: int, floor: int) -> tuple[float, float]:
    entry = cfg.get("calibration", {}).get(f"{rack},{floor}", {})
    return float(entry.get("t_offset", 0.0)), float(entry.get("h_offset", 0.0))


def build_sensor_map(cfg: dict) -> list[dict]:
    """Resolve the full list of sensor positions from the config.

    Every (rack, floor) position gets an entry, wired by default as
    ``mux = floor_mux[floor]``, ``channel = rack - 1 + channel_base``. Any
    entry in ``cfg["sensors"]`` overrides the matching position (mux, channel,
    type, calibration). Positions are returned floor-by-floor, rack-by-rack.
    """
    racks = int(cfg["layout"]["racks"])
    floors = int(cfg["layout"]["floors"])
    if racks < 1 or floors < 1:
        raise ConfigError("layout.racks and layout.floors must be >= 1")

    i2c = cfg["i2c"]
    floor_mux = {int(k): parse_addr(v) for k, v in i2c.get("floor_mux", {}).items()}
    base = int(i2c.get("channel_base", 0))

    overrides: dict[tuple[int, int], dict] = {}
    for entry in cfg.get("sensors", []):
        if not isinstance(entry, dict) or "rack" not in entry or "floor" not in entry:
            raise ConfigError(f"each sensors[] entry needs rack & floor: {entry!r}")
        overrides[(int(entry["rack"]), int(entry["floor"]))] = entry

    positions = []
    for floor in range(1, floors + 1):
        for rack in range(1, racks + 1):
            ov = overrides.get((rack, floor), {})
            mux = parse_addr(ov["mux"]) if "mux" in ov else floor_mux.get(floor)
            channel = int(ov["channel"]) if "channel" in ov else base + rack - 1
            stype = str(ov.get("type", "auto")).lower()
            t_off, h_off = _calibration_for(cfg, rack, floor)
            t_off = float(ov.get("t_offset", t_off))
            h_off = float(ov.get("h_offset", h_off))
            positions.append({
                "rack": rack,
                "floor": floor,
                "mux": mux,          # None -> position is disabled (no mux mapped)
                "channel": channel,
                "type": stype,
                "t_offset": t_off,
                "h_offset": h_off,
            })
    return positions


# ---------------------------------------------------------------------------
# Pure sensor math (unit-testable without hardware).
# ---------------------------------------------------------------------------
def hdc1080_convert(raw_t: int, raw_h: int) -> tuple[float, float]:
    """Convert HDC1080 raw 16-bit words to (temperature °C, humidity %RH).

    Per the TI HDC1080 datasheet:
        T  = (raw_t / 2**16) * 165 - 40
        RH = (raw_h / 2**16) * 100
    """
    temp = (raw_t / 65536.0) * 165.0 - 40.0
    hum = (raw_h / 65536.0) * 100.0
    return temp, max(0.0, min(100.0, hum))


if __name__ == "__main__":  # tiny smoke print
    c = load_config()
    m = build_sensor_map(c)
    print(f"{len(m)} positions, floors={floor_names(c)}")
