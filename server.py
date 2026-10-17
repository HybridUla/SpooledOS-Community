#!/usr/bin/env python3
# SpooledOS Community - a touchscreen dash for a DS1-tuned car on a Raspberry Pi
# Copyright (C) 2026 Spooled Racing
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY; see LICENSE for the details.

import argparse
import base64
import json
import math
import os
import random
import re
import signal
import socket
import struct
import subprocess
import threading
import time
import csv
import gzip
import itertools
import urllib.request
import zoneinfo
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("SPOOLED_LOG_DIR", os.path.join(HERE, "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(HERE, "settings.json")

DEFAULT_SETTINGS = {
    "ds1_host": "ds1.local",
    "ds1_hosts_extra": ["192.168.4.1", "192.168.50.10", "ds1"],
    "units": "F",
    "autolog": True,
    "wifi_ssid": "Spooled",
    "wifi_pass": "SpooledDash",
    "debug": False,
    "brightness": 100,
    "tank_gal": 0,
    "e85_pct": 85,
    "pump_pct": 10,
    "blend_target": 50,
    "log_mask": "",
    "gauges": {},
    "map_names": [""] * 8,
}

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s

def save_settings(s):
    write_json_atomic(SETTINGS_FILE, s)

SETTINGS = load_settings()

STATE_DIR = os.path.expanduser("~/.spooled")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
DEFAULT_STATE = {"setup_done": False, "tour_done": False, "seen_version": ""}

def load_state():
    s = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE) as f:
            s.update(json.load(f))
    except Exception:
        pass
    if not s.get("setup_done") and SETTINGS.get("setup_done"):
        s["setup_done"] = True
    return s

def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    d = os.path.dirname(path) or "."
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            fd = os.open(d, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass
        return True
    except Exception as e:
        print("atomic write error:", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def save_state(s):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        write_json_atomic(STATE_FILE, s)
    except Exception as e:
        print("state save error:", e)

APPSTATE = load_state()


def parse_changelog(text=None):
    out, cur = {}, None
    try:
        if text is None:
            with open(os.path.join(HERE, "CHANGELOG.md")) as f:
                text = f.read()
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                cur = s[3:].strip().split()[0]
                out[cur] = []
            elif cur and (s.startswith("- ") or s.startswith("* ")):
                out[cur].append(s[2:].strip())
    except Exception:
        pass
    return out


def changelog_from_release(tag):
    ok, text = _run(["git", "-C", HERE, "show", "%s:CHANGELOG.md" % tag], timeout=10)
    return parse_changelog(text).get(tag, []) if ok else []


def changelog_for(version):
    if not version:
        return []
    cl = parse_changelog()
    base = version.split("-")[0]
    return cl.get(version) or cl.get(base) or []

NETMODE_SH = os.path.join(HERE, "netmode.sh")
UPDATE_SH = os.path.join(HERE, "update.sh")

UPDATE = {"stage": "idle", "message": "", "done": False,
          "ok": False, "version": "", "reboot": False}


def _run(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return False, "helper not available on this machine"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


def plain_wifi_error(out, ssid=""):
    low = (out or "").lower()
    name = ssid or "that network"
    if "no saved network" in low:
        return "There's no saved network yet. Join one first."
    if any(s in low for s in ("sudo", "not available on this machine",
                              "command not found", "not authorized",
                              "permission")):
        return ("This dash can't change its WiFi settings. Run the installer again "
                "to repair its permissions.")
    if "psk: property is invalid" in low:
        return "WiFi passwords are 8 to 63 characters. Check it and try again."
    if "ssid: property is invalid" in low:
        return "That network name is too long."
    if any(s in low for s in ("secrets were required", "no secrets",
                              "802-1x", "supplicant")):
        return "Wrong password for %s. Check it and try again." % name
    if any(s in low for s in ("could not be found", "no network with ssid",
                              "no suitable device", "not found")):
        return "Couldn't find %s. Make sure it's on and in range." % name
    if any(s in low for s in ("ip configuration", "ip-config", "dhcp")):
        return ("Joined %s, but it didn't give the dash an address. "
                "Try again or use another network." % name)
    if "timed out" in low or "timeout" in low:
        return "%s didn't answer. Move closer and try again." % (ssid or "The network")
    if any(s in low for s in ("not ready", "unavailable", "unmanaged")):
        return "WiFi isn't ready yet. Wait a few seconds and try again."
    return "Couldn't join %s. Check the name and password and try again." % name


def plain_update_error(out):
    low = (out or "").lower()
    if any(s in low for s in ("no internet", "could not resolve", "unable to access",
                              "timed out", "network is unreachable",
                              "connection refused", "could not read from remote")):
        return "No internet connection. Connect the dash to WiFi and try again."
    return ("The update didn't install. Try again; if it keeps happening, see "
            "Troubleshooting in the SpooledOS Community guide.")


BACKLIGHT_DIR = "/sys/class/backlight"
BRIGHT_MIN = 10
BRIGHT = {"mode": "screen"}


def set_brightness(pct):
    try:
        pct = int(round(float(pct)))
    except (TypeError, ValueError):
        pct = 100
    pct = max(BRIGHT_MIN, min(100, pct))
    try:
        names = sorted(os.listdir(BACKLIGHT_DIR))
    except OSError:
        names = []
    for n in names:
        base = os.path.join(BACKLIGHT_DIR, n)
        mx = _read(os.path.join(base, "max_brightness"), int, 0)
        if not mx:
            continue
        try:
            with open(os.path.join(base, "brightness"), "w") as f:
                f.write(str(max(1, int(round(mx * pct / 100.0)))))
            BRIGHT["mode"] = "backlight"
            return pct, "backlight"
        except OSError:
            continue
    BRIGHT["mode"] = "screen"
    return pct, "screen"


L_PER_GAL = 3.785411784
TANK_BY_PLATFORM = {
    "4G": (19.8, "C7 A6/A7/S6/S7/RS7"),
    "4H": (21.7, "D4 A8/S8"),
}


def blend_status():
    with LOCK:
        live = bool(STATE.get("connected"))
        eth = float(TELEMETRY.get("ethanol", 0.0) or 0.0)
        tank_l = float(TELEMETRY.get("tank", 0.0) or 0.0)
        present = set(DERIVED_PRESENT)
        ecu = str((STATE["ds1"].get("device") or {}).get("ecu", "") or "")

    def has(key, val):
        return (key in present) if present else bool(val)

    m = re.search(r"\b(4[GH])\d{7}", ecu)
    auto_gal, car = TANK_BY_PLATFORM.get(m.group(1) if m else "", (None, ""))
    return {
        "live": live,
        "ethanol": round(eth, 1) if live and has("ethanol", eth) else None,
        "tank_gal": round(tank_l / L_PER_GAL, 2) if live and has("tank", tank_l) else None,
        "tank_auto_gal": auto_gal,
        "car": car,
    }


def wifi_powersave_off():
    for con in ("spooled-ap", "spooled-client"):
        ok, cur = _run(["nmcli", "-g", "802-11-wireless.powersave",
                        "connection", "show", con], timeout=8)
        if not ok or "disable" in cur or cur.strip().startswith("2"):
            continue
        ok, out = _run(["sudo", "-n", "nmcli", "connection", "modify", con,
                        "802-11-wireless.powersave", "2"], timeout=10)
        print("[wifi] power saving off on %s: %s" % (con, "ok" if ok else out))


def net_status():
    ok, out = _run(["bash", NETMODE_SH, "status"], timeout=8)
    d = {"ok": ok, "role": "unknown", "ssid": "", "ip": "", "internet": "no"}
    if ok:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    else:
        d["error"] = out
    return d


def net_scan():
    ok, out = _run(["bash", NETMODE_SH, "scan"], timeout=15)
    nets = []
    if ok:
        for line in out.splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                sig = parts[1] if len(parts) > 1 and parts[1].isdigit() else "0"
                nets.append({"ssid": parts[0], "signal": int(sig)})
    return {"ok": ok, "networks": nets, "error": None if ok else out}


BOOT_T = time.time()

THROTTLE_BITS = [
    (0,  "now",  "Undervoltage"),
    (1,  "now",  "ARM frequency capped"),
    (2,  "now",  "Throttled"),
    (3,  "now",  "Soft temp limit"),
    (16, "ever", "Undervoltage"),
    (17, "ever", "ARM frequency capped"),
    (18, "ever", "Throttled"),
    (19, "ever", "Soft temp limit"),
]


def _read(path, cast=str, default=None):
    try:
        with open(path) as f:
            return cast(f.read().strip())
    except Exception:
        return default


def _vcgencmd(arg):
    ok, out = _run(["vcgencmd"] + arg.split(), timeout=3)
    return out if ok else ""


def debug_stats():
    d = {"ok": True, "at": time.time()}

    milli = _read("/sys/class/thermal/thermal_zone0/temp", int, None)
    d["temp_c"] = round(milli / 1000.0, 1) if milli else None
    khz = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", int, None)
    d["cpu_mhz"] = round(khz / 1000) if khz else None
    mx = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", int, None)
    d["cpu_max_mhz"] = round(mx / 1000) if mx else None

    volt = _vcgencmd("measure_volts core")
    m = re.search(r"([\d.]+)V", volt or "")
    d["core_v"] = float(m.group(1)) if m else None

    thr = _vcgencmd("get_throttled")
    m = re.search(r"0x([0-9a-fA-F]+)", thr or "")
    if m:
        bits = int(m.group(1), 16)
        d["throttled_raw"] = "0x%x" % bits
        d["throttle_now"] = [lbl for b, when, lbl in THROTTLE_BITS
                             if when == "now" and bits & (1 << b)]
        d["throttle_ever"] = [lbl for b, when, lbl in THROTTLE_BITS
                              if when == "ever" and bits & (1 << b)]
    else:
        d["throttled_raw"] = None
        d["throttle_now"] = d["throttle_ever"] = []

    d["fan"] = None
    try:
        for n in sorted(os.listdir("/sys/class/thermal")):
            if not n.startswith("cooling_device"):
                continue
            base = "/sys/class/thermal/" + n
            kind = _read(base + "/type", str, "") or ""
            if "fan" not in kind.lower() and "gpio" not in kind.lower():
                continue
            cur = _read(base + "/cur_state", int, None)
            mxs = _read(base + "/max_state", int, None)
            d["fan"] = {"type": kind, "state": cur, "max_state": mxs,
                        "on": bool(cur)}
            break
    except Exception:
        pass

    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.split()[0]) * 1024
    except Exception:
        pass
    d["mem_total"] = mem.get("MemTotal")
    d["mem_avail"] = mem.get("MemAvailable")
    d["mem_used"] = ((mem["MemTotal"] - mem["MemAvailable"])
                     if "MemTotal" in mem and "MemAvailable" in mem else None)
    d["swap_used"] = ((mem["SwapTotal"] - mem["SwapFree"])
                      if "SwapTotal" in mem and "SwapFree" in mem else None)

    up = _read("/proc/uptime", str, "") or ""
    d["uptime_s"] = int(float(up.split()[0])) if up else None
    d["backend_uptime_s"] = int(time.time() - BOOT_T)
    la = _read("/proc/loadavg", str, "") or ""
    d["load"] = [float(x) for x in la.split()[:3]] if la else None
    d["cpus"] = os.cpu_count()

    try:
        import shutil
        t, u, f = shutil.disk_usage(LOG_DIR)
        d["disk_total"], d["disk_used"], d["disk_free"] = t, u, f
    except Exception:
        d["disk_total"] = d["disk_used"] = d["disk_free"] = None

    ok, mnt = _run(["findmnt", "-no", "OPTIONS", "/"], timeout=3)
    d["root_ro"] = bool(ok and re.search(r"\bro\b", mnt or ""))

    ok, ver = _run(["git", "-C", HERE, "describe", "--tags", "--always"], timeout=5)
    d["version"] = ver if ok else None
    ok, dirty = _run(["git", "-C", HERE, "status", "--porcelain"], timeout=5)
    d["dirty"] = bool(ok and (dirty or "").strip())
    d["host"] = socket.gethostname()
    d["clock"] = clock_status()

    try:
        d["ds1"] = {"connected": bool(getattr(DS1, "connected", False)),
                    "host": getattr(DS1, "host", None),
                    "monitors": len(getattr(WS, "layout", []) or []),
                    "last_error": getattr(DS1, "last_error", "") or ""}
    except Exception:
        d["ds1"] = None

    try:
        names = [n for n in os.listdir(LOG_DIR) if n.endswith(".csv")]
        d["logs"] = {"count": len(names),
                     "bytes": sum(os.path.getsize(os.path.join(LOG_DIR, n))
                                  for n in names),
                     "active": bool(getattr(LOGGER, "active", False))}
    except Exception:
        d["logs"] = None

    try:
        d["dtc_table"] = dtc_table_status()
    except Exception:
        d["dtc_table"] = None

    return d


def storage_info():
    out = {"ok": False}
    try:
        import shutil
        total, used, free = shutil.disk_usage(LOG_DIR)
        logs_bytes, logs_count = 0, 0
        for n in os.listdir(LOG_DIR):
            if n.endswith(".csv"):
                try:
                    logs_bytes += os.path.getsize(os.path.join(LOG_DIR, n))
                    logs_count += 1
                except OSError:
                    pass
        chans = max(15, len(WS.layout) or 15)
        per_sec = 7.8 * chans * 50
        out = {"ok": True, "total": total, "used": used, "free": free,
               "used_pct": (used / total) if total else 0,
               "logs_bytes": logs_bytes, "logs_count": logs_count,
               "hours_left": (free / per_sec / 3600) if per_sec else 0,
               "channels": chans}
    except Exception as e:
        out["error"] = str(e)
    return out


def run_update(ref=""):
    def stage(name, msg, **kw):
        UPDATE["stage"] = name
        UPDATE["message"] = msg
        for k, v in kw.items():
            UPDATE[k] = v
        print(f"[update] {name}: {msg}")

    UPDATE.update({"stage": "start", "message": "Starting…", "done": False,
                   "ok": False, "version": "", "reboot": False})
    try:
        stage("download", "Downloading the update…")
        args = ["bash", UPDATE_SH, "--no-restart"] + ([ref] if ref else [])
        ok, out = _run(args, timeout=600)
        tail = "\n".join(out.strip().splitlines()[-3:]) if out else ""

        if not ok:
            print("[update] failed:", tail)
            stage("error", plain_update_error(out), done=True, ok=False)
            return

        stage("install", "Installing…", version=app_version())
        time.sleep(1.0)
        if _run(["which", "systemctl"], timeout=5)[0]:
            stage("reboot", "Restarting…", reboot=True)
            time.sleep(1.5)
            _run(["sudo", "-n", "reboot"], timeout=20)
            time.sleep(5)
            stage("error", "Updated, but the device could not restart itself. "
                           "Power it off and on to finish.", done=True, ok=False)
            return
        else:
            stage("done", "Update complete", done=True, ok=True,
                  version=app_version())
    except Exception as e:
        print("[update] error:", e)
        stage("error", plain_update_error(str(e)), done=True, ok=False)


def app_version():
    ok, out = _run(["git", "-C", HERE, "describe", "--tags", "--always"], timeout=5)
    if ok and out:
        return out
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip()
    except Exception:
        return "unknown"

TELEMETRY = {
    "t": 0.0, "rpm": 0, "boost": 0.0, "afr": 0.0, "timing": 0.0,
    "iat": 0.0, "egt": 0.0, "coolant": 0.0, "oilT": 0.0, "wg": 0.0,
    "ethanol": 0.0, "speed": 0.0, "gear": 0, "pedal": 0.0, "volts": 0.0,
    "lambda": 0.0, "lpf": 0.0, "hpf": 0.0, "knk": 0.0, "load": 0.0,
    "dinj": 0.0, "pinj": 0.0, "thr_raw": 0.0, "ign": 0.0,
    "gLat": 0.0, "gLong": 0.0, "mil": 0.0, "da": 0.0,
    "tgt": 0.0,
    "tank": 0.0,
}
STATE = {"map": 0, "connected": False, "recording": "idle", "source": "disconnected",
         "internet": False,
         "ds1": {"connected": False, "host": None, "url": None,
                 "monitors": 0, "last_error": "", "since": None, "device": {},
                 "streaming": False, "stream_channels": 0, "ws_host": None,
                 "paused": False},
         "faults": []}
MONITORS = {}
MAP_SLOTS = 8
MAP_NAME_MAX = 24
LOCK = threading.Lock()


def clean_map_names(v):
    v = v if isinstance(v, list) else []
    out = []
    for i in range(MAP_SLOTS):
        n = v[i] if i < len(v) and isinstance(v[i], str) else ""
        out.append(" ".join(n.split())[:MAP_NAME_MAX])
    return out


def map_list():
    names = clean_map_names(SETTINGS.get("map_names"))
    return [{"index": i, "name": names[i] or "Map %d" % i}
            for i in range(MAP_SLOTS)]


class SimSource:
    def __init__(self):
        self.t = 0.0
        self.throttle = 0.0
        self.throttle_target = 0.0
        self.gear = 2
        self.rpm = 850.0
        self.speed = 0.0
        self.scenario = "idle"
        self.scenario_t = 4.0

    def _pick(self):
        r = random.random()
        if r < 0.40:
            self.scenario, self.scenario_t = "pull", 5 + random.random() * 3
            self.gear = 3 if random.random() < 0.5 else 4
            self.rpm = 2600
        elif r < 0.65:
            self.scenario, self.scenario_t = "cruise", 4 + random.random() * 5
        elif r < 0.85:
            self.scenario, self.scenario_t = "partial", 3 + random.random() * 3
        else:
            self.scenario, self.scenario_t = "idle", 2 + random.random() * 3

    def step(self, dt):
        self.t += dt
        self.scenario_t -= dt
        if self.scenario_t <= 0:
            self._pick()
        s = self.scenario
        if s == "idle":
            self.throttle_target = 0
        elif s == "cruise":
            self.throttle_target = 18 + math.sin(self.t * 0.6) * 8
        elif s == "partial":
            self.throttle_target = 45 + math.sin(self.t * 1.3) * 15
        else:
            self.throttle_target = 100
        self.throttle += (self.throttle_target - self.throttle) * min(1, dt * 6)
        wot = self.throttle > 80
        dr = (self.throttle * 70 - 300) - (self.rpm - 850) * 0.55
        self.rpm += dr * dt
        if self.rpm > 7000:
            self.rpm = 3200
            self.gear = min(6, self.gear + 1)
        if self.rpm < 1100 and self.throttle < 10:
            self.rpm = 850 + random.random() * 80
        self.rpm = max(750, min(7100, self.rpm))
        gear_ratio = [0, 95, 56, 38, 29, 23, 19][self.gear] if self.gear < 7 else 19
        self.speed += ((self.rpm / gear_ratio) - self.speed) * min(1, dt * 1.5)
        self.speed = max(0, self.speed)
        load = self.throttle / 100.0
        with LOCK:
            T = TELEMETRY
            T["t"] = self.t
            T["rpm"] = self.rpm
            T["pedal"] = self.throttle
            T["gear"] = 0 if (self.throttle < 3 and self.speed < 2) else self.gear
            T["speed"] = self.speed
            T["boost"] = (18 + load * 10 + math.sin(self.t * 8) * 0.8) if wot else (load * 14 - (1 - load) * 8)
            T["tgt"] = (18.5 + load * 10) if wot else 0.0
            T["afr"] = (11.6 + math.sin(self.t * 6) * 0.15) if wot else (14.7 - load * 1.5)
            T["timing"] = (7 + random.random() * 1.5) if wot else (14 - load * 4)
            T["iat"] = 28 + load * 18 + math.sin(self.t * 0.3) * 3
            T["egt"] = 300 + load * 580 + (math.sin(self.t * 4) * 30 if wot else 0)
            T["coolant"] = 88 + math.sin(self.t * 0.15) * 3
            T["oilT"] = 95 + load * 25 + math.sin(self.t * 0.1) * 4
            T["wg"] = (35 + math.sin(self.t * 5) * 20) if wot else (60 + load * 20)
            T["ethanol"] = 40.0 + math.sin(self.t * 0.05) * 1.5
            T["tank"] = 53.0 - (self.t * 0.002) % 30.0
            T["lambda"] = T["afr"] / 14.7
            T["load"] = 15 + load * 82
            T["lpf"] = 4.5 + load * 2.5
            T["hpf"] = 8 + load * 14
            T["knk"] = (-(0.5 + random.random() * 1.5)) if wot else 0.0
            T["thr_raw"] = self.throttle
            T["ign"] = T["timing"]
            T["dinj"] = 1.0 + load * 3.2
            T["pinj"] = 0.0
            T["volts"] = 13.8 + math.sin(self.t * 0.7) * 0.25
            T["da"] = 1180 + math.sin(self.t * 0.08) * 120
            T["mil"] = 0.0
            T["gLat"] = math.sin(self.t * 0.9) * 0.4
            T["gLong"] = (load * 0.8) if wot else (math.sin(self.t) * 0.2)
        return TELEMETRY


NAME_MAP = {
    "rpm":    ["nmot", "rpm", "engine_speed", "epm_neng"],
    "boost":  ["boost", "ladedruck", "pmanifold", "map_psi", "pushup"],
    "afr":    ["afr", "lambda", "lam_w", "lambda1"],
    "timing": ["timing", "ignition", "zwout", "ign_"],
    "iat":    ["iat", "tia", "intake_air", "tans"],
    "egt":    ["egt", "taikr", "tabg"],
    "coolant":["coolant", "tmot", "ect"],
    "oilT":   ["oil_tem", "toil", "oiltemp", "oil_t"],
    "wg":     ["wgfinal", "wgd", "wastegate", "wg_"],
    "ethanol":["ethanol", "e_content", "eth_"],
    "pedal":  ["pedal", "ped_w", "throttle", "tps", "accel"],
    "speed":  ["vehicle_speed", "vfzg", "vspeed", "speed"],
    "gear":   ["gear", "gang"],
    "volts":  ["ubatt", "vbat", "batt", "uadc", "volt"],
}


def _http_get(url, timeout=1.2):
    req = urllib.request.Request(url, headers={"User-Agent": "Spooled"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_device_info(text):
    line = (text.strip().splitlines() or [""])[0]
    parts = [p.strip() for p in line.split(",")]
    info = {"firmware": "", "vin": "", "ecu": "", "flash_status": "",
            "tune": "", "last_flash": ""}
    for p in parts:
        low = p.lower()
        if low.startswith("ds1 firmware:"):
            info["firmware"] = p.split(":", 1)[1].strip()
        elif low.startswith("last flash:"):
            path = p.split(":", 1)[1].strip()
            info["last_flash"] = path
            base = path.rsplit("/", 1)[-1]
            m = re.search(r"MED17_1_1_(.+?)\.(?:ben|bin)$", base, re.I)
            info["tune"] = m.group(1) if m else re.sub(r"\.(ben|bin)$", "", base, flags=re.I)
        elif re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", p):
            info["vin"] = p
        elif p in ("Modified", "Stock", "Original", "Unmodified"):
            info["flash_status"] = p
        elif "MED17" in p.upper() or low.startswith("vag"):
            info["ecu"] = p
    return info


def first_status_line(text):
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.rstrip("\r\n")
    return ""


def parse_status_csv(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out = {}
    if not lines:
        return out
    if len(lines) == 2 and lines[0].count(",") == lines[1].count(",") and lines[0].count(",") > 1:
        names = [c.strip() for c in lines[0].split(",")]
        vals = [c.strip() for c in lines[1].split(",")]
        for n, v in zip(names, vals):
            try:
                out[n] = float(v)
            except ValueError:
                pass
        return out
    for ln in lines:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[-1])
            except ValueError:
                pass
    return out


def map_status_to_telemetry(status):
    low = {k.lower(): v for k, v in status.items()}
    updated = {}
    for field, cands in NAME_MAP.items():
        for c in cands:
            hit = next((v for k, v in low.items() if c in k), None)
            if hit is not None:
                updated[field] = hit
                break
    if "afr" in updated and updated["afr"] < 3:
        updated["afr"] = updated["afr"] * 14.7
    return updated


DS1_ADDR = {"ip": None, "name": None}


def _split_host(h):
    h = (h or "").replace("http://", "").replace("https://", "").split("/")[0]
    if h.count(":") == 1:
        name, port = h.split(":")
        try:
            return name, int(port)
        except ValueError:
            return name, 80
    return h, 80


def _resolve(name, timeout=3.0):
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", name or ""):
        return name
    out = []

    def work():
        try:
            out.append(socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0])
        except Exception:
            pass
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


def ds1_lease_ips():
    ips = []
    base = "/var/lib/NetworkManager"
    try:
        names = [n for n in os.listdir(base)
                 if n.startswith("dnsmasq-") and n.endswith(".leases")]
    except OSError:
        return ips
    for n in names:
        try:
            with open(os.path.join(base, n)) as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 4 and "ds1" in p[3].lower():
                        ips.append(p[2])
        except OSError:
            pass
    return ips


def ds1_candidates():
    seen = set()
    if DS1_ADDR["ip"]:
        seen.add((DS1_ADDR["ip"], DS1_ADDR.get("port", 80)))
        yield (DS1_ADDR["name"] or DS1_ADDR["ip"], DS1_ADDR["ip"],
               DS1_ADDR.get("port", 80))
    raw = [DS1.force_host, SETTINGS.get("ds1_host")] + \
        list(SETTINGS.get("ds1_hosts_extra", []) or [])
    for h in raw:
        if not h:
            continue
        name, port = _split_host(h)
        ip = _resolve(name)
        if ip and (ip, port) not in seen:
            seen.add((ip, port))
            yield (name, ip, port)
    for ip in ds1_lease_ips():
        if (ip, 80) not in seen:
            seen.add((ip, 80))
            yield ("ds1 (lease)", ip, 80)


def ds1_found(label, ip, port=80):
    DS1_ADDR.update(ip=ip, name=label, port=port)


class DS1Manager(threading.Thread):
    daemon = True
    POLL_SEC = 4.0
    LOST_AFTER = 3

    def __init__(self):
        super().__init__()
        self.running = True
        self.connected = False
        self.host = None
        self.force_host = None
        self.want = True
        self.first_try_done = False

    def try_connect(self):
        for label, ip, port in ds1_candidates():
            base = "http://%s" % ip + (":%d" % port if port != 80 else "")
            try:
                text = _http_get(base + "/worm/monitors.csv", timeout=4.0)
                self._load_monitors(text)
                self.host = base
                self.connected = True
                ds1_found(label, ip, port)
                device = {}
                status_line = ""
                try:
                    status_text = _http_get(base + "/ram/status.csv", timeout=4.0)
                    device = parse_device_info(status_text)
                    status_line = first_status_line(status_text)
                except Exception:
                    pass
                with LOCK:
                    STATE["ds1"].update(connected=True, host=label, url=base,
                                        monitors=len(MONITORS), last_error="",
                                        since=datetime.now().strftime("%H:%M:%S"),
                                        device=device)
                    if status_line:
                        STATE["ds1"]["status_line"] = status_line
                        STATE["ds1"]["status_raw"] = status_text
                if not WS.streaming:
                    run_dtc_import_if_needed(base, device.get("firmware", ""))
                print(f"[ds1] connected: {label} at {base} ({len(MONITORS)} monitors)"
                      + (f" VIN {device.get('vin')} tune '{device.get('tune')}'" if device else ""))
                return True
            except Exception as e:
                with LOCK:
                    STATE["ds1"]["last_error"] = f"{label}: {e.__class__.__name__}"
        return False

    def _load_monitors(self, text):
        MONITORS.clear()
        rdr = csv.reader(text.splitlines())
        header = next(rdr, None)
        for row in rdr:
            if not row or not row[0] or row[0].startswith("#"):
                continue
            name = row[0].strip()
            readable = row[1].strip() if len(row) > 1 else name
            units = row[2].strip() if len(row) > 2 else ""
            MONITORS[name] = {"readable": readable, "units": units}

    def poll(self):
        try:
            text = _http_get(self.host + "/ram/status.csv", timeout=4.0)
            line = first_status_line(text)
            if line.lower().startswith("ds1 firmware"):
                with LOCK:
                    STATE["ds1"]["status_line"] = line
                    STATE["ds1"]["status_raw"] = text
            return True
        except Exception as e:
            with LOCK:
                STATE["ds1"]["last_error"] = f"poll: {e.__class__.__name__}"
            return False

    def run(self):
        fails = 0
        while self.running:
            if not self.want:
                time.sleep(0.5); continue
            if not self.connected:
                ok = self.try_connect()
                self.first_try_done = True
                if not ok:
                    time.sleep(3.0)
                    continue
                fails = 0
            if WS.streaming and WS.rx_fresh():
                fails = 0
                time.sleep(1.0)
                continue
            ok = self.poll()
            fails = 0 if ok else fails + 1
            if fails >= self.LOST_AFTER:
                self.connected = False
                with LOCK:
                    STATE["ds1"]["connected"] = False
                print("[ds1] lost connection")
            time.sleep(self.POLL_SEC)

    def connect(self, host=None):
        self.force_host = host
        self.want = True
        self.connected = False

    def disconnect(self):
        self.want = False
        self.connected = False
        with LOCK:
            STATE["ds1"]["connected"] = False


DS1 = DS1Manager()


CAN_VALUES = {}


class CANDecoder:

    def __init__(self, path):
        self.path = path
        self.frames = {}
        self.names = []
        self.seen_ids = {}
        self.matched = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                table = json.load(f)
        except Exception as e:
            print("[can] could not load", self.path, "-", e)
            return
        for m in table.get("messages", []):
            specs = []
            for s in m.get("signals", []):
                if s.get("byte_order") != "little_endian":
                    continue
                specs.append({"name": s["channel"], "start": int(s["start"]),
                              "length": int(s["length"]), "signed": bool(s["signed"]),
                              "scale": float(s["scale"]), "offset": float(s["offset"])})
                self.names.append(s["channel"])
            if specs:
                self.frames[int(m["id"])] = specs
        print(f"[can] loaded {len(self.frames)} CAN messages, "
              f"{len(self.names)} channels from {os.path.basename(self.path)}")

    @staticmethod
    def _bits(data, start, length, signed):
        val = 0
        for i in range(length):
            idx = start + i
            b = idx >> 3
            if b >= len(data):
                break
            val |= ((data[b] >> (idx & 7)) & 1) << i
        if signed and length and (val >> (length - 1)) & 1:
            val -= (1 << length)
        return val

    def decode(self, payload):
        if len(payload) < 4:
            return
        le = int.from_bytes(payload[:4], "little")
        be = int.from_bytes(payload[:4], "big")
        can_id = le if le in self.frames else (be if be in self.frames else le)
        data = payload[4:]
        self.seen_ids[can_id] = self.seen_ids.get(can_id, 0) + 1
        specs = self.frames.get(can_id)
        if not specs:
            return
        self.matched += 1
        out = {}
        for s in specs:
            raw = self._bits(data, s["start"], s["length"], s["signed"])
            out[s["name"]] = round(raw * s["scale"] + s["offset"], 4)
        with LOCK:
            CAN_VALUES.update(out)


CAN = CANDecoder(os.path.join(HERE, "can_signals.json"))


def derive_telemetry(v):
    def g(*names):
        for n in names:
            if n in v:
                return float(v[n])
        return 0.0

    MAP = (v["psrg_w"] if "psrg_w" in v else v.get("psr_w", 0.0)) * 0.001
    ATM = g("pu_w") * 0.001
    boost = (MAP - ATM) * 14.5037738
    if boost < 0:
        boost = 0.0
    TGT = (v["pvds_w"] if "pvds_w" in v else v.get("psrs_w", 0.0)) * 0.001
    tgt = max(0.0, (TGT - ATM) * 14.5037738) if TGT else 0.0
    lams = [v[k] for k in ("lamsoni_w", "lamsoni2_w") if k in v]
    LAM = min(max(lams), 1.99) if lams else 0.0
    hpfs = [v[k] for k in ("prroh_w", "prroh2_w") if k in v]
    HPF = min(hpfs) if hpfs else 0.0
    knk = [v[k] for k in v if k.startswith("wkrv")]
    KNK = (sum(knk) / len(knk)) if knk else 0.0

    tele = {
        "rpm":     g("nmot_w"),
        "boost":   boost,
        "afr":     LAM * 14.7,
        "lambda":  LAM,
        "timing":  g("zwist"),
        "ign":     g("zwist"),
        "iat":     g("tans", "tavdk_w"),
        "egt":     g("tabgm", "tabg", "egt"),
        "coolant": g("tmot"),
        "oilT":    g("toel", "Gbx_tOilConv"),
        "wg":      g("tvldste_w"),
        "ethanol": g("ethanolpercent"),
        "speed":   g("vfzg_w"),
        "gear":    int(g("gangi")),
        "pedal":   g("wped_w"),
        "thr_raw": g("wdkba", "AirSys_rAbsThrPos"),
        "volts":   g("ubsq"),
        "lpf":     g("pbkist_w") / 100.0,
        "hpf":     HPF,
        "knk":     KNK,
        "load":    g("rl_w"),
        "dinj":    g("ti_l"),
        "pinj":    g("te_l"),
        "gLat":    g("Com_aTrnvrs"),
        "gLong":   g("Com_aLng") * 0.101971621,
        "tgt":     tgt,
        "tank":    g("tfst"),
    }
    baro = g("pu_w")
    if baro > 300:
        pa = 145366.45 * (1.0 - (baro / 1013.25) ** 0.190284)
        isa = 15.0 - 0.0019812 * pa
        tele["da"] = pa + 118.8 * (g("tumc") - isa)
    present = set()
    for key, srcs in DERIVED_SOURCES.items():
        if key in DERIVED_REQUIRE_ALL:
            if all(x in v for x in srcs):
                present.add(key)
        elif any(x in v for x in srcs):
            present.add(key)
    return tele, present


VIEW_NEEDS_ATM = {"boost", "tgt", "da"}


def viewer_derived_keys(cols):
    have = set(cols)
    keys = []
    for key, srcs in DERIVED_SOURCES.items():
        if key in have or key not in TELEMETRY:
            continue
        ok = (all(x in have for x in srcs) if key in DERIVED_REQUIRE_ALL
              else any(x in have for x in srcs))
        if ok:
            keys.append(key)
    if "knk" not in have and any(c.startswith("wkrv") for c in cols):
        keys.append("knk")
    if "hpf" not in have and ("prroh_w" in have or "prroh2_w" in have):
        keys.append("hpf")
    src = {x for srcs in DERIVED_SOURCES.values() for x in srcs}
    src |= {c for c in cols if c.startswith("wkrv")}
    src |= {"prroh_w", "prroh2_w", "pu_w", "psr_w", "mapswitch"}
    return keys, src


def viewer_derived_row(keys, raw_idx, vals, last):
    for j, c in raw_idx:
        if vals[j] is not None:
            last[c] = vals[j]
    if "__atm_col__" not in last:
        last["__atm_col__"] = any(c == "pu_w" for _, c in raw_idx)
    tele, present = derive_telemetry(last)
    out = []
    for k in keys:
        if k == "knk":
            kn = [last[c] for c in last if c.startswith("wkrv")]
            out.append(max(kn, key=abs) if kn else None)
        elif k == "hpf":
            b = [last[c] for c in ("prroh_w", "prroh2_w") if c in last]
            out.append(min(b) if b else None)
        elif k in VIEW_NEEDS_ATM and last["__atm_col__"] and "pu_w" not in last:
            out.append(None)
        elif k in present:
            try:
                out.append(round(float(tele.get(k, 0.0)), 4))
            except (TypeError, ValueError):
                out.append(None)
        else:
            out.append(None)
    return out


def gauge_raw_keys():
    g = SETTINGS.get("gauges") or {}
    if not isinstance(g, dict):
        return []
    slots = list(g.get("p1") or []) + list(g.get("p2") or []) + [g.get("big2")]
    return [k[4:] for k in slots if isinstance(k, str) and k.startswith("raw:")]


def gauge_channels():
    with LOCK:
        layout = list(WS.layout or [])
        have = dict(RAW_VALUES)
    out, seen = [], set()
    for it in layout:
        var = it.get("var")
        if not var or var in seen or var not in have:
            continue
        seen.add(var)
        name = it.get("name", "")
        lp = name.rfind("(")
        label = (name[:lp] if lp > 0 else name).strip() or var
        out.append({"key": var, "label": label, "unit": it.get("metric_unit") or "",
                    "value": round(have[var], 3)})
    out.sort(key=lambda c: c["label"].lower())
    return out


def wanted_log_mask():
    v = SETTINGS.get("log_mask", "")
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 32767 else None


class DS1WebSocket(threading.Thread):
    daemon = True
    PORT = 9997

    def __init__(self):
        super().__init__()
        self.running = True
        self.paused = False
        self.streaming = False
        self.sock = None
        self.host = None
        self.layout = []
        self.lastraster = {}
        self.clock = 0.0
        self._ts_prev = None
        self.seen_raster2 = False
        self.last_signal = 0.0
        self.frames = 0
        self.frame_ts = {}
        self.r2_since_row = False
        self.sig_seen = False
        self.start_pending = False
        self.start_deadline = 0.0
        self.mask_check_later = False
        self._tx = threading.Lock()
        self.dir_at = 0.0
        self.lost_at = 0.0
        self.last_rx = 0.0
        self.last_frame = 0.0
        self.ds2_sent_at = 0.0
        self.ds2_tries = 0
        self._last_dtc = 0.0
        self._err_at = 0.0
        self.stats = {"sessions": 0, "drops": [], "ds2_retries": 0,
                      "frame_errors": 0, "connected_at": None}

    def rx_fresh(self, within=5.0):
        return self.sock is not None and time.time() - self.last_rx < within

    def _handshake(self, host):
        s = socket.create_connection((host, self.PORT), timeout=2)
        s.settimeout(4)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\n"
               f"Host: {host}:{self.PORT}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Origin: http://{host}\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            d = s.recv(1024)
            if not d:
                raise IOError("closed during handshake")
            resp += d
        if b" 101 " not in resp.split(b"\r\n", 1)[0] + b" ":
            raise IOError("handshake rejected: " + resp[:48].decode("latin1", "replace"))
        return s, resp.split(b"\r\n\r\n", 1)[1]

    def _send(self, msg, opcode=0x1):
        data = msg.encode("utf-8") if isinstance(msg, str) else msg
        hdr = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        hdr += mask
        frame = bytes(hdr) + bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        with self._tx:
            sock = self.sock
            if sock is None:
                raise IOError("not connected")
            sock.sendall(frame)

    @staticmethod
    def _parse(buf):
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        n = b1 & 0x7F
        i = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack(">H", buf[2:4])[0]; i = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack(">Q", buf[2:10])[0]; i = 10
        mask = b""
        if masked:
            if len(buf) < i + 4:
                return None
            mask = buf[i:i + 4]; i += 4
        if len(buf) < i + n:
            return None
        payload = bytes(buf[i:i + n])
        if masked:
            payload = bytes(b ^ mask[j & 3] for j, b in enumerate(payload))
        return opcode, payload, i + n

    def _load_layout(self, arr):
        labels = {}
        for it in arr:
            name = it.get("name", "")
            lp, rp = name.rfind("("), name.rfind(")")
            it["var"] = name[lp + 1:rp] if (0 <= lp < rp) else name
            it["_val"] = None
            it["_ready"] = False
            it["_res"] = ""
            key, nth = it["var"], 2
            while key in labels:
                key = "%s#%d" % (it["var"], nth)
                nth += 1
            it["_key"] = key
            unit = it.get("metric_unit")
            labels[key] = "%s(%s)" % (name, "-" if unit is None else unit)
        self.layout = arr
        self.seen_raster2 = False
        self.r2_since_row = False
        with LOCK:
            LAYOUT_LABELS.clear()
            LAYOUT_LABELS.update(labels)
        with LOCK:
            STATE["ds1"]["stream_channels"] = len(arr)
        print(f"[ds1-ws] DAQ layout: {len(arr)} channels")

    def _on_text(self, text):
        if not text:
            return
        if text.startswith("*_z"):
            note_ds1_fault("iso", text[3:])
            return
        if text.startswith("*_y"):
            note_ds1_fault("vag", text[3:])
            return
        if text[0] != "[":
            return
        try:
            arr = json.loads(text)
        except Exception:
            return
        if not (isinstance(arr, list) and arr and isinstance(arr[0], dict)):
            return
        first = arr[0]
        if isinstance(first.get("SIG"), dict):
            sig = first["SIG"]
            with LOCK:
                STATE["ds1"]["sig"] = {k: sig.get(k) for k in
                                       ("MASK", "CSV", "FS", "RSSI", "IP",
                                        "SSID", "APP", "AWS")}
            self.sig_seen = True
            if self.mask_check_later and self.streaming:
                self.mask_check_later = False
                want = self._mask_mismatch()
                if want is not None:
                    self._send("mask%d" % want)
                    print("[ds1-ws] log mask -> %d (restarting stream)" % want)
                    self._send("ds2stop")
                    self._send("ds2start")
            return
        if "DIR" in first:
            self.dir_at = time.time()
            return
        if isinstance(first.get("NVS_STA"), dict):
            with LOCK:
                STATE["ds1"]["ds1_serial"] = first["NVS_STA"].get("serial")
            return
        if "raster" in first and "byte_offset" in first:
            self._load_layout(arr)

    def _decode(self, it, payload):
        bo, bs = it["byte_offset"], it["byte_size"]
        if bo + bs > len(payload):
            return
        r = 0
        for k in range(bs):
            r += payload[bo + k] << (k * 8)
        r &= it.get("bit_mask", 0xFFFFFFFF)
        r >>= it.get("bit_offset", 0)
        if it.get("signed"):
            bits = bs * 8
            if r >= (1 << (bits - 1)):
                r -= (1 << bits)
        val = r * it.get("metric_scaler", 1.0) + it.get("metric_offset", 0.0)
        it["_val"] = val
        fmt = it.get("metric_format")
        try:
            it["_res"] = "%.*f" % (int(fmt), val)
        except (TypeError, ValueError):
            it["_res"] = repr(round(val, 4))
        it["_ready"] = True

    def _frame_clock(self, ts_ms):
        t = ts_ms / 1000.0
        if self._ts_prev is not None:
            d = t - self._ts_prev
            if 0.0 <= d <= 5.0:
                self.clock += d
        self._ts_prev = t
        return self.clock

    def _on_binary(self, payload):
        n = len(payload)
        if n > 12:
            self.last_frame = time.time()
        if n > 12 and self.layout:
            raster = payload[0]
            stamp = int.from_bytes(payload[1:5], "little")
            self.frame_ts[raster] = stamp
            self._frame_clock(stamp)
            body = payload[5:]
            self.lastraster[raster] = body
            for it in self.layout:
                if it.get("raster") == raster:
                    self._decode(it, body)
            if raster == 2:
                self.seen_raster2 = True
                self.r2_since_row = True
            elif raster == 1:
                self._emit()
        elif 4 <= n <= 12:
            CAN.decode(payload)

    def _emit(self):
        v, raw = {}, {}
        for it in self.layout:
            val = it.get("_val")
            if val is None:
                continue
            v[it["var"]] = val
            raw[it["var"]] = float(val)

        tele, present = derive_telemetry(v)

        try:
            mps = int(float(v.get("mapswitch", 0)))
        except (TypeError, ValueError):
            mps = 0
        with LOCK:
            DERIVED_PRESENT.clear()
            DERIVED_PRESENT.update(present)
            TELEMETRY.update({k: v2 for k, v2 in tele.items() if k in TELEMETRY})
            RAW_VALUES.clear()
            RAW_VALUES.update(raw)
            RAW_VALUES.update(CAN_VALUES)
            if 0 <= mps < MAP_SLOTS:
                STATE["map"] = mps
        self.frames += 1
        row = self._log_row()
        after_r2 = self.r2_since_row
        self.r2_since_row = False
        LOGGER.on_frame(self.clock, row, after_r2)
        CONTLOG.on_frame(self.clock, row, after_r2)
        now = time.time()
        if now - self._last_dtc < 0.5:
            return
        self._last_dtc = now
        rep = dtc_report()
        if rep["available"]:
            with LOCK:
                TELEMETRY["mil"] = 1.0 if rep["mil_on"] else 0.0
                STATE["faults"] = rep["faults"]

    def _log_row(self):
        row = {"t": "%.3f" % (self.frame_ts.get(1, 0) * 0.001)}
        for it in self.layout:
            if it.get("_ready"):
                row[it["_key"]] = it["_res"]
                it["_ready"] = False
            else:
                row[it["_key"]] = ""
        return row

    def _mask_mismatch(self):
        want = wanted_log_mask()
        if want is None:
            return None
        with LOCK:
            cur = (STATE["ds1"].get("sig") or {}).get("MASK")
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            return None
        return want if cur != want else None

    def _start_stream(self):
        self.start_pending = False
        want = self._mask_mismatch()
        if want is not None:
            self._send("mask%d" % want)
            print("[ds1-ws] log mask -> %d" % want)
            time.sleep(0.3)
        elif wanted_log_mask() is not None and not self.sig_seen:
            self.mask_check_later = True
        self._send("ds2start")
        self.ds2_sent_at = time.time()
        self.streaming = True
        with LOCK:
            STATE["ds1"]["streaming"] = True

    def set_map(self, i):
        if self.streaming and self.sock:
            try:
                self._send("mapsw" + str(int(i)))
                return True
            except Exception:
                return False
        return False

    INIT_DELAY = 1.0
    AFTER_DIR = 0.3
    START_DELAY = 2.5
    SIGNAL_EVERY = 3.0
    SILENT_LIMIT = 10.0
    NO_LAYOUT_RETRY = 5.0
    DS2_TRIES = 3
    STALL_RETRY = 6.0
    SAME_ADDR_SEC = 60.0

    def _session(self, label, ip):
        self.sock, leftover = self._handshake(ip)
        self.sock.settimeout(0.25)
        self.host = label
        ds1_found(label, ip, DS1_ADDR.get("port", 80) if DS1_ADDR["ip"] == ip else 80)
        buf = bytearray(leftover)
        opened = time.time()
        self.last_rx = opened
        self.last_frame = 0.0
        self.layout = []
        self.sig_seen = False
        self.mask_check_later = False
        self.start_pending = False
        self.ds2_sent_at = 0.0
        self.ds2_tries = 0
        sent_init = False
        start_at = None
        self.last_signal = 0.0
        self.stats["sessions"] += 1
        self.stats["connected_at"] = opened
        with LOCK:
            STATE["ds1"]["ws_host"] = label
        print(f"[ds1-ws] connected to ws://{ip}:{self.PORT} ({label})")
        while self.running and not self.paused:
            now = time.time()
            if not sent_init and now - opened >= self.INIT_DELAY:
                self._send("init")
                sent_init = True
                self.dir_at = 0.0
                start_at = now + self.START_DELAY
                self.last_signal = now
            if start_at and self.dir_at:
                start_at = min(start_at, self.dir_at + self.AFTER_DIR)
            if start_at and now >= start_at and not self.streaming and not self.start_pending:
                start_at = None
                if wanted_log_mask() is not None:
                    self._send("signal")
                    self.last_signal = now
                    self.start_pending = True
                    self.start_deadline = now + 2.5
                else:
                    self._start_stream()
                    self.ds2_tries = 1
            if self.start_pending and (self.sig_seen or now >= self.start_deadline):
                self._start_stream()
                self.ds2_tries = 1
            if sent_init and now - self.last_signal >= self.SIGNAL_EVERY:
                self._send("signal")
                self.last_signal = now
            if now - self.last_rx > self.SILENT_LIMIT:
                raise IOError("DS1 silent for %ds" % self.SILENT_LIMIT)
            if self.streaming and not self.layout and self.ds2_sent_at \
                    and now - self.ds2_sent_at > self.NO_LAYOUT_RETRY:
                if self.ds2_tries >= self.DS2_TRIES:
                    raise IOError("no data after %d ds2start" % self.ds2_tries)
                self.ds2_tries += 1
                self.stats["ds2_retries"] += 1
                print("[ds1-ws] no layout yet - ds2start again (%d)" % self.ds2_tries)
                self._send("ds2start")
                self.ds2_sent_at = now
            elif self.layout and self.last_frame and now - self.last_frame > self.STALL_RETRY \
                    and now - self.ds2_sent_at > self.STALL_RETRY:
                self.stats["ds2_retries"] += 1
                self._send("ds2start")
                self.ds2_sent_at = now
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                raise IOError("socket closed")
            self.last_rx = time.time()
            buf += data
            while True:
                res = self._parse(buf)
                if res is None:
                    break
                opcode, payload, consumed = res
                del buf[:consumed]
                try:
                    if opcode == 0x1:
                        self._on_text(payload.decode("utf-8", "replace"))
                    elif opcode == 0x2:
                        self._on_binary(payload)
                    elif opcode == 0x9:
                        self._send(payload, opcode=0xA)
                    elif opcode == 0x8:
                        raise IOError("DS1 closed the connection")
                except IOError:
                    raise
                except Exception as e:
                    self.stats["frame_errors"] += 1
                    if time.time() - self._err_at > 30:
                        self._err_at = time.time()
                        import traceback
                        print("[ds1-ws] frame skipped: %s: %s\n%s" % (
                            e.__class__.__name__, e,
                            "".join(traceback.format_exc().splitlines(True)[-4:])))

    def pause(self):
        self.paused = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        with LOCK:
            STATE["ds1"]["paused"] = True

    def resume(self):
        self.paused = False
        with LOCK:
            STATE["ds1"]["paused"] = False

    def _note_drop(self, label, why):
        up = time.time() - (self.stats["connected_at"] or time.time())
        self.stats["drops"] = (self.stats["drops"] + [{
            "at": datetime.now().strftime("%H:%M:%S"), "host": label,
            "why": why, "after_s": round(up, 1), "frames": self.frames}])[-10:]
        print("[ds1-ws] dropped (%s) after %.0fs: %s" % (label, up, why))

    def run(self):
        t0 = time.time()
        while not DS1.first_try_done and time.time() - t0 < 15:
            time.sleep(0.2)
        while self.running:
            if self.paused:
                time.sleep(0.3)
                continue
            if _DTC_IMPORT["running"]:
                time.sleep(0.5)
                continue
            had_session = False
            recent = (DS1_ADDR["ip"] and self.lost_at
                      and time.time() - self.lost_at < self.SAME_ADDR_SEC)
            cands = ([(DS1_ADDR["name"] or DS1_ADDR["ip"], DS1_ADDR["ip"],
                       DS1_ADDR.get("port", 80))] if recent else ds1_candidates())
            for label, ip, port in cands:
                if self.paused:
                    break
                try:
                    self._session(label, ip)
                except Exception as e:
                    if self.sock is not None and self.stats["connected_at"]:
                        had_session = True
                        self.lost_at = time.time()
                        why = str(e) if isinstance(e, IOError) and str(e) else e.__class__.__name__
                        if not self.paused:
                            self._note_drop(label, why)
                    with LOCK:
                        STATE["ds1"]["last_error"] = f"ws {label}: {e.__class__.__name__}"
                finally:
                    self.streaming = False
                    self.seen_raster2 = False
                    try:
                        if self.sock:
                            self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    self.stats["connected_at"] = None
                    with LOCK:
                        STATE["ds1"]["streaming"] = False
                if had_session or not self.running:
                    break
            time.sleep(1.0 if had_session else 2.0)


WS = DS1WebSocket()


class InternetMonitor(threading.Thread):
    daemon = True

    def run(self):
        while True:
            ok = False
            for host in (("1.1.1.1", 53), ("8.8.8.8", 53)):
                try:
                    s = socket.create_connection(host, timeout=2)
                    s.close(); ok = True; break
                except OSError:
                    continue
            STATE["internet"] = ok
            time.sleep(8)


NET_MON = InternetMonitor()


RAW_VALUES = {}
FALLBACK_FIELDS = ["rpm", "boost", "afr", "timing", "iat", "egt", "coolant",
                   "oilT", "wg", "ethanol", "speed", "gear", "pedal", "volts"]

LOG_LABELS = {
    "t": ("Time", "s"),              "rpm": ("Engine speed", "rpm"),
    "boost": ("Boost", "psi"),       "afr": ("AFR", "afr"),
    "lambda": ("Lambda", "lambda"),  "timing": ("Ignition timing", "deg"),
    "ign": ("Ignition advance", "deg"), "iat": ("Charge air temp", "C"),
    "egt": ("Exhaust gas temp", "C"), "coolant": ("Coolant temp", "C"),
    "oilT": ("Oil temp", "C"),       "wg": ("Wastegate duty", "%"),
    "ethanol": ("Ethanol", "%"),     "speed": ("Vehicle speed", "km/h"),
    "gear": ("Gear", "-"),           "pedal": ("Pedal", "%"),
    "thr_raw": ("Throttle plate", "%"), "volts": ("Battery", "V"),
    "lpf": ("LPF actual", "bar"),    "load": ("Load", "%"),
    "dinj": ("Inj pulse all", "ms"), "pinj": ("Port inj pulse", "ms"),
    "gLat": ("Lateral accel", "g"),  "gLong": ("Longitudinal accel", "g"),
    "mil": ("MIL on", "-"),          "da": ("Density altitude", "ft"),
    "tgt": ("Boost target", "psi"),
    "tank": ("Tank level", "l"),
}

DERIVED_SOURCES = {
    "rpm": ("nmot_w",),          "boost": ("psrg_w", "psr_w"),
    "afr": ("lamsoni_w", "lamsoni2_w"), "lambda": ("lamsoni_w", "lamsoni2_w"),
    "timing": ("zwist",),        "ign": ("zwist",),
    "iat": ("tans", "tavdk_w"),  "egt": ("tabgm", "tabg", "egt"),
    "coolant": ("tmot",),        "oilT": ("toel", "Gbx_tOilConv"),
    "wg": ("tvldste_w",),        "ethanol": ("ethanolpercent",),
    "speed": ("vfzg_w",),        "gear": ("gangi",),
    "pedal": ("wped_w",),        "volts": ("ubsq",),
    "thr_raw": ("wdkba", "AirSys_rAbsThrPos"),
    "lpf": ("pbkist_w",),        "load": ("rl_w",),
    "dinj": ("ti_l",),           "pinj": ("te_l",),
    "gLat": ("Com_aTrnvrs",),    "gLong": ("Com_aLng",),
    "mil": ("DFES_ctMIL",),      "da": ("pu_w", "tumc"),
    "tgt": ("pvds_w", "psrs_w"),
    "tank": ("tfst",),
}
DERIVED_REQUIRE_ALL = {"da"}

LOG_COLLAPSED = {"knk", "hpf"}

DERIVED_PRESENT = set()


CLOCK = {"synced_by": "", "at": 0.0}


def has_rtc():
    return os.path.exists("/dev/rtc0") or os.path.exists("/dev/rtc")


def timezone_list():
    try:
        return sorted(zoneinfo.available_timezones())
    except Exception:
        return []


def current_timezone():
    ok, out = _run(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=3)
    return out.strip() if ok else ""


def clock_status():
    src, ntp = "", False
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized",
                              "--value"], capture_output=True, timeout=2)
        ntp = out.stdout.decode().strip() == "yes"
    except Exception:
        pass
    if has_rtc():
        src = "rtc"
    elif ntp:
        src = "ntp"
    elif CLOCK["synced_by"]:
        src = CLOCK["synced_by"]
    elif DEMO:
        src = "demo"
    now = datetime.now()
    return {"ok": bool(src), "source": src, "rtc": has_rtc(), "ntp": ntp,
            "iso": now.isoformat(timespec="seconds"),
            "epoch": time.time(),
            "date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p").lstrip("0")}


def set_clock(epoch_ms, source="phone"):
    if has_rtc():
        return False, "rtc fitted"
    try:
        when = float(epoch_ms) / 1000.0
    except (TypeError, ValueError):
        return False, "bad timestamp"
    if abs(when - time.time()) < 90:
        CLOCK.update(synced_by=source, at=time.time())
        return True, "already close"
    stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S")
    ok, out = _run(["sudo", "-n", "date", "-s", stamp], timeout=5)
    if ok:
        CLOCK.update(synced_by=source, at=time.time())
        _run(["sudo", "-n", "fake-hwclock", "save"], timeout=5)
        print(f"[clock] set from {source} -> {stamp}")
    return ok, out


LOG_TYPES = {"pull": "PULL", "manual": "MANUAL", "general": "AUTO",
             "auto": "AUTO", "power": "POWER"}


LAYOUT_LABELS = {}


class Ds1RowWriter:

    def __init__(self, fh):
        self.fh = fh

    def writerow(self, cells):
        self.fh.write("\r\n" + ",".join("" if c is None else str(c) for c in cells))


def col_label(key):
    if key == "t":
        return "Time(s)"
    if key in LAYOUT_LABELS:
        return LAYOUT_LABELS[key]
    name, unit = LOG_LABELS.get(key, (None, None))
    if name is None:
        m = MONITORS.get(key) or {}
        name = m.get("readable") or key
        unit = m.get("units") or ""
    return "%s(%s)(%s)" % (name, key, unit or "-")


def _peel_group(s):
    if not s.endswith(")"):
        return None, s
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        if s[i] == ")":
            depth += 1
        elif s[i] == "(":
            depth -= 1
            if depth == 0:
                return s[i + 1:-1], s[:i]
    return None, s


def col_key(label):
    s = (label or "").strip()
    if s in ("Time(s)", "Time"):
        return "t"
    unit, rest = _peel_group(s)
    if unit is None:
        return s
    var, _ = _peel_group(rest)
    return var if var else s


def is_data_row(row):
    for x in row:
        if x == "":
            continue
        try:
            float(x)
        except ValueError:
            return False
    return True


def read_header(rdr):
    header = []
    for row in rdr:
        if not row:
            continue
        if is_data_row(row):
            return header, row
        header = row
    return header, None


def log_name(stamp, ltype):
    kind = LOG_TYPES.get(str(ltype).lower(), str(ltype).upper())
    base = f"{stamp:%m%d%Y_%H%M}_{kind}"
    name, n = base + ".csv", 1
    while os.path.exists(os.path.join(LOG_DIR, name)):
        n += 1
        name = f"{base}-{n}.csv"
    return name


def being_written(name):
    return any(r.active and r.fname == name for r in (LOGGER, CONTLOG))


PRUNE_TARGET_BYTES = 1200 * 1024 * 1024
_last_prune = [0.0]


def prune_logs():
    import shutil
    try:
        names = [n for n in os.listdir(LOG_DIR) if n.endswith(".csv")]
    except OSError:
        return 0

    def age(n):
        try:
            return os.path.getmtime(os.path.join(LOG_DIR, n))
        except OSError:
            return 0.0

    order = sorted(names, key=age)

    removed = 0
    for name in order:
        try:
            if shutil.disk_usage(LOG_DIR).free >= PRUNE_TARGET_BYTES:
                break
        except Exception:
            break
        if being_written(name):
            continue
        try:
            os.remove(os.path.join(LOG_DIR, name))
            removed += 1
        except OSError:
            continue
        with LOCK:
            LOG_META[:] = [m for m in LOG_META if m.get("file") != name]
    if removed:
        print("[log] pruned %d old log(s) to free space" % removed)
    return removed


def prune_if_needed():
    now = time.monotonic()
    if now - _last_prune[0] < 30.0:
        return
    _last_prune[0] = now
    try:
        prune_logs()
    except Exception as e:
        print("[log] prune error:", e)


def stamp_from_name(fname, path):
    base = os.path.splitext(fname)[0].split("-")[0]
    parts = base.split("_")
    for fmt, n in (("%m%d%Y%H%M", 2), ("%Y%m%d%H%M%S", 2)):
        if len(parts) >= n:
            try:
                return datetime.strptime("".join(parts[:n]), fmt)
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return datetime.now()


DTC_LETTERS = "PCBU"

DTC_TABLE_PATH = os.environ.get("SPOOLED_DTC_TABLE") or os.path.join(HERE, "dtc_table.json")
_DTC = {"iso": {}, "vag": {}, "mtime": -1.0, "source": ""}
DTC_PENDING = "Description loads once your DS1 is connected"


def _load_dtc_table():
    try:
        mtime = os.path.getmtime(DTC_TABLE_PATH)
    except OSError:
        if _DTC["mtime"] != -1.0:
            _DTC.update(iso={}, vag={}, mtime=-1.0, source="")
        return
    if mtime == _DTC["mtime"]:
        return
    try:
        with open(DTC_TABLE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        iso = data.get("iso") or {}
        vag = data.get("vag") or {}
        if not isinstance(iso, dict):
            raise ValueError("'iso' is not an object")
        _DTC.update(iso=iso, vag=vag, mtime=mtime,
                    source=str(data.get("source", "")))
        print(f"[dtc] loaded {len(iso)} ISO + {len(vag)} VAG descriptions "
              f"from {DTC_TABLE_PATH}")
    except Exception as e:
        print(f"[dtc] {DTC_TABLE_PATH} unusable ({e}); codes show without descriptions")
        _DTC.update(iso={}, vag={}, mtime=mtime, source="")


def dtc_desc(code):
    if not code:
        return ""
    key = str(code).strip().upper()
    return _DTC["iso"].get(key) or ""


def vag_desc(num):
    if num is None:
        return ""
    return _DTC["vag"].get(str(num).strip()) or ""


DS1_FAULT_MSGS = []
DS1_FAULT_KEEP = 20


def note_ds1_fault(kind, text):
    with LOCK:
        DS1_FAULT_MSGS.append({"kind": kind, "text": text, "at": time.time()})
        del DS1_FAULT_MSGS[:-DS1_FAULT_KEEP]


_DTC_IMPORT = {"running": False, "tried": set(), "error": ""}


def _dtc_table_firmware():
    try:
        with open(DTC_TABLE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("firmware")
    except Exception:
        return None


def run_dtc_import_if_needed(base, firmware=""):
    _load_dtc_table()
    have = bool(_DTC["iso"])
    stored = _dtc_table_firmware() if have else None
    stale = have and stored is not None and bool(firmware) and stored != firmware
    if have and not stale:
        return
    key = firmware or "?"
    if _DTC_IMPORT["running"] or key in _DTC_IMPORT["tried"]:
        return
    _DTC_IMPORT["tried"].add(key)
    _DTC_IMPORT["running"] = True
    try:
        import_dtc_from_ds1(base, firmware)
    finally:
        _DTC_IMPORT["running"] = False


def fetch_page(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": "Spooled",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")


def import_dtc_from_ds1(base, firmware=""):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "spooled_import_dtc", os.path.join(HERE, "tools", "import_dtc.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        url = base.rstrip("/") + "/"
        page = fetch_page(url)
        iso = mod.extract_object(page, "dtc_lookup")
        try:
            vag = mod.extract_object(page, "vag_lookup")
        except Exception:
            vag = {}
        if len(iso) < 1000 or "P0299" not in iso:
            raise ValueError("only %d ISO codes - that page is not the DS1 app" % len(iso))
        payload = {"source": url, "firmware": firmware, "iso": iso, "vag": vag}
        tmp = DTC_TABLE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, DTC_TABLE_PATH)
        _DTC_IMPORT["error"] = ""
        print("[dtc] imported %d ISO + %d VAG descriptions from %s"
              % (len(iso), len(vag), url))
        _load_dtc_table()
    except Exception as e:
        _DTC_IMPORT["error"] = "%s: %s" % (e.__class__.__name__, e)
        print("[dtc] import from DS1 failed: %s" % _DTC_IMPORT["error"])


def dtc_table_status():
    _load_dtc_table()
    return {"imported": len(_DTC["iso"]),
            "vag": len(_DTC["vag"]),
            "import_running": _DTC_IMPORT["running"],
            "import_error": _DTC_IMPORT["error"],
            "source": _DTC["source"],
            "total": len(_DTC["iso"])}


def iso_to_code(raw):
    try:
        r = int(round(float(raw))) & 0xFFFF
    except (TypeError, ValueError):
        return None
    if r == 0:
        return None
    return "%s%X%X%X%X" % (DTC_LETTERS[(r >> 14) & 0x3], (r >> 12) & 0x3,
                           (r >> 8) & 0xF, (r >> 4) & 0xF, r & 0xF)


def dtc_report():
    _load_dtc_table()
    with LOCK:
        rv = dict(RAW_VALUES)

    demo = False
    if DEMO and not any(k in rv for k in ("DFES_ctMIL", "DFES_ctSrv",
                                          "DFES_ISOFrstCnfDFC")):
        demo = True
        rv.update({
            "DFES_ctMIL": 2, "DFES_ctOBD": 2,
            "DFES_ctOBDPend": 1, "DFES_ctSrv": 4,
            "DFES_ISOFrstCnfDFC":  0x0299,
            "DFES_ISORcntCnfDFC":  0x0234,
            "DFES_ISOFrstFailDFC": 0x0299,
            "DFES_ISORcntFailDFC": 0x0303,
        })

    def n(k):
        try:
            return int(round(float(rv.get(k, 0))))
        except (TypeError, ValueError):
            return 0

    counts = {"mil": n("DFES_ctMIL"), "confirmed": n("DFES_ctOBD"),
              "pending": n("DFES_ctOBDPend"), "service": n("DFES_ctSrv")}
    slots = [("DFES_ISOFrstCnfDFC", "first confirmed"),
             ("DFES_ISORcntCnfDFC", "most recent confirmed"),
             ("DFES_ISOFrstFailDFC", "first failed"),
             ("DFES_ISORcntFailDFC", "most recent failed")]
    no_iso, no_vag = not _DTC["iso"], not _DTC["vag"]

    def described(desc, waiting):
        return {"desc": desc or (DTC_PENDING if waiting else "unknown code"),
                "known": bool(desc), "pending": not desc and waiting}

    seen = {}
    for key, label in slots:
        code = iso_to_code(rv.get(key, 0))
        if not code:
            continue
        if code not in seen:
            seen[code] = dict(described(dtc_desc(code), no_iso), code=code,
                              raw="0x%04X" % (int(rv.get(key, 0)) & 0xFFFF),
                              slots=[])
        seen[code]["slots"].append(label)
    with LOCK:
        msgs = list(DS1_FAULT_MSGS)
    for m in msgs:
        text = m["text"].strip()
        if m["kind"] == "iso":
            code = text.split()[0].upper() if text else ""
            if not code:
                continue
            desc, waiting = dtc_desc(code), no_iso
        else:
            parts = text.split(None, 1)
            if not parts:
                continue
            code = "VAG " + parts[0]
            desc = vag_desc(parts[0]) or (parts[1] if len(parts) > 1 else "")
            waiting = no_vag
        entry = seen.setdefault(code, dict(described(desc, waiting), code=code,
                                           raw=text, slots=[]))
        if "reported by DS1" not in entry["slots"]:
            entry["slots"].append("reported by DS1")
    available = bool(msgs) or any(k in rv for k in ("DFES_ctMIL", "DFES_ctSrv",
                                                    "DFES_ISOFrstCnfDFC"))
    return {"mil_on": counts["mil"] > 0, "counts": counts,
            "faults": list(seen.values()), "available": available,
            "demo": demo}


LOG_META = []


class AutoLogger:
    THROTTLE_ON = 50.0
    THROTTLE_OFF = 0.5

    SEGMENT_SEC = 180.0
    IDLE_RPM = 300.0
    STOP_AFTER = 5.0

    def __init__(self, mode="pull"):
        self.mode = mode
        self.idle_since = None
        self.await_r2 = False
        self.active = False
        self.trigger = None
        self.manual_request = False
        self.force_stop = False
        self.buffer = []
        self.warmup = []
        self.cols = None
        self.rows = 0
        self.dur = 0.0
        self.fh = None
        self.writer = None
        self.fname = ""
        self.stamp = datetime.now()
        self.start_t = 0.0
        self.peak = {}
        self.curve = []
        self.logs = LOG_META
        self.last_file = None
        self.last_error = ""
        self.stop_reason = ""
        self.write_errors = 0
        self.wlock = threading.Lock()
        self.last_frame_at = 0.0
        self.held_at = None
        self.last_t = None

    @staticmethod
    def est_power(T):
        load = max(0.0, min(1.0, T.get("pedal", 0) / 100.0))
        boost_factor = 1 + max(0.0, T.get("boost", 0)) / 14.7
        hp = load * boost_factor * (T.get("rpm", 0) / 7000.0) * 620.0
        tq = hp * 5252.0 / max(1500.0, T.get("rpm", 1))
        return max(0.0, hp), max(0.0, tq)

    def _track(self, T):
        p = self.peak
        p["boost"] = max(p.get("boost", -99), T.get("boost", 0))
        p["egt"] = max(p.get("egt", 0), T.get("egt", 0))
        p["rpm"] = max(p.get("rpm", 0), T.get("rpm", 0))

    def _snapshot(self, now):
        with LOCK:
            row = {"t": round(now - self.start_t, 3)}
            skip = DERIVED_PRESENT and True
            for k in FALLBACK_FIELDS:
                if skip and k not in DERIVED_PRESENT:
                    continue
                row[k] = round(float(TELEMETRY.get(k, 0)), 4)
            for k, v in TELEMETRY.items():
                if k in row or k in LOG_COLLAPSED:
                    continue
                if skip and k not in DERIVED_PRESENT:
                    continue
                try:
                    row[k] = round(float(v), 4)
                except (TypeError, ValueError):
                    pass
            for k, v in RAW_VALUES.items():
                if k not in row:
                    try:
                        row[k] = round(float(v), 4)
                    except (TypeError, ValueError):
                        pass
        return row

    def on_frame(self, now, row=None, after_r2=False):
        self.last_frame_at = time.monotonic()
        with self.wlock:
            if self.held_at is not None:
                self._resume_or_split(row)
            self.sample(0.0, now, row, after_r2)

    def _resume_or_split(self, row):
        self.held_at = None
        if not self.active or not row:
            return
        ref = self.cols if self.cols is not None else (self.warmup[0] if self.warmup else None)
        same_cols = ref is None or set(row) == set(ref)
        try:
            t = float(row.get("t"))
        except (TypeError, ValueError):
            t = None
        forward = t is None or self.last_t is None or t > self.last_t
        if same_cols and forward:
            print("[log] stream back - continuing %s" % self.fname)
            return
        print("[log] stream back but %s - new file" %
              ("the DS1 restarted" if same_cols else "the channels changed"))
        self.stop_reason = "stream"
        self._end(self.trigger or "manual")

    def tick(self, dt, now):
        with self.wlock:
            self.sample(dt, now)

    def watchdog(self):
        if not self.active or not self.last_frame_at:
            return
        gap = time.monotonic() - self.last_frame_at
        if gap <= self.STREAM_GAP_SEC:
            return
        with self.wlock:
            if not self.active:
                return
            if self.held_at is None:
                self.held_at = time.monotonic()
                try:
                    if self.fh:
                        self.fh.flush()
                        os.fsync(self.fh.fileno())
                except Exception:
                    pass
                print("[log] stream stopped - holding %s" % (self.fname or "log"))
            if gap > self.RESUME_WINDOW_SEC:
                self.stop_reason = "stream"
                self.manual_request = False
                self._end(self.trigger or "manual")

    def start_manual(self):
        self.manual_request = True

    def stop_manual(self):
        self.manual_request = False
        self.force_stop = True

    WARMUP_SAMPLES = 100
    MAX_LOG_SEC = 1800
    MIN_FREE_BYTES = 500 * 1024 * 1024
    MAX_WRITE_ERRORS = 5
    STREAM_GAP_SEC = 2.0
    RESUME_WINDOW_SEC = 30.0

    def _begin(self, trigger, now, fresh=True):
        self.await_r2 = fresh
        self.active = True
        self.trigger = trigger
        self.start_t = now
        self.peak = {}
        self.curve = []
        self.warmup = []
        self.cols = None
        self.rows = 0
        self.dur = 0.0
        self.fh = None
        self.writer = None
        self.stamp = datetime.now()
        self.stop_reason = ""
        self.write_errors = 0
        self.held_at = None
        self.last_t = None
        ltype = ("general" if trigger == "general" else
                 "manual" if trigger == "manual" else "pull")
        self.fname = log_name(self.stamp, ltype)
        print(f"[log] start ({trigger}) -> {self.fname}")

    def _preamble(self):
        with LOCK:
            text = STATE["ds1"].get("status_raw") or ""
            d = dict(STATE["ds1"].get("device") or {})
        if not text:
            parts = []
            if d.get("firmware"):
                parts.append("DS1 firmware:" + d["firmware"])
            for k in ("vin", "ecu", "flash_status"):
                if d.get(k):
                    parts.append(d[k])
            if d.get("last_flash"):
                parts.append("Last flash:" + d["last_flash"])
            text = ",".join(parts) or ("SpooledOS Community %s" % app_version())
        return text if text.endswith("\n") else text + "\n"

    def _open_and_flush(self):
        cols, seen = [], set()
        for s in self.warmup:
            for k in s:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        self.cols = cols or ["t"]
        try:
            self.fh = open(os.path.join(LOG_DIR, self.fname), "w", newline="")
            self.writer = Ds1RowWriter(self.fh)
            header = [col_label(c) for c in self.cols]
            self.fh.write(self._preamble())
            self.fh.write(",".join(header))
            for s in self.warmup:
                self.writer.writerow([s.get(k, "") for k in self.cols])
                self.rows += 1
            self.fh.flush()
        except Exception as e:
            self.last_error = str(e)
            print("log open error:", e)
            self.fh = None
        self.warmup = []

    def _low_disk(self):
        try:
            import shutil
            if shutil.disk_usage(LOG_DIR).free >= self.MIN_FREE_BYTES:
                return False
            prune_if_needed()
            return shutil.disk_usage(LOG_DIR).free < self.MIN_FREE_BYTES
        except Exception:
            return False

    def _write_row(self, row):
        if self.writer is None:
            return
        try:
            self.writer.writerow([row.get(k, "") for k in self.cols])
            self.rows += 1
            self.write_errors = 0
            if self.rows % 250 == 0:
                self.fh.flush()
                os.fsync(self.fh.fileno())
        except Exception as e:
            self.last_error = str(e)
            self.write_errors += 1

    def _end(self, trigger):
        self.active = False
        self.trigger = None
        if self.cols is None:
            if len(self.warmup) < 3:
                self.warmup = []
                return
            self._open_and_flush()
        if self.fh:
            try:
                self.fh.flush()
                self.fh.close()
            except Exception:
                pass
        self.fh = None
        self.writer = None
        if not self.rows:
            return
        self.last_file = self.fname
        self._finish_meta(trigger)

    def _take_row(self, row, after_r2):
        if self.await_r2:
            if not after_r2:
                return
            self.await_r2 = False
        try:
            self.last_t = float(row.get("t"))
        except (TypeError, ValueError, AttributeError):
            pass
        if self.cols is None:
            self.warmup.append(row)
            if len(self.warmup) >= self.WARMUP_SAMPLES:
                self._open_and_flush()
        else:
            self._write_row(row)

    def sample(self, dt, now, row=None, after_r2=True):
        with LOCK:
            thr = float(TELEMETRY.get("pedal", 0))
            T = dict(TELEMETRY)
        auto_on = bool(SETTINGS.get("autolog", True))

        if self.mode == "general":
            return self._sample_general(now, T, row, after_r2)

        if not self.active:
            if (self.manual_request or thr > self.THROTTLE_ON) and self._low_disk():
                self.manual_request = False
                self.stop_reason = "disk"
                return
            if self.manual_request:
                self._begin("manual", now)
            elif auto_on and thr > self.THROTTLE_ON:
                self._begin("auto", now)

        if self.active:
            if row is None:
                row = self._snapshot(now)
            self.dur = now - self.start_t
            self._take_row(row, after_r2)
            self._track(T)
            if self.dur >= self.MAX_LOG_SEC:
                self.stop_reason = "length"
                self.manual_request = False
                return self._end(self.trigger or "manual")
            if self.write_errors >= self.MAX_WRITE_ERRORS:
                self.stop_reason = "disk" if self._low_disk() else "write"
                self.manual_request = False
                return self._end(self.trigger or "manual")
            if self.rows and self.rows % 1000 == 0 and self._low_disk():
                self.stop_reason = "disk"
                self.manual_request = False
                return self._end(self.trigger or "manual")
            if self.force_stop:
                self.force_stop = False
                self._end(self.trigger or "manual")
            elif self.trigger == "manual":
                if not self.manual_request:
                    self._end("manual")
            else:
                if thr <= self.THROTTLE_OFF:
                    self._end("auto")

    def _sample_general(self, now, T, row=None, after_r2=True):
        if not SETTINGS.get("contlog", True):
            if self.active:
                self._end("general")
            return
        running = (float(T.get("rpm", 0)) > self.IDLE_RPM
                   or float(T.get("speed", 0)) > 1.0)
        if running:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now

        if not self.active:
            if not running or self._low_disk():
                return
            self._begin("general", now)

        if not self.active:
            return

        if row is None:
            row = self._snapshot(now)
        self.dur = now - self.start_t
        self._take_row(row, after_r2)
        self._track(T)

        if self.write_errors >= self.MAX_WRITE_ERRORS:
            self.stop_reason = "disk" if self._low_disk() else "write"
            return self._end("general")
        if (not running and self.idle_since is not None
                and now - self.idle_since >= self.STOP_AFTER):
            return self._end("general")
        if self.dur >= self.SEGMENT_SEC:
            self._end("general")
            if not self._low_disk():
                self._begin("general", now, fresh=False)

    def _finish_meta(self, trigger):
        ltype = ("general" if trigger == "general" else
                 "manual" if trigger == "manual" else "pull")
        boost = self.peak.get("boost", 0) or 0
        dur = round(self.dur, 1)
        meta = {
            "type": ltype,
            "label": ("Auto" if trigger == "general" else
                      "Manual" if trigger == "manual" else "Pull"),
            "sub": f"{boost:.1f} psi · {dur}s",
            "file": self.fname,
            "time": self.stamp.strftime("%H:%M:%S"),
            "date": self.stamp.strftime("%Y-%m-%d"),
            "samples": self.rows,
            "channels": max(0, len(self.cols or []) - 1),
            "synced": False,
            "kpi": {k: round(v, 1) for k, v in self.peak.items()},
        }
        with LOCK:
            self.logs.insert(0, meta)
            del self.logs[60:]
        self.last_error = ""
        print(f"[log] saved {self.fname}: {self.rows} samples, "
              f"{len(self.cols or [])} columns")


LOGGER = AutoLogger("pull")
CONTLOG = AutoLogger("general")


INDEX_FILE = os.path.join(LOG_DIR, ".index.json")


def _scan_one(path, fname):
    dur = 0.0
    first_t = None
    peak_boost = None
    rows = 0
    cols = []
    try:
        with open(path, newline="") as f:
            rdr = csv.reader(f)
            header, first = read_header(rdr)
            cols = [col_key(c) for c in header]
            ti = cols.index("t") if "t" in cols else -1
            bi = cols.index("boost") if "boost" in cols else -1
            mi = next((cols.index(k) for k in ("psrg_w", "psr_w") if k in cols), -1)
            atm_i = cols.index("pu_w") if "pu_w" in cols else -1
            atm = None
            body = itertools.chain([first], rdr) if first is not None else rdr
            for r in body:
                if not r:
                    continue
                rows += 1
                if 0 <= ti < len(r) and r[ti]:
                    try:
                        tv = float(r[ti])
                        if first_t is None:
                            first_t = tv
                        dur = tv - first_t
                    except ValueError:
                        pass
                if 0 <= bi < len(r) and r[bi]:
                    try:
                        v = float(r[bi])
                        if peak_boost is None or v > peak_boost:
                            peak_boost = v
                    except ValueError:
                        pass
                elif bi < 0 and mi >= 0 and atm_i >= 0:
                    try:
                        if atm_i < len(r) and r[atm_i]:
                            atm = float(r[atm_i])
                        if atm is not None and mi < len(r) and r[mi]:
                            v = max(0.0, (float(r[mi]) - atm) * 0.0145037738)
                            if peak_boost is None or v > peak_boost:
                                peak_boost = v
                    except ValueError:
                        pass
    except Exception:
        return None

    base = os.path.splitext(fname)[0]
    parts = base.split("_")
    ltype = (parts[2].split("-")[0].lower() if len(parts) > 2 else "manual")
    ltype = {"auto": "general"}.get(ltype, ltype)
    label = {"pull": "Pull", "power": "Power Curve", "general": "Auto"}.get(ltype, "Manual")
    stamp = stamp_from_name(fname, path)

    sub = (f"{peak_boost:.1f} psi · {round(dur,1)}s" if peak_boost is not None
           else f"{round(dur,1)}s")
    return {
        "type": ltype, "label": label, "sub": sub, "file": fname,
        "time": stamp.strftime("%H:%M:%S"), "date": stamp.strftime("%Y-%m-%d"),
        "samples": rows, "channels": max(0, len(cols) - 1), "synced": False,
        "kpi": {"boost": round(peak_boost, 1) if peak_boost is not None else 0},
    }


def load_log_index(limit=60):
    cache = {}
    try:
        with open(INDEX_FILE) as f:
            cache = json.load(f)
    except Exception:
        pass

    try:
        names = [n for n in os.listdir(LOG_DIR) if n.endswith(".csv")]
    except Exception:
        return []
    def _mtime(n):
        try:
            return os.path.getmtime(os.path.join(LOG_DIR, n))
        except OSError:
            return 0.0
    names.sort(key=_mtime, reverse=True)

    out, fresh, changed = [], {}, False
    for name in names[:limit]:
        path = os.path.join(LOG_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = f"{name}:{st.st_size}:{int(st.st_mtime)}"
        meta = cache.get(key)
        if meta is None:
            meta = _scan_one(path, name)
            changed = True
            if meta is None:
                continue
        fresh[key] = meta
        out.append(meta)

    if changed or len(fresh) != len(cache):
        try:
            with open(INDEX_FILE, "w") as f:
                json.dump(fresh, f)
        except Exception:
            pass
    return out


def init_log_index():
    logs = load_log_index()
    with LOCK:
        LOGGER.logs[:] = logs
    print(f"[log] indexed {len(logs)} saved log(s) from {LOG_DIR}")


DEMO = False


class Engine(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__()
        self.running = True
        self.hz = 50.0
        self.clock = 0.0
        self.sim = SimSource()

    def run(self):
        dt = 1.0 / self.hz
        while self.running:
            self.clock += dt
            try:
                if DEMO:
                    self.sim.step(dt)
                    STATE["source"] = "demo"
                    STATE["connected"] = True
                    LOGGER.tick(dt, self.clock)
                    CONTLOG.tick(dt, self.clock)
                    STATE["recording"] = "rec" if LOGGER.active else "logging"
                    time.sleep(dt)
                    continue
                connected = WS.streaming or DS1.connected
                STATE["source"] = "ds1" if connected else "disconnected"
                STATE["connected"] = connected
                LOGGER.watchdog()
                CONTLOG.watchdog()
                fresh = WS.last_frame and time.time() - WS.last_frame < 1.5
                STATE["recording"] = (("rec" if LOGGER.active else "logging")
                                      if connected else "idle")
                if not (connected and fresh):
                    with LOCK:
                        for k in TELEMETRY:
                            if k != "t":
                                TELEMETRY[k] = 0
            except Exception as e:
                print("engine error:", e)
            time.sleep(dt)


_FIRST_PAGE = [False]


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if sys.exc_info()[0] in (ConnectionResetError, BrokenPipeError,
                                 ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


class _ChunkedWriter:

    def __init__(self, wfile):
        self.wfile = wfile

    def write(self, data):
        if not data:
            return 0
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        return len(data)

    def flush(self):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    DOWNLOAD_CHUNK = 256 * 1024

    def _send_file(self, fpath, ctype, extra=None):
        import shutil
        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with open(fpath, "rb") as f:
            shutil.copyfileobj(f, self.wfile, self.DOWNLOAD_CHUNK)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/logs" or path == "/logs/":
            with LOCK:
                logs = list(LOGGER.logs)
            rows = []
            for m in logs:
                fn = m.get("file", "")
                try:
                    kb = os.path.getsize(os.path.join(LOG_DIR, fn)) / 1024.0
                    size = f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
                except OSError:
                    size = ""
                rows.append(
                    f'<div class="row">'
                    f'<a class="ri" href="/api/log/download?file={fn}">'
                    f'<div class="rt">{m.get("label","Log")}'
                    f' <span class="rs">{m.get("sub","")}</span></div>'
                    f'<div class="rm">{m.get("date","")} {m.get("time","")}'
                    f' &middot; {m.get("samples",0):,} samples &middot; {size}</div></a>'
                    f'<a class="dl" href="/api/log/download?file={fn}">Get</a>'
                    f'<button class="del" onclick="del(\'{fn}\')" '
                    f'aria-label="Delete log">&#10005;</button>'
                    f'</div>')
            body = "".join(rows) or '<p class="empty">No logs recorded yet.</p>'
            total = len(logs)
            html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spooled - Logs</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#060708;color:#f5f7fa;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 padding:24px 16px 48px;-webkit-text-size-adjust:100%}}
.wrap{{max-width:720px;margin:0 auto}}
h1{{font-size:26px;letter-spacing:.14em;margin:0 0 2px;font-weight:800}}
h1 b{{color:#ff2d3f}}
.sub{{color:#98a1ad;font-size:14px;margin-bottom:22px}}
.all{{display:block;text-align:center;background:#ff2d3f;color:#fff;text-decoration:none;
 font-weight:700;padding:15px;border-radius:12px;margin-bottom:22px}}
.row{{display:flex;align-items:center;gap:12px;text-decoration:none;color:inherit;
 background:#0b0d10;border:1px solid rgba(255,255,255,.07);border-radius:12px;
 padding:14px 16px;margin-bottom:10px}}
.row:active{{background:#101318}}
.ri{{flex:1;min-width:0;text-decoration:none;color:inherit}}
.rt{{font-weight:600;font-size:16px}}
.rs{{color:#98a1ad;font-weight:400}}
.rm{{color:#586170;font-size:12px;margin-top:3px;
 font-family:ui-monospace,Menlo,monospace}}
.dl{{flex:none;font-size:13px;font-weight:700;color:#ff8a7a;text-decoration:none;
 border:1px solid rgba(255,138,122,.35);border-radius:8px;padding:8px 12px}}
.del{{flex:none;background:none;border:1px solid rgba(255,255,255,.12);color:#6b7482;
 border-radius:8px;width:38px;height:38px;font-size:15px;cursor:pointer;
 font-family:inherit;line-height:1}}
.del:active{{background:rgba(255,45,63,.18);color:#ff8a7a;border-color:#ff2d3f}}
.clr{{display:block;width:100%;text-align:center;background:none;color:#6b7482;
 border:1px solid rgba(255,255,255,.10);border-radius:12px;padding:13px;
 font-size:14px;font-family:inherit;margin-top:14px;cursor:pointer}}
.empty{{color:#586170;text-align:center;padding:40px 0}}
.foot{{color:#586170;font-size:12px;text-align:center;margin-top:26px;line-height:1.6}}
</style></head><body><div class="wrap">
<h1>SPOOL<b>ED</b></h1>
<div class="sub">{total} saved log{'' if total==1 else 's'} &middot; tap any log to download the CSV</div>
{'<a class="all" href="/api/logs/zip">Download all as .zip</a>' if total else ''}
{body}
{'<button class="clr" onclick="delAll()">Delete all logs</button>' if total else ''}
<div class="foot">Connected to the Spooled network.<br>Open your logs in Excel, Sheets, or any log viewer.</div>
</div>
<script>
function post(u,b){{return fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify(b||{{}})}}).then(r=>r.json());}}
function del(f){{if(!confirm('Delete this log?'))return;
  post('/api/log/delete',{{file:f}}).then(()=>location.reload());}}
function delAll(){{if(!confirm('Delete ALL logs? This cannot be undone.'))return;
  post('/api/logs/delete-all',{{}}).then(()=>location.reload());}}
fetch('/api/time').then(r=>r.json()).then(c=>{{if(!c.ok)post('/api/time',{{epoch_ms:Date.now(),source:'phone'}});}}).catch(()=>{{}});
</script></body></html>"""
            return self._send(200, html.encode("utf-8"),
                              "text/html; charset=utf-8")
        if path == "/" or path == "/index.html":
            if not _FIRST_PAGE[0]:
                _FIRST_PAGE[0] = True
                up = _read("/proc/uptime", str, "") or ""
                if up:
                    print("[boot] dash page first requested %.1fs after boot"
                          % float(up.split()[0]))
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/stream":
            return self._stream()
        if path == "/api/status":
            with LOCK:
                return self._json({"state": STATE, "maps": map_list(),
                                   "logs": LOGGER.logs, "settings": SETTINGS})
        if path == "/api/version":
            return self._json({"version": app_version(), "edition": "community"})
        if path == "/api/state":
            with LOCK:
                st = dict(APPSTATE)
            st["version"] = app_version()
            st["notes"] = changelog_for(st["version"])
            return self._json(st)
        if path == "/api/wifi/status":
            return self._json(net_status())
        if path == "/api/time":
            return self._json(clock_status())
        if path == "/api/timezone":
            return self._json({"current": current_timezone(), "zones": timezone_list()})
        if path == "/api/update/progress":
            return self._json(UPDATE)
        if path == "/api/debug":
            return self._json(debug_stats())
        if path == "/api/storage":
            return self._json(storage_info())
        if path == "/api/wifi/scan":
            return self._json(net_scan())
        if path == "/api/logs":
            with LOCK:
                return self._json(LOGGER.logs)
        if path == "/api/log/download":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            safe = os.path.basename((q.get("file") or [""])[0])
            fpath = os.path.join(LOG_DIR, safe)
            if not safe.endswith(".csv") or not os.path.isfile(fpath):
                return self._send(404, b"not found")
            return self._send_file(
                fpath, "text/csv",
                {"Content-Disposition": 'attachment; filename="%s"' % safe})
        if path == "/api/logs/zip":
            import zipfile
            names = sorted(n for n in os.listdir(LOG_DIR) if n.endswith(".csv"))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Disposition",
                             'attachment; filename="spooled-logs.zip"')
            self.end_headers()
            sink = _ChunkedWriter(self.wfile)
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
                for fn in names:
                    z.write(os.path.join(LOG_DIR, fn), fn)
            self.wfile.write(b"0\r\n\r\n")
            return
        if path == "/api/ds1/dtc":
            rep = dtc_report()
            rep["hint"] = ("simulated fault codes (--demo)" if rep.get("demo")
                           else "reading DTCs from the DS1 stream" if rep["available"]
                           else "DFES channels not in the live layout yet - is the "
                           "9997 WebSocket streaming? (check /api/ds1/live)")
            rep["table"] = dtc_table_status()
            return self._json(rep)
        if path == "/api/log/status":
            with LOCK:
                ped = float(TELEMETRY.get("pedal", 0))
                st = {"active": LOGGER.active, "trigger": LOGGER.trigger,
                      "manual_request": LOGGER.manual_request,
                      "rows_written": LOGGER.rows,
                      "elapsed": round(LOGGER.dur, 1),
                      "max_seconds": LOGGER.MAX_LOG_SEC,
                      "stop_reason": LOGGER.stop_reason,
                      "autolog_enabled": bool(SETTINGS.get("autolog", True)),
                      "throttle_on_at": LOGGER.THROTTLE_ON, "pedal_now": round(ped, 1),
                      "log_count": len(LOGGER.logs),
                      "log_dir": LOG_DIR,
                      "last_write": getattr(LOGGER, "last_file", None),
                      "last_error": getattr(LOGGER, "last_error", ""),
                      "streaming": WS.streaming}
            st["hint"] = (
                "Auto-log fires only when pedal > %g%%; you're at %g%% now. "
                "Tap 'Start logging' for a manual log at idle." %
                (LOGGER.THROTTLE_ON, round(ped, 1))
                if not LOGGER.active else "logging now")
            return self._json(st)
        if path == "/api/ds1":
            with LOCK:
                return self._json(STATE["ds1"])
        if path == "/api/brightness":
            return self._json({"level": SETTINGS.get("brightness", 100),
                               "mode": BRIGHT["mode"]})
        if path == "/api/blend":
            return self._json(blend_status())
        if path == "/api/settings":
            return self._json(SETTINGS)
        if path == "/api/gauges/channels":
            return self._json({"channels": gauge_channels()})
        if path == "/api/monitors":
            with LOCK:
                return self._json({"count": len(MONITORS),
                                   "names": list(MONITORS.keys())[:400]})
        if path == "/api/ds1/live":
            with LOCK:
                tele = {k: round(v, 3) if isinstance(v, float) else v
                        for k, v in TELEMETRY.items()}
                raw_sample = dict(list(RAW_VALUES.items())[:30])
            with LOCK:
                can_sample = dict(list(CAN_VALUES.items())[:30])
            return self._json({
                "streaming": WS.streaming,
                "ws_host": WS.host,
                "frames": WS.frames,
                "channels": len(WS.layout),
                "address": DS1_ADDR.get("ip"),
                "last_rx_age": round(time.time() - WS.last_rx, 1) if WS.last_rx else None,
                "last_frame_age": round(time.time() - WS.last_frame, 1) if WS.last_frame else None,
                "link": dict(WS.stats, connected_for=round(time.time() - WS.stats["connected_at"], 1)
                             if WS.stats.get("connected_at") else None),
                "http_connected": DS1.connected,
                "last_error": STATE["ds1"].get("last_error", ""),
                "gauges": tele,
                "raw_channels_sample": raw_sample,
                "can_channels": len(CAN_VALUES),
                "can_frames_matched": CAN.matched,
                "can_ids_seen": {hex(k): v for k, v in
                                 sorted(CAN.seen_ids.items())},
                "can_sample": can_sample,
                "hint": ("streaming OK" if WS.streaming else
                         "NOT streaming - is the Mac on the DS1 WiFi and is DS2 "
                         "closed on the laptop? (DS1 allows one client at a time)"),
            })
        if path == "/api/ds1/can":
            with LOCK:
                vals = dict(CAN_VALUES)
            return self._json({
                "count": len(vals),
                "matched_frames": CAN.matched,
                "ids_seen": {hex(k): v for k, v in sorted(CAN.seen_ids.items())},
                "values": vals,
            })
        if path == "/api/ds1/raw":
            if not DS1.connected or not DS1.host:
                return self._json({"connected": False,
                                   "hint": "Not connected. Join the DS1 WiFi and connect."})
            try:
                text = _http_get(DS1.host + "/ram/status.csv", timeout=1.5)
                status = parse_status_csv(text)
                mapped = map_status_to_telemetry(status)
                return self._json({"connected": True, "host": DS1.host,
                                   "value_count": len(status),
                                   "mapped_to_gauges": mapped,
                                   "raw_values": status,
                                   "raw_text_head": text[:1500]})
            except Exception as e:
                return self._json({"connected": True, "error": str(e)})
        if path == "/api/log":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            fname = (q.get("file") or [""])[0]
            safe = os.path.basename(fname)
            fpath = os.path.join(LOG_DIR, safe)
            if not safe.endswith(".csv") or not os.path.isfile(fpath):
                return self._json({"cols": [], "rows": []}, 404)
            try:
                with open(fpath, newline="") as f:
                    rdr = csv.reader(f)
                    read_header(rdr)
                    total = sum(1 for r in rdr if r) + 1
                step = max(1, total // 400 + 1) if total > 400 else 1
                rows, lo, hi = [], None, None
                with open(fpath, newline="") as f:
                    rdr = csv.reader(f)
                    header, first = read_header(rdr)
                    cols = [col_key(c) for c in header]
                    rdr = itertools.chain([first], rdr) if first is not None else rdr
                    n = len(cols)
                    dkeys, dsrc = viewer_derived_keys(cols)
                    raw_idx = [(j, c) for j, c in enumerate(cols) if c in dsrc]
                    last = {}
                    cols = cols + dkeys
                    lo = [float("inf")] * len(cols)
                    hi = [float("-inf")] * len(cols)
                    for i, r in enumerate(rdr):
                        if not r:
                            continue
                        vals = []
                        for x in r[:n]:
                            try:
                                vals.append(float(x))
                            except ValueError:
                                vals.append(None)
                        vals += [None] * (n - len(vals))
                        if dkeys:
                            vals += viewer_derived_row(dkeys, raw_idx, vals, last)
                        for j, v in enumerate(vals):
                            if v is None:
                                continue
                            if v < lo[j]:
                                lo[j] = v
                            if v > hi[j]:
                                hi[j] = v
                        if i % step == 0:
                            rows.append(vals)
                lo = [None if x == float("inf") else round(x, 4) for x in lo]
                hi = [None if x == float("-inf") else round(x, 4) for x in hi]
                return self._json({"cols": cols, "rows": rows,
                                   "total": total, "step": step,
                                   "min": lo, "max": hi})
            except Exception as e:
                return self._json({"cols": [], "rows": [], "error": str(e)}, 500)
        safe = os.path.normpath(path.lstrip("/"))
        if safe and not safe.startswith("..") and os.path.isfile(os.path.join(HERE, safe)):
            ctype = ("text/css" if safe.endswith(".css") else
                     "application/javascript" if safe.endswith(".js") else
                     "text/plain")
            return self._file(safe, ctype)
        return self._send(404, b"not found")

    def _file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        except FileNotFoundError:
            return self._send(404, b"not found")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                with LOCK:
                    frame = dict(TELEMETRY)
                    frame["_rec"] = STATE["recording"]
                    frame["_map"] = STATE["map"]
                    frame["_src"] = STATE["source"]
                    frame["_ds1"] = STATE["connected"]
                    frame["_http"] = STATE["ds1"]["connected"]
                    frame["_stream"] = STATE["ds1"].get("streaming", False)
                    frame["_net"] = STATE["internet"]
                    frame["_logging"] = LOGGER.active
                    raw = gauge_raw_keys()
                    if raw:
                        frame["_raw"] = {k: RAW_VALUES.get(k) for k in raw}
                payload = "data: " + json.dumps(frame) + "\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
                time.sleep(1 / 30.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if path == "/api/map":
            try:
                i = int(body.get("index", -1))
            except (TypeError, ValueError):
                i = -1
            if not 0 <= i < MAP_SLOTS:
                return self._json({"ok": False, "map": STATE["map"],
                                   "message": "There's no map %s." % body.get("index")})
            sent = WS.set_map(i)
            if sent or DEMO:
                STATE["map"] = i
            name = map_list()[i]["name"]
            print(f"[map] {name}: " + ("sent to DS1" if sent else
                                       "demo, nothing sent" if DEMO else
                                       "not sent, DS1 not streaming"))
            if not (sent or DEMO):
                return self._json({"ok": False, "map": STATE["map"],
                                   "message": "The DS1 isn't streaming, so the map wasn't switched."})
            return self._json({"ok": True, "map": STATE["map"]})
        if path == "/api/faults/clear":
            STATE["faults"] = []
            return self._json({"ok": True})
        if path == "/api/time":
            ok, out = set_clock(body.get("epoch_ms"), body.get("source", "phone"))
            return self._json({"ok": ok, "detail": out, "clock": clock_status()})
        if path == "/api/timezone":
            tz = str(body.get("tz", "")).strip()
            if tz not in timezone_list():
                return self._json({"ok": False,
                                   "message": "Pick a time zone from the list."}, 400)
            ok, out = _run(["sudo", "-n", "timedatectl", "set-timezone", tz], timeout=10)
            if not ok:
                print("[tz] set %s failed: %s" % (tz, out))
            return self._json({"ok": ok, "current": current_timezone(),
                               "message": "" if ok else "Couldn't set the time zone."})
        if path == "/api/update/check":
            cur = app_version()
            ok, out = _run(["git", "-C", HERE, "fetch", "--tags", "--force",
                            "--prune", "origin"], timeout=45)
            if not ok:
                print("[update] check failed:", out)
                return self._json({"ok": False, "current": cur,
                                   "message": "Couldn't check for updates. "
                                              "Connect the dash to WiFi first."})
            ok2, tags = _run(["git", "-C", HERE, "tag", "--sort=-v:refname"],
                             timeout=10)
            latest = (tags.splitlines()[0].strip() if (ok2 and tags.strip()) else "")
            base = cur.split("-")[0]
            available = bool(latest) and latest != base
            return self._json({"ok": True, "current": cur, "latest": latest,
                               "available": available,
                               "notes": changelog_from_release(latest) if available else []})
        if path == "/api/state":
            with LOCK:
                for k in ("setup_done", "tour_done", "seen_version"):
                    if k in body:
                        APPSTATE[k] = body[k]
                save_state(APPSTATE)
                st = dict(APPSTATE)
            return self._json({"ok": True, "state": st})
        if path == "/api/wifi/connect":
            ssid = str(body.get("ssid", "")).strip()
            pw = str(body.get("pass", ""))
            if not ssid:
                return self._json({"ok": False, "message": "Enter a network name."}, 400)
            ok, out = _run(["bash", NETMODE_SH, "client", ssid, pw], timeout=45)
            if not ok:
                print("[wifi] join %r failed: %s" % (ssid, out))
            return self._json({"ok": ok, "status": net_status(),
                               "message": "" if ok else plain_wifi_error(out, ssid)})
        if path == "/api/wifi/reconnect":
            ok, out = _run(["bash", NETMODE_SH, "reconnect"], timeout=45)
            st = net_status()
            if not ok:
                print("[wifi] reconnect failed:", out)
            return self._json({"ok": ok, "status": st, "message": "" if ok else
                               plain_wifi_error(out, st.get("saved", ""))})
        if path == "/api/wifi/ap":
            ok, out = _run(["bash", NETMODE_SH, "ap"], timeout=30)
            WS.resume()
            if not ok:
                print("[wifi] ap failed:", out)
            return self._json({"ok": ok, "status": net_status(),
                               "message": "" if ok else
                               "Couldn't switch back to DS1 mode. Restart the dash."})
        if path == "/api/wifi/ap-config":
            ssid = str(body.get("ssid", "")).strip()
            pw = str(body.get("pass", ""))
            if not ssid:
                return self._json({"ok": False, "message": "Enter a network name."}, 400)
            if len(pw) < 8:
                return self._json({"ok": False, "message":
                                   "The password needs at least 8 characters."}, 400)
            with LOCK:
                SETTINGS["wifi_ssid"] = ssid
                SETTINGS["wifi_pass"] = pw
            save_settings(SETTINGS)
            applied, out = _run(["bash", NETMODE_SH, "apcfg", ssid, pw], timeout=30)
            if not applied:
                print("[wifi] apcfg not applied:", out)
            return self._json({"ok": True, "applied": applied,
                               "note": None if applied else
                               "Saved. It applies after the dash restarts."})
        if path == "/api/update":
            if UPDATE["stage"] not in ("idle", "done", "error"):
                return self._json({"ok": True, "already": True})
            ref = str(body.get("ref", "")).strip()
            threading.Thread(target=run_update, args=(ref,), daemon=True).start()
            return self._json({"ok": True, "started": True})
        if path == "/api/ds1/ws/pause":
            WS.pause()
            print("[ds1-ws] paused (released connection)")
            return self._json({"ok": True, "paused": True})
        if path == "/api/ds1/ws/resume":
            WS.resume()
            print("[ds1-ws] resumed")
            return self._json({"ok": True, "paused": False})
        if path == "/api/log/delete":
            safe = os.path.basename(str(body.get("file", "")))
            if not safe.endswith(".csv"):
                return self._json({"ok": False, "error": "bad file"}, 400)
            fpath = os.path.join(LOG_DIR, safe)
            try:
                os.remove(fpath)
            except FileNotFoundError:
                pass
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            with LOCK:
                LOGGER.logs = [m for m in LOGGER.logs if m.get("file") != safe]
            load_log_index()
            print(f"[log] deleted {safe}")
            return self._json({"ok": True, "file": safe})
        if path == "/api/logs/delete-all":
            removed = 0
            try:
                for n in os.listdir(LOG_DIR):
                    if n.endswith(".csv"):
                        try:
                            os.remove(os.path.join(LOG_DIR, n))
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
            with LOCK:
                LOGGER.logs = []
            load_log_index()
            print(f"[log] deleted all ({removed})")
            return self._json({"ok": True, "removed": removed})
        if path == "/api/log/start":
            LOGGER.start_manual()
            return self._json({"ok": True, "logging": True})
        if path == "/api/log/stop":
            LOGGER.stop_manual()
            return self._json({"ok": True, "logging": False})
        if path == "/api/ds1/connect":
            host = body.get("host") or None
            if host:
                SETTINGS["ds1_host"] = host
                save_settings(SETTINGS)
            DS1.connect(host)
            return self._json({"ok": True})
        if path == "/api/ds1/disconnect":
            DS1.disconnect()
            return self._json({"ok": True})
        if path == "/api/brightness":
            level, mode = set_brightness(body.get("level", 100))
            if body.get("save"):
                with LOCK:
                    SETTINGS["brightness"] = level
                save_settings(SETTINGS)
            return self._json({"ok": True, "level": level, "mode": mode})
        if path == "/api/settings":
            for k, v in body.items():
                if k == "map_names":
                    v = clean_map_names(v)
                if k in DEFAULT_SETTINGS:
                    SETTINGS[k] = v
            save_settings(SETTINGS)
            if any(k in body for k in ("ds1_host", "ds1_hosts_extra", "log_mask")):
                DS1.connect(None)
            return self._json({"ok": True, "settings": SETTINGS})
        if path == "/api/system/restart":
            print("[system] restart requested")
            threading.Thread(target=_do_restart, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/system/shutdown":
            print("[system] shutdown requested")
            threading.Thread(target=lambda: _run_cmd(["sudo", "shutdown", "-h", "now"]),
                             daemon=True).start()
            return self._json({"ok": True})
        return self._send(404, b"not found")


def _run_cmd(cmd):
    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        print("cmd error:", e)


def _do_restart():
    time.sleep(0.6)
    for cmd in (["sudo", "reboot"], ["sudo", "systemctl", "reboot"], ["reboot"]):
        try:
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                return
        except Exception:
            continue
    print("[system] reboot command unavailable (not a Pi / no sudo). "
          "On the Pi, install.sh grants passwordless reboot.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true",
                    help="run with simulated live data (showroom / screenshots)")
    args = ap.parse_args()

    global DEMO
    DEMO = args.demo
    if DEMO:
        print("  [demo] simulated data enabled")
        STATE["ds1"].update({
            "connected": True, "host": "demo", "since": time.time(),
            "streaming": True,
            "device": {"vin": "Audi S6 (C7)", "tune": "Stage 2 \u00b7 93 oct",
                       "firmware": "demo", "ecu": "simulated"},
        })

    init_log_index()
    level, mode = set_brightness(SETTINGS.get("brightness", 100))
    print("  brightness %d%% (%s)" % (level, mode))

    if not DEMO:
        threading.Thread(target=wifi_powersave_off, daemon=True).start()
        DS1.start()
        WS.start()
    NET_MON.start()
    Engine().start()
    def _on_term(signum, _frame):
        print(f"\nsignal {signum}: closing log and exiting")
        try:
            if LOGGER.active:
                LOGGER.force_stop = True
                LOGGER.manual_request = False
                LOGGER._end(LOGGER.trigger or "manual")
            elif LOGGER.fh:
                LOGGER.fh.flush()
                os.fsync(LOGGER.fh.fileno())
                LOGGER.fh.close()
        except Exception as e:
            print("shutdown flush error:", e)
        os._exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_term)
        except (ValueError, OSError):
            pass

    httpd = QuietHTTPServer((args.host, args.port), Handler)
    print(f"Spooled running: http://{args.host}:{args.port}")
    print(f"  DS1 target: {SETTINGS['ds1_host']} (extras: {SETTINGS['ds1_hosts_extra']})")
    print(f"  logs -> {LOG_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
