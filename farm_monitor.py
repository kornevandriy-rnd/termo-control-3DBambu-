#!/usr/bin/env python3
"""Farm climate monitor — touchscreen UI (Qt).

Native desktop app for the 3D-printing farm climate monitor. Designed for the
Raspberry Pi 7" Touch Display 2 (portrait 720x1280) but runs on Windows/macOS/
Linux for development.

Real sensors are read via HardwareDataSource, which auto-detects a mix of
sensor types per multiplexer channel:
    AHT10 / AHT20  @ 0x38  (temperature + humidity)
    HDC1080        @ 0x40  (temperature + humidity)
    BMP280/BME280  @ 0x76/0x77  (pressure, best-effort add-on)
so any sensor at hand can be wired into a channel and it just comes online.
Without the hardware stack it falls back to SimulatedDataSource so you can see
the interface. The UI only needs three methods from a data source:
    reading(rack, floor) -> Reading | None   (None == sensor offline)
    history(rack, floor) -> (temps, hums)     (empty lists if offline)
    step()                                    (advance / pull fresh values)

Topology, I2C wiring, thresholds and calibration all come from config.json
(see farm_config.py) — scaling from 1 to 21 sensors never needs a code edit.

Alerts: a red banner appears on screen for any ALERT/offline sensor; if
config.json holds a Telegram token + chat_id, alerts are also pushed there.
Every alert (and its recovery) is appended to alerts.log.

Bindings: tries PySide6 (pip, Windows/mac), falls back to PyQt6 (apt on the Pi).

Run:  python farm_monitor.py   (or  python3 farm_monitor.py  on the Pi)
Keys: tap a rack tile / a top chip to drill in · "Назад" to return ·
      F11 (or F) = toggle fullscreen · Esc = leave fullscreen ·
      Ctrl+C in the launching terminal quits
"""
from __future__ import annotations

import csv
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import farm_config as fc

try:
    from PySide6.QtCore import Qt, QTimer, QPointF
    from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
        QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
    )
except ImportError:  # Raspberry Pi: PySide6 has no ARM wheel, use apt python3-pyqt6
    from PyQt6.QtCore import Qt, QTimer, QPointF
    from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
    from PyQt6.QtWidgets import (
        QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
        QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
    )

HERE = Path(__file__).resolve().parent

# ---- configuration (loaded from config.json, merged over farm_config.DEFAULTS)
# These module-level names are set by _apply_config() so the rest of the UI can
# keep referring to plain globals (RACKS, TEMP_WARN, FLOOR_NAMES, ...).
CONFIG: dict = {}
RACKS = FLOORS = TOTAL = COLUMNS = 0
HISTORY = TICK_MS = 0
TEMP_WARN = TEMP_ALERT = HUM_WARN = HUM_ALERT = 0.0
ENTER_TICKS = CLEAR_TICKS = 0
FLOOR_NAMES: dict[int, str] = {}
SENSOR_MAP: list[dict] = []


def _apply_config(cfg: dict) -> None:
    """Push a resolved config dict into the module globals used across the UI."""
    global CONFIG, RACKS, FLOORS, TOTAL, COLUMNS, HISTORY, TICK_MS
    global TEMP_WARN, TEMP_ALERT, HUM_WARN, HUM_ALERT
    global ENTER_TICKS, CLEAR_TICKS, FLOOR_NAMES, SENSOR_MAP
    CONFIG = cfg
    RACKS = int(cfg["layout"]["racks"])
    FLOORS = int(cfg["layout"]["floors"])
    TOTAL = RACKS * FLOORS
    COLUMNS = max(1, int(cfg["layout"].get("columns", 2)))
    HISTORY = int(cfg["ui"]["history"])
    TICK_MS = int(cfg["ui"]["tick_ms"])
    th = cfg["thresholds"]
    TEMP_WARN, TEMP_ALERT = float(th["temp_warn"]), float(th["temp_alert"])
    HUM_WARN, HUM_ALERT = float(th["hum_warn"]), float(th["hum_alert"])
    ENTER_TICKS = int(cfg["debounce"]["enter_ticks"])
    CLEAR_TICKS = int(cfg["debounce"]["clear_ticks"])
    FLOOR_NAMES = fc.floor_names(cfg)
    SENSOR_MAP = fc.build_sensor_map(cfg)


# Load once at import so both the app and headless helpers see the same config.
# A broken config.json must never leave the wall display on a black screen, so
# we fall back to the defaults and surface the reason (printed by main()).
CONFIG_ERROR = ""
try:
    _apply_config(fc.load_config(HERE / "config.json"))
except fc.ConfigError as _exc:
    CONFIG_ERROR = str(_exc)
    _apply_config(fc.load_config(None))  # safe defaults

# ---- palette (dark theme for a wall display) -------------------------------
BG, CARD, CARD2 = "#0f1115", "#1a1d24", "#222730"
TXT, MUT, LINE = "#e8eaed", "#9aa0a8", "#2c313a"
OK, WARN, ALERT, OFFLINE = "#2ecc71", "#f39c12", "#e74c3c", "#6b7280"
TEMP_C, HUM_C = "#f5a524", "#3aa0e6"


