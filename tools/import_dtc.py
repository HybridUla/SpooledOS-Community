#!/usr/bin/env python3

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
OUT = os.path.join(APP_DIR, "dtc_table.json")
DEFAULT_SRC = "http://ds1.local/"


def extract_object(text, name):
    marker = "var %s" % name
    i = text.find(marker)
    if i < 0:
        raise LookupError("no '%s' in the page - is this the DS1 app?" % name)
    start = text.find("{", i)
    if start < 0:
        raise LookupError("'%s' found but no object after it" % name)

    depth, in_str, esc = 0, False, False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:j + 1])
    raise ValueError("'%s' object is not closed" % name)


def load(src):
    if src.startswith("http://") or src.startswith("https://"):
        print("[dtc] fetching %s" % src)
        with urllib.request.urlopen(src, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    print("[dtc] reading %s" % src)
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    try:
        page = load(src)
    except Exception as e:
        print("[dtc] could not read %s: %s" % (src, e))
        if src == DEFAULT_SRC:
            print("[dtc] the DS1 has to be powered and on its own WiFi for this.")
            print("[dtc] otherwise save the page in a browser and pass the file:")
            print("[dtc]   python3 tools/import_dtc.py ~/Downloads/ds1.html")
        return 1

    tables = {}
    for name, key in (("dtc_lookup", "iso"), ("vag_lookup", "vag")):
        try:
            tables[key] = extract_object(page, name)
            print("[dtc] %-10s %5d entries" % (name, len(tables[key])))
        except Exception as e:
            print("[dtc] %s: %s" % (name, e))

    if not tables.get("iso"):
        print("[dtc] nothing usable found - not writing anything.")
        return 1

    iso = tables["iso"]
    for probe in ("P0087", "P0299", "P0300"):
        if probe not in iso:
            print("[dtc] warning: %s missing - table looks incomplete" % probe)

    payload = {
        "source": src,
        "iso": iso,
        "vag": tables.get("vag", {}),
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
    os.replace(tmp, OUT)
    size = os.path.getsize(OUT)
    print("[dtc] wrote %s (%.0f KB, %d ISO + %d VAG)"
          % (OUT, size / 1024.0, len(iso), len(tables.get("vag", {}))))
    print("[dtc] restart the Spooled service to pick it up:")
    print("[dtc]   sudo systemctl restart spooled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