def status_color(temp: float, hum: float) -> str:
    if temp >= TEMP_ALERT or hum >= HUM_ALERT:
        return ALERT
    if temp >= TEMP_WARN or hum >= HUM_WARN:
        return WARN
    return OK


def status_word(color: str) -> str:
    return {OK: "норма", WARN: "підвищ.", ALERT: "тривога"}[color]


# ============================================================================
#  DataSource — swap this out for real data tomorrow.
# ============================================================================
@dataclass
class Reading:
    temperature: float
    humidity: float
    pressure: float = 0.0   # kept for compatibility; not shown (AHT20-only)


class SimulatedDataSource:
    def __init__(self):
        self._t, self._h, self._p = {}, {}, {}
        self.hist_t, self.hist_h = {}, {}
        self.online = {}
        for r in range(1, RACKS + 1):
            for f in range(1, FLOORS + 1):
                base_t = 24.0 + (f - 1) * 2.2 + random.uniform(-1.5, 1.5)
                base_h = 45.0 - (f - 1) * 1.5 + random.uniform(-6, 6)
                self._t[(r, f)] = base_t
                self._h[(r, f)] = base_h
                self._p[(r, f)] = 1013.0 + random.uniform(-4, 4)
                self.hist_t[(r, f)] = deque([base_t], maxlen=HISTORY)
                self.hist_h[(r, f)] = deque([base_h], maxlen=HISTORY)
                self.online[(r, f)] = True
        # demo states: rack 4 runs hot (alert), one sensor is offline.
        # Guarded so a smaller configured grid can't KeyError.
        for f in range(1, FLOORS + 1):
            if (4, f) in self._t:
                self._t[(4, f)] += 13
                self._h[(4, f)] += 20
        if (6, 2) in self.online:
            self.online[(6, 2)] = False

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def step(self):
        for k in self._t:
            if not self.online[k]:
                continue
            self._t[k] = self._clamp(self._t[k] + random.uniform(-0.4, 0.4), 10, 60)
            self._h[k] = self._clamp(self._h[k] + random.uniform(-0.9, 0.9), 10, 95)
            self._p[k] = self._clamp(self._p[k] + random.uniform(-0.3, 0.3), 980, 1040)
            self.hist_t[k].append(self._t[k])
            self.hist_h[k].append(self._h[k])

    def reading(self, rack, floor):
        k = (rack, floor)
        if not self.online.get(k, False):
            return None
        return Reading(self._t[k], self._h[k], self._p[k])

    def history(self, rack, floor):
        k = (rack, floor)
        if not self.online.get(k, False):
            return [], []
        return list(self.hist_t[k]), list(self.hist_h[k])


def _read_hdc1080(chan, addr=fc.ADDR_HDC1080):
    """Trigger + read an HDC1080 over a busio-style I2C channel (from the mux).

    Sequence per TI datasheet: set config MODE=1 (T then RH, 14-bit), write the
    temperature-register pointer to start a conversion, wait, read 4 bytes."""
    while not chan.try_lock():
        time.sleep(0.005)
    try:
        chan.writeto(addr, bytes([0x02, 0x10, 0x00]))  # config: MODE=1, 14-bit
        time.sleep(0.015)
        chan.writeto(addr, bytes([0x00]))              # point at T reg -> triggers
        time.sleep(0.02)                                # ~13 ms conversion + margin
        buf = bytearray(4)
        chan.readfrom_into(addr, buf)
    finally:
        chan.unlock()
    raw_t = (buf[0] << 8) | buf[1]
    raw_h = (buf[2] << 8) | buf[3]
    return fc.hdc1080_convert(raw_t, raw_h)


def _read_reg(chan, addr, reg):
    """Read one register byte over a mux channel (used for BMP/BME chip-id)."""
    while not chan.try_lock():
        time.sleep(0.005)
    try:
        buf = bytearray(1)
        chan.writeto_then_readfrom(addr, bytes([reg]), buf)
    finally:
        chan.unlock()
    return buf[0]


class HardwareDataSource:
    """Real sensors via TCA9548A (Adafruit Blinka), mixed types, auto-detected.

    Per channel, after the mux selects it, the device is identified by its I2C
    address and read with the right driver:
        AHT10 / AHT20   0x38        temperature + humidity
        HDC1080         0x40        temperature + humidity
        BMP280/BME280   0x76/0x77   pressure add-on (best-effort)
    A channel that doesn't answer yet stays offline and is retried, and muxes
    plugged in later are picked up on a periodic rescan — so all 21 sensors can
    be wired in one at a time without ever restarting the app. I2C is polled in
    a background thread; the UI never blocks on a slow/stuck bus. Needs
    adafruit-blinka + adafruit-circuitpython-ahtx0/-tca9548a (see setup_hw.sh);
    HDC1080 needs no extra library.
    """

    def __init__(self, i2c=None, tca_factory=None, start_thread=True):
        # i2c / tca_factory are injectable for headless tests; in production they
        # come from Adafruit Blinka (imported lazily so a dev PC needn't have it).
        if i2c is None:
            import board
            import busio
            i2c = busio.I2C(board.SCL, board.SDA)
        if tca_factory is None:
            import adafruit_tca9548a
            tca_factory = adafruit_tca9548a.TCA9548A

        self._i2c = i2c
        self._TCA = tca_factory
        i2ccfg = CONFIG["i2c"]
        self._redetect = float(i2ccfg.get("redetect_seconds", 5))
        self._mux_rescan = float(i2ccfg.get("mux_rescan_seconds", 30))
        self._auto = bool(i2ccfg.get("auto_detect", True))

        self._latest = {}
        self.hist_t, self.hist_h = {}, {}
        self._muxes = {}          # mux addr -> TCA9548A object
        self._pos = []            # per-position runtime state
        for p in SENSOR_MAP:
            key = (p["rack"], p["floor"])
            self._latest[key] = None
            self.hist_t[key] = deque(maxlen=HISTORY)
            self.hist_h[key] = deque(maxlen=HISTORY)
            self._pos.append({
                "key": key, "mux": p["mux"], "channel": p["channel"],
                "type": p["type"], "t_off": p["t_offset"], "h_off": p["h_offset"],
                "chan": None, "reader": None, "next_detect": 0.0,
            })

        self._present = set()
        self._last_root_scan = 0.0
        self._scan_root()         # first mux discovery + channel handles

        self._lock = threading.Lock()
        if start_thread:
            threading.Thread(target=self._poll_loop, daemon=True).start()

    # --- bus / discovery ---------------------------------------------------
    @staticmethod
    def _scan_bus(bus):
        while not bus.try_lock():
            time.sleep(0.005)
        try:
            return set(bus.scan())
        finally:
            bus.unlock()

    def _scan_root(self):
        """(Re)discover which muxes respond; attach channel handles to positions."""
        self._last_root_scan = time.monotonic()
        try:
            self._present = self._scan_bus(self._i2c)
        except Exception:
            return
        for p in self._pos:
            if p["chan"] is not None or p["mux"] is None:
                continue
            if p["mux"] not in self._present:
                continue
            if p["mux"] not in self._muxes:
                try:
                    self._muxes[p["mux"]] = self._TCA(self._i2c, address=p["mux"])
                except Exception:
                    continue
            try:
                p["chan"] = self._muxes[p["mux"]][p["channel"]]
            except Exception:
                p["chan"] = None

    # --- per-sensor driver builders (return a reader, or None on failure) ---
    @staticmethod
    def _make_aht(chan):
        try:
            import adafruit_ahtx0
            dev = adafruit_ahtx0.AHTx0(chan)  # AHT10 & AHT20 share this driver
        except Exception:
            return None
        return lambda: (float(dev.temperature), float(dev.relative_humidity))

    @staticmethod
    def _make_hdc(chan, addr=fc.ADDR_HDC1080):
        try:
            _read_hdc1080(chan, addr)  # probe once so a dead sensor fails now
        except Exception:
            return None
        return lambda: _read_hdc1080(chan, addr)

    @staticmethod
    def _make_bmp(chan, addr):
        """BMP280/BME280 pressure. Chip-id picks the driver; a missing library
        just means no pressure (never a crash)."""
        try:
            chip = _read_reg(chan, addr, 0xD0)
        except Exception:
            chip = None
        dev = None
        try:
            if chip == 0x60:  # BME280
                import adafruit_bme280.basic as adafruit_bme280
                dev = adafruit_bme280.Adafruit_BME280_I2C(chan, address=addr)
            else:             # BMP280 (0x58) or unknown -> try BMP280
                import adafruit_bmp280
                dev = adafruit_bmp280.Adafruit_BMP280_I2C(chan, address=addr)
        except Exception:
            return None
        return lambda: float(dev.pressure)

    def _detect(self, p):
        """Scan a channel and bind a reader() -> (temp, hum, pressure), or None."""
        chan = p["chan"]
        try:
            devs = self._scan_bus(chan)
        except Exception:
            return None
        forced = p["type"]
        read_th = None
        if forced in ("aht", "aht10", "aht20"):
            read_th = self._make_aht(chan)
        elif forced in ("hdc1080", "hdc"):
            read_th = self._make_hdc(chan)
        elif self._auto and forced in ("", "auto"):
            if fc.ADDR_AHT in devs:
                read_th = self._make_aht(chan)
            elif fc.ADDR_HDC1080 in devs:
                read_th = self._make_hdc(chan)
        if read_th is None:
            return None  # no temperature/humidity source -> not a climate position

        read_p = None
        for a in (fc.ADDR_BMP_PRIMARY, fc.ADDR_BMP_SECONDARY):
            if a in devs:
                read_p = self._make_bmp(chan, a)
                break

        def read():
            t, h = read_th()
            pr = 0.0
            if read_p is not None:
                try:
                    pr = read_p()
                except Exception:
                    pr = 0.0
            return t, h, pr

        return read

    # --- polling -----------------------------------------------------------
    def _poll_once(self):
        """One full sweep of all positions. Split out so tests can drive it."""
        now = time.monotonic()
        if (now - self._last_root_scan >= self._mux_rescan and
                any(p["chan"] is None and p["mux"] is not None for p in self._pos)):
            self._scan_root()
        for p in self._pos:
            reading = None
            if p["chan"] is not None:
                if p["reader"] is None and now >= p["next_detect"]:
                    p["reader"] = self._detect(p)
                    p["next_detect"] = now + self._redetect
                if p["reader"] is not None:
                    try:
                        t, h, pr = p["reader"]()
                        t += p["t_off"]
                        h += p["h_off"]
                        reading = Reading(round(t, 2), round(h, 2), round(pr, 2))
                    except Exception:
                        p["reader"] = None            # force re-detect next cycle
                        p["next_detect"] = now + self._redetect
                        reading = None
            with self._lock:
                self._latest[p["key"]] = reading
                if reading is not None:
                    self.hist_t[p["key"]].append(reading.temperature)
                    self.hist_h[p["key"]].append(reading.humidity)

    def _poll_loop(self):
        while True:
            self._poll_once()
            time.sleep(TICK_MS / 1000.0)

    def step(self):
        pass  # the background thread does the polling

    def reading(self, rack, floor):
        with self._lock:
            return self._latest.get((rack, floor))

    def history(self, rack, floor):
        with self._lock:
            return (list(self.hist_t.get((rack, floor), [])),
                    list(self.hist_h.get((rack, floor), [])))


# ============================================================================
#  Alerts — detection, debounce, on-disk log, optional Telegram push
# ============================================================================
class AlertManager:
    LABEL = {"alert": "ТРИВОГА", "offline": "ДАТЧИК OFFLINE"}

    def __init__(self, source, log_path=HERE / "alerts.log"):
        self.source = source
        self.log_path = log_path
        self._active = {}    # key -> message (currently notified)
        self._streak = {}    # key -> consecutive present(+)/absent(-) ticks
        tg = CONFIG.get("telegram", {})
        self.tg_token = str(tg.get("token", "") or "")
        self.tg_chat = str(tg.get("chat_id", "") or "")

    def evaluate(self):
        """Recompute problems; fire transitions. Returns (n_alert, n_offline)."""
        current = {}
        n_alert = n_off = 0
        for r in range(1, RACKS + 1):
            for f in range(1, FLOORS + 1):
                rd = self.source.reading(r, f)
                if rd is None:
                    current[("offline", r, f)] = f"Стелаж {r} · {FLOOR_NAMES[f]}: датчик offline"
                    n_off += 1
                elif status_color(rd.temperature, rd.humidity) == ALERT:
                    current[("alert", r, f)] = (
                        f"Стелаж {r} · {FLOOR_NAMES[f]}: "
                        f"тривога T {rd.temperature:.1f}°, RH {rd.humidity:.0f}%"
                    )
                    n_alert += 1
        self._update_active(current)
        return n_alert, n_off

    def _update_active(self, current):
        for key in set(self._streak) | set(current):
            s = self._streak.get(key, 0)
            if key in current:
                s = s + 1 if s >= 0 else 1
            else:
                s = s - 1 if s <= 0 else -1
            self._streak[key] = s

        for key, msg in current.items():
            if key not in self._active and self._streak[key] >= ENTER_TICKS:
                self._active[key] = msg
                self._fire(f"🔴 {self.LABEL[key[0]]}\n{msg}")

        for key in list(self._active):
            if self._streak.get(key, 0) <= -CLEAR_TICKS:
                msg = self._active.pop(key)
                self._fire(f"✅ Відновлено\n{msg}")

        for key in list(self._streak):
            if key not in self._active and self._streak[key] <= -CLEAR_TICKS:
                del self._streak[key]

    def _fire(self, text):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {text.replace(chr(10), ' | ')}\n")
        except OSError:
            pass
        if self.tg_token and self.tg_chat:
            threading.Thread(target=self._send_telegram, args=(f"{text}\n{stamp}",),
                             daemon=True).start()

    def _send_telegram(self, text):
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.tg_chat, "text": text}).encode()
        try:
            urllib.request.urlopen(url, data=data, timeout=10).read()
        except Exception:
            pass


class ReadingsLogger:
    """Optional CSV log of every sensor's reading, one row per (rack, floor) per
    interval. OFF unless config.logging.readings_csv is set — on a read-only
    overlay FS or to spare the SD card, leave it empty. Writes a header once."""

    def __init__(self, source):
        self.source = source
        cfg = CONFIG.get("logging", {})
        path = str(cfg.get("readings_csv", "") or "").strip()
        self.path = (HERE / path) if path and not Path(path).is_absolute() else (
            Path(path) if path else None)
        self.every = max(1, int(cfg.get("log_every_ticks", 30)))
        self._n = 0
        if self.path and not self.path.exists():
            try:
                with open(self.path, "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(
                        ["timestamp", "rack", "floor", "temperature", "humidity", "pressure"])
            except OSError:
                self.path = None

    def tick(self):
        if self.path is None:
            return
        self._n += 1
        if self._n % self.every:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for r in range(1, RACKS + 1):
            for f in range(1, FLOORS + 1):
                rd = self.source.reading(r, f)
                if rd is None:
                    rows.append([stamp, r, f, "", "", ""])
                else:
                    p = f"{rd.pressure:.1f}" if getattr(rd, "pressure", 0.0) > 0 else ""
                    rows.append([stamp, r, f, f"{rd.temperature:.2f}",
                                 f"{rd.humidity:.2f}", p])
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
        except OSError:
            pass


# ============================================================================
#  Widgets
# ============================================================================
class Sparkline(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(58)
        self._t, self._h = [], []

    def set_data(self, temps, hums):
        self._t, self._h = temps, hums
        self.update()

    @staticmethod
    def _poly(data, w, h, pad):
        if len(data) < 2:
            return None
        lo, hi = min(data), max(data)
        rng = (hi - lo) or 1.0
        n = len(data)
        pts = []
        for i, v in enumerate(data):
            x = pad + (w - 2 * pad) * i / (n - 1)
            y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
            pts.append(QPointF(x, y))
        return QPolygonF(pts)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(LINE), 1))
        p.drawLine(0, h - 1, w, h - 1)
        for data, color in ((self._h, HUM_C), (self._t, TEMP_C)):
            poly = self._poly(data, w, h, 6)
            if poly is not None:
                p.setPen(QPen(QColor(color), 2))
                p.drawPolyline(poly)
        p.end()


class ZoneTile(QFrame):
    def __init__(self, rack: int, on_click):
        super().__init__()
        self.rack = rack
        self._on_click = on_click
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.title = QLabel(f"Стелаж {rack}")
        self.title.setObjectName("tileTitle")
        self.temp = QLabel("–")
        self.temp.setObjectName("tileTemp")
        self.hum = QLabel("вологість –")
        self.hum.setObjectName("tileSub")
        self.state = QLabel("–")
        self.state.setObjectName("tileState")

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.state)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self.temp)
        lay.addWidget(self.hum)

    def _border(self, color):
        self.setStyleSheet(
            f"#tile {{ background: {CARD}; border: 2px solid {color};"
            f" border-radius: 16px; }}"
        )

    def update_values(self, temp, hum, online_floors):
        if online_floors == 0:
            self._border(OFFLINE)
            self.temp.setText("—")
            self.hum.setText("немає зв'язку")
            self.state.setText("offline")
            self.state.setStyleSheet(f"color: {OFFLINE}; font-size: 14px;")
            return
        col = status_color(temp, hum)
        self._border(col)
        self.temp.setText(f"{temp:.1f}°")
        sub = f"вологість {hum:.0f}%"
        if online_floors < FLOORS:
            sub += f"   ·   {online_floors}/{FLOORS} онлайн"
        self.hum.setText(sub)
        self.state.setText(status_word(col))
        self.state.setStyleSheet(f"color: {col}; font-size: 14px;")

    def mousePressEvent(self, _e):
        self._on_click(self.rack)


class FloorCard(QFrame):
    def __init__(self, floor: int):
        super().__init__()
        self.floor = floor
        self.setObjectName("floorCard")

        self.title = QLabel(FLOOR_NAMES[floor])
        self.title.setObjectName("floorTitle")
        self.state = QLabel("–")
        self.state.setObjectName("tileState")
        self.temp = QLabel("–")
        self.temp.setObjectName("floorTemp")
        self.sub = QLabel("вологість –")
        self.sub.setObjectName("tileSub")
        self.spark = Sparkline()

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.state)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self.temp)
        lay.addWidget(self.sub)
        lay.addWidget(self.spark)

    def _border(self, color):
        self.setStyleSheet(
            f"#floorCard {{ background: {CARD}; border: 2px solid {color};"
            f" border-radius: 16px; }}"
        )

    def update_values(self, r: Reading, temps, hums):
        col = status_color(r.temperature, r.humidity)
        self._border(col)
        self.temp.setText(f"{r.temperature:.1f}°")
        sub = f"вологість {r.humidity:.0f}%"
        if getattr(r, "pressure", 0.0) > 0:
            sub += f"   ·   {r.pressure:.0f} гПа"
        self.sub.setText(sub)
        self.state.setText(status_word(col))
        self.state.setStyleSheet(f"color: {col}; font-size: 14px;")
        self.spark.set_data(temps, hums)

    def update_offline(self):
        self._border(OFFLINE)
        self.temp.setText("—")
        self.sub.setText("немає зв'язку з датчиком")
        self.state.setText("offline")
        self.state.setStyleSheet(f"color: {OFFLINE}; font-size: 14px;")
        self.spark.set_data([], [])


class ClickableChip(QFrame):
    """Top summary chip; drills into an info page when tapped."""

    def __init__(self, caption: str, on_click=None):
        super().__init__()
        self.setObjectName("chip")
        self._on_click = on_click
        if on_click is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.val = QLabel("–")
        self.val.setObjectName("chipVal")
        self.cap = QLabel(caption)
        self.cap.setObjectName("chipCap")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        lay.addWidget(self.val)
        lay.addWidget(self.cap)

    def mousePressEvent(self, _e):
        if self._on_click is not None:
            self._on_click()


class InfoCard(QFrame):
    """Generic card used on the info pages (alerts / extremes / offline)."""

    def __init__(self):
        super().__init__()
        self.setObjectName("floorCard")
        self.heading = QLabel("")
        self.heading.setObjectName("tileTitle")
        self.value = QLabel("")
        self.value.setObjectName("floorTemp")
        self.sub = QLabel("")
        self.sub.setObjectName("tileSub")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        lay.addWidget(self.heading)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def update_card(self, heading, value, sub, color):
        self.setStyleSheet(
            f"#floorCard {{ background: {CARD}; border: 2px solid {color};"
            f" border-radius: 16px; }}"
        )
        self.heading.setText(heading)
        self.value.setText(value)
        self.sub.setText(sub)


# ============================================================================
#  Pages
# ============================================================================
class OverviewPage(QWidget):
    def __init__(self, source, on_open_rack, on_open_info, on_power):
        super().__init__()
        self.setObjectName("page")
        self.source = source

        header = QLabel("Ферма · клімат")
        header.setObjectName("h1")
        self.clock = QLabel("")
        self.clock.setObjectName("clock")
        power = QPushButton("Вимкнути")
        power.setObjectName("power")
        power.setCursor(Qt.CursorShape.PointingHandCursor)
        power.clicked.connect(on_power)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch()
        top.addWidget(self.clock)
        top.addWidget(power)

        chips = QHBoxLayout()
        chips.setSpacing(10)
        c_temp = ClickableChip("сер. температура", lambda: on_open_info("temp"))
        c_hum = ClickableChip("сер. вологість", lambda: on_open_info("hum"))
        c_alert = ClickableChip("тривоги", lambda: on_open_info("alerts"))
        c_sensors = ClickableChip("датчики", lambda: on_open_info("offline"))
        self.m_temp, self.m_hum = c_temp.val, c_hum.val
        self.m_alert, self.m_sensors = c_alert.val, c_sensors.val
        for c in (c_temp, c_hum, c_alert, c_sensors):
            chips.addWidget(c)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.tiles = {}
        for rack in range(1, RACKS + 1):
            tile = ZoneTile(rack, on_open_rack)
            self.tiles[rack] = tile
            grid.addWidget(tile, (rack - 1) // COLUMNS, (rack - 1) % COLUMNS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(14)
        lay.addLayout(top)
        lay.addLayout(chips)
        lay.addLayout(grid, 1)

    def refresh(self):
        temps, hums, alerts, online = [], [], 0, 0
        for rack, tile in self.tiles.items():
            rt, rh = [], []
            for f in range(1, FLOORS + 1):
                r = self.source.reading(rack, f)
                if r is None:
                    continue
                online += 1
                rt.append(r.temperature)
                rh.append(r.humidity)
                temps.append(r.temperature)
                hums.append(r.humidity)
                if status_color(r.temperature, r.humidity) == ALERT:
                    alerts += 1
            if rt:
                tile.update_values(sum(rt) / len(rt), sum(rh) / len(rh), len(rt))
            else:
                tile.update_values(0, 0, 0)

        self.m_temp.setText(f"{sum(temps) / len(temps):.1f}°" if temps else "—")
        self.m_hum.setText(f"{sum(hums) / len(hums):.0f}%" if hums else "—")
        self.m_alert.setText(str(alerts))
        self.m_alert.setStyleSheet(f"color: {ALERT if alerts else OK};")
        self.m_sensors.setText(f"{online}/{TOTAL}")
        self.m_sensors.setStyleSheet(f"color: {OK if online == TOTAL else WARN};")
        self.clock.setText(datetime.now().strftime("%H:%M:%S  ·  %d.%m"))


class RackDetailPage(QWidget):
    def __init__(self, source, on_back):
        super().__init__()
        self.setObjectName("page")
        self.source = source
        self.rack = 1

        back = QPushButton("←  Стелажі")
        back.setObjectName("back")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(on_back)
        self.title = QLabel("Стелаж 1")
        self.title.setObjectName("h1")
        top = QHBoxLayout()
        top.addWidget(back)
        top.addStretch()
        top.addWidget(self.title)

        self.cards = {}
        cards_lay = QVBoxLayout()
        cards_lay.setSpacing(12)
        # top of screen = top floor (Верхній), bottom = Нижній — matches the rack
        for f in range(FLOORS, 0, -1):
            card = FloorCard(f)
            self.cards[f] = card
            cards_lay.addWidget(card, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(14)
        lay.addLayout(top)
        lay.addLayout(cards_lay, 1)

    def set_rack(self, rack: int):
        self.rack = rack
        self.title.setText(f"Стелаж {rack}")
        self.refresh()

    def refresh(self):
        for f, card in self.cards.items():
            r = self.source.reading(self.rack, f)
            if r is None:
                card.update_offline()
            else:
                temps, hums = self.source.history(self.rack, f)
                card.update_values(r, temps, hums)


class InfoPage(QWidget):
    """Drill-down for the top chips: alerts, temp/humidity extremes, offline."""

    TITLES = {"alerts": "Тривоги", "temp": "Температура по фермі",
              "hum": "Вологість по фермі", "offline": "Зв'язок з датчиками"}

    def __init__(self, source, on_back):
        super().__init__()
        self.setObjectName("page")
        self.source = source
        self.mode = None
        self._cards = []

        back = QPushButton("←  Огляд")
        back.setObjectName("back")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(on_back)
        self.title = QLabel("")
        self.title.setObjectName("h1")
        top = QHBoxLayout()
        top.addWidget(back)
        top.addStretch()
        top.addWidget(self.title)

        self.cards_lay = QVBoxLayout()
        self.cards_lay.setSpacing(12)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(14)
        lay.addLayout(top)
        lay.addLayout(self.cards_lay)
        lay.addStretch(1)

    def show_mode(self, mode: str):
        self.mode = mode
        self.title.setText(self.TITLES.get(mode, ""))
        self.refresh()

    def _ensure_cards(self, n):
        while len(self._cards) < n:
            card = InfoCard()
            self._cards.append(card)
            self.cards_lay.addWidget(card)
        for i, card in enumerate(self._cards):
            card.setVisible(i < n)

    def _rows(self):
        rows = []
        if self.mode == "alerts":
            items = []
            for r in range(1, RACKS + 1):
                for f in range(1, FLOORS + 1):
                    rd = self.source.reading(r, f)
                    if rd is not None and status_color(rd.temperature, rd.humidity) == ALERT:
                        items.append((r, f, rd))
            items.sort(key=lambda x: x[2].temperature, reverse=True)
            if not items:
                rows.append(("Немає тривог", "0", "усі стелажі в нормі", OK))
            else:
                for r, f, rd in items:
                    rows.append((
                        f"Стелаж {r} · {FLOOR_NAMES[f]}",
                        f"{rd.temperature:.1f}°",
                        f"вологість {rd.humidity:.0f}%",
                        ALERT,
                    ))
        elif self.mode in ("temp", "hum"):
            vals = []
            for r in range(1, RACKS + 1):
                for f in range(1, FLOORS + 1):
                    rd = self.source.reading(r, f)
                    if rd is None:
                        continue
                    metric = rd.temperature if self.mode == "temp" else rd.humidity
                    vals.append((metric, r, f, rd))
            if not vals:
                rows.append(("Немає даних", "—", "усі датчики offline", OFFLINE))
            else:
                hi = max(vals, key=lambda x: x[0])
                lo = min(vals, key=lambda x: x[0])
                for heading, (_, r, f, rd) in (("Найвища", hi), ("Найнижча", lo)):
                    value = f"{rd.temperature:.1f}°" if self.mode == "temp" else f"{rd.humidity:.0f}%"
                    rows.append((
                        heading, value, f"Стелаж {r} · {FLOOR_NAMES[f]}",
                        status_color(rd.temperature, rd.humidity),
                    ))
        elif self.mode == "offline":
            offs = [(r, f) for r in range(1, RACKS + 1) for f in range(FLOORS, 0, -1)
                    if self.source.reading(r, f) is None]
            if not offs:
                rows.append(("Усі датчики онлайн", f"{TOTAL}/{TOTAL}", "на зв'язку", OK))
            else:
                for r, f in offs:
                    rows.append((f"Стелаж {r} · {FLOOR_NAMES[f]}", "—",
                                 "немає зв'язку з датчиком", OFFLINE))
        return rows

    def refresh(self):
        rows = self._rows()
        self._ensure_cards(len(rows))
        for card, (heading, value, sub, color) in zip(self._cards, rows):
            card.update_card(heading, value, sub, color)


# ============================================================================
#  Main window
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.alerts = AlertManager(source)
        self.logger = ReadingsLogger(source)
        self.setWindowTitle("Farm climate monitor")
        geo = QApplication.primaryScreen().availableGeometry()
        self.resize(min(560, int(geo.width() * 0.85)),
                    min(940, int(geo.height() * 0.85)))

        for seq, fn in (("F11", self.toggle_fullscreen), ("F", self.toggle_fullscreen),
                        ("Escape", self.exit_fullscreen)):
            QShortcut(QKeySequence(seq), self).activated.connect(fn)

        self.stack = QStackedWidget()
        self.overview = OverviewPage(source, self.open_rack, self.open_info, self.shutdown)
        self.detail = RackDetailPage(source, self.show_overview)
        self.info = InfoPage(source, self.show_overview)
        for w in (self.overview, self.detail, self.info):
            self.stack.addWidget(w)

        self.setCentralWidget(self.stack)

        self.overview.refresh()
        self.alerts.evaluate()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)

    def tick(self):
        self.source.step()
        self.alerts.evaluate()
        self.logger.tick()
        cur = self.stack.currentWidget()
        if cur is self.detail:
            self.detail.refresh()
        elif cur is self.info:
            self.info.refresh()
        else:
            self.overview.refresh()

    def open_rack(self, rack: int):
        self.detail.set_rack(rack)
        self.stack.setCurrentWidget(self.detail)

    def open_info(self, mode: str):
        self.info.show_mode(mode)
        self.stack.setCurrentWidget(self.info)

    def show_overview(self):
        self.overview.refresh()
        self.stack.setCurrentWidget(self.overview)

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()

    def shutdown(self):
        box = QMessageBox(self)
        box.setWindowTitle("Вимкнення")
        box.setText("Вимкнути Raspberry Pi?")
        box.setIcon(QMessageBox.Icon.Question)
        yes = box.addButton("Так, вимкнути", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self._poweroff()

    @staticmethod
    def _poweroff():
        for cmd in (["sudo", "-n", "poweroff"], ["systemctl", "poweroff"],
                    ["sudo", "-n", "shutdown", "-h", "now"]):
            try:
                if subprocess.run(cmd, timeout=5).returncode == 0:
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue


QSS = f"""
* {{ font-family: "Segoe UI", "DejaVu Sans", sans-serif; }}
QLabel {{ background: transparent; color: {TXT}; }}
QMainWindow, QStackedWidget {{ background: {BG}; }}
#page {{ background: {BG}; }}
#h1 {{ font-size: 22px; font-weight: 600; }}
#clock {{ color: {MUT}; font-size: 15px; }}
#chip {{ background: {CARD2}; border-radius: 12px; }}
#chipVal {{ font-size: 22px; font-weight: 600; }}
#chipCap {{ color: {MUT}; font-size: 12px; }}
#tile {{ background: {CARD}; border: 2px solid {LINE}; border-radius: 16px; }}
#tileTitle {{ color: {MUT}; font-size: 16px; }}
#tileTemp {{ font-size: 40px; font-weight: 600; }}
#tileSub {{ color: {MUT}; font-size: 14px; }}
#tileState {{ font-size: 14px; }}
#floorCard {{ background: {CARD}; border: 2px solid {LINE}; border-radius: 16px; }}
#floorTitle {{ color: {TXT}; font-size: 18px; font-weight: 600; }}
#floorTemp {{ font-size: 34px; font-weight: 600; }}
#back {{ background: {CARD2}; border: none; border-radius: 10px;
         padding: 8px 16px; font-size: 15px; color: {TXT}; }}
#back:hover {{ background: {LINE}; }}
#power {{ background: {CARD2}; border: 1px solid {ALERT}; border-radius: 10px;
          padding: 8px 16px; font-size: 14px; color: {ALERT}; }}
#power:hover {{ background: {ALERT}; color: #ffffff; }}
QMessageBox, QDialog {{ background: {CARD}; }}
QMessageBox QLabel {{ color: {TXT}; font-size: 16px; }}
QMessageBox QPushButton {{ background: {CARD2}; color: {TXT}; border: 1px solid {LINE};
                           border-radius: 8px; padding: 8px 18px; font-size: 15px; }}
QMessageBox QPushButton:hover {{ background: {LINE}; }}
"""


def make_source():
    # Default: use real sensors when the hardware stack is available; fall back
    # to simulation (e.g. on a dev PC without Blinka). Force sim with --sim.
    if "--sim" in sys.argv:
        print("[mode] SIMULATION (--sim)")
        return SimulatedDataSource()
    force_hw = bool({"--hardware", "--real", "--live"} & set(sys.argv))
    try:
        source = HardwareDataSource()
        print("[mode] HARDWARE - real sensors")
        return source
    except Exception as exc:
        if force_hw:
            print(f"[hardware] init failed: {exc}")
            print("[hardware] check I2C (i2cdetect -y 1), wiring and dependencies.")
            sys.exit(1)
        print(f"[mode] SIMULATION (hardware unavailable: {exc})")
        return SimulatedDataSource()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in the terminal quits
    if CONFIG_ERROR:
        print(f"[config] config.json problem, using defaults: {CONFIG_ERROR}")
    print(f"[config] {RACKS} racks x {FLOORS} floors = {TOTAL} sensors")
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = MainWindow(make_source())
    if {"--fullscreen", "--kiosk"} & set(sys.argv):
        win.showFullScreen()
    else:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
