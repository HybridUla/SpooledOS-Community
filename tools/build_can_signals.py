#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "can_signals.json")

SIGNALS = [
    (0x081, "Motor_02",    "MO_Status_Zylabschalt_01",     "DS1:Cyl Deactivate Status"),
    (0x081, "Motor_02",    "MO_Gangposition",              "DS1:Gear Position"),
    (0x084, "ESP_06",      "ESP_Teilsollwinkel",           "DS1:Partial Target Angle"),
    (0x084, "ESP_06",      "ESP_Winkeleingriff",           "DS1:Angular Engagement"),
    (0x086, "LWI_01",      "LWI_Lenkradwinkel",            "DS1:Steering Wheel Angle"),
    (0x086, "LWI_01",      "LWI_Lenkradw_Geschw",          "DS1:Steering Wheel Speed"),
    (0x100, "ESP_01",      "ESP_v_Signal",                 "DS1:Vehicle Speed"),
    (0x101, "ESP_02",      "ESP_Stillstandsflag",          "DS1:Standstill Flag"),
    (0x101, "ESP_02",      "ESP_Querbeschleunigung",       "DS1:Lateral Accel"),
    (0x101, "ESP_02",      "ESP_Laengsbeschl",             "DS1:Longitudinal Accel"),
    (0x101, "ESP_02",      "ESP_Gierrate",                 "DS1:Yaw Rate"),
    (0x103, "ESP_03",      "ESP_VL_Radgeschw",             "DS1:Wheel Speed Front Left"),
    (0x103, "ESP_03",      "ESP_VR_Radgeschw",             "DS1:Wheel Speed Front Right"),
    (0x103, "ESP_03",      "ESP_HL_Radgeschw",             "DS1:Wheel Speed Rear Left"),
    (0x103, "ESP_03",      "ESP_HR_Radgeschw",             "DS1:Wheel Speed Rear Right"),
    (0x105, "Motor_03",    "MO_Drehzahl_01",               "DS1:Engine RPM"),
    (0x105, "Motor_03",    "MO_Moment_im_Leerlauf",        "DS1:Engine Idling"),
    (0x105, "Motor_03",    "MO_Motor_laeuft",              "DS1:Engine Running"),
    (0x105, "Motor_03",    "MO_Solldrehz_Leerlauf",        "DS1:Target Idle Speed"),
    (0x105, "Motor_03",    "MO_Fahrpedalrohwert_01",       "DS1:Pedal"),
    (0x106, "ESP_05",      "ESP_Bremsdruck",               "DS1:Brake Pressure"),
    (0x106, "ESP_05",      "ESP_Rueckwaertsfahrt_erkannt", "DS1:Reverse Detected"),
    (0x106, "ESP_05",      "ESP_Status_Anfahrhilfe",       "DS1:Traction Assist Status"),
    (0x107, "Motor_04",    "MO_Oeldruck",                  "DS1:Oil Pressure"),
    (0x107, "Motor_04",    "MO_Ladedruck",                 "DS1:Boost Pressure"),
    (0x111, "TSK_05",      "TSK_Fahrzeugmasse",            "DS1:Vehicle Mass"),
    (0x111, "TSK_05",      "TSK_Fahrzeugmasse_02",         "DS1:Vehicle Mass 2"),
    (0x114, "Motor_10",    "MO_Fahrpedalgradient",         "DS1:Accel Pedal Gradient"),
    (0x114, "Motor_10",    "MO_Schubabschaltung",          "DS1:Fuel Cut-Off"),
    (0x309, "BEM_01",      "BEM_P_Generator",              "DS1:Alternator Output"),
    (0x30B, "Kombi_01",    "KBI_ABS_Lampe",                "DS1:ABS Lamp"),
    (0x30B, "Kombi_01",    "KBI_ESP_Lampe",                "DS1:ESP Lamp"),
    (0x30B, "Kombi_01",    "KBI_BKL_Lampe",                "DS1:BKL Lamp"),
    (0x30B, "Kombi_01",    "KBI_Airbag_Lampe",             "DS1:Airbag Lamp"),
    (0x30B, "Kombi_01",    "KBI_Anzeigestatus_ACC",        "DS1:Display ACC Lamp"),
    (0x30B, "Kombi_01",    "KBI_Oeldruck_Schalter",        "DS1:Oil Pressure Lamp"),
    (0x30B, "Kombi_01",    "KBI_Tankwarnung",              "DS1:Tank Warning"),
    (0x30B, "Kombi_01",    "KBI_Variante_USA",             "DS1:USA Variant"),
    (0x30B, "Kombi_01",    "KBI_Oeldruckwarnung",          "DS1:Oil Pressure Warning"),
    (0x30B, "Kombi_01",    "KBI_Handbremse",               "DS1:Parking Brake Lamp"),
    (0x30B, "Kombi_01",    "KBI_angez_Geschw",             "DS1:Indicated Speed"),
    (0x30E, "Motor_05",    "MO_Luftpfad_aktiv",            "DS1:Air Path Active??"),
    (0x39C, "Gateway_05",  "BCM1_02_alt",                  "DS1:BCM_02_alt??"),
    (0x39C, "Gateway_05",  "Wischer_01_alt",               "Wiper_01_alt??"),
    (0x39C, "Gateway_05",  "Klima_02_alt",                 "Climate_02_alt??"),
    (0x39C, "Gateway_05",  "BCM1_01_alt",                  "BCM1_01_alt??"),
    (0x39C, "Gateway_05",  "ESP_07_alt",                   "ESP_07_alt??"),
    (0x39C, "Gateway_05",  "TSG_BT_1_alt",                 "TSG_BT_1_alt??"),
    (0x39C, "Gateway_05",  "TSG_HL_1_alt",                 "TSG_HL_1_alt??"),
    (0x39C, "Gateway_05",  "TSG_HR_1_alt",                 "TSG_HR_1_alt??"),
    (0x39C, "Gateway_05",  "Motor_02_alt",                 "Motor_02_alt??"),
    (0x39C, "Gateway_05",  "FT_Tuer_geoeffnet",            "DS1:FL Door Opened"),
    (0x39C, "Gateway_05",  "BCM1_Aussen_Temp_ungef",       "DS1:Outside Temp"),
    (0x39C, "Gateway_05",  "BCM1_Oeldruck_Schalter",       "DS1:Oil Pressure Switch"),
    (0x39C, "Gateway_05",  "BT_Tuer_geoeffnet",            "DS1:FR Door Opened"),
    (0x39C, "Gateway_05",  "HL_Tuer_geoeffnet",            "DS1:RL Door Opened"),
    (0x39C, "Gateway_05",  "HR_Tuer_geoeffnet",            "DS1:RR Door Opened"),
    (0x440, "Motor_06",    "WIV_Oelmin_Warn",              "DS1:Oil Min Warning"),
    (0x440, "Motor_06",    "WIV_Oelstand",                 "DS1:Oil Level Percent"),
    (0x440, "Motor_06",    "WIV_nicht_betriebswarm",       "DS1:Not At Operating Temp"),
    (0x440, "Motor_06",    "WIV_Ueberfuell_Warn",          "DS1:Oil Overfill Warning"),
    (0x440, "Motor_06",    "WIV_Oelmenge",                 "DS1:Oil Amount"),
    (0x440, "Motor_06",    "WIV_Oelstand_nicht_vorhanden", "DS1:Oil Level Not Available"),
    (0x440, "Motor_06",    "WIV_nachfuellanzeige_ein",     "DS1:Fuel Refill Indicator"),
    (0x440, "Motor_06",    "WIV_Ueberfuell_deaktiv",       "DS1:Overfill Deactivated"),
    (0x440, "Motor_06",    "WIV_Unterfuell_Warn",          "DS1:Underfill Warning"),
    (0x440, "Motor_06",    "MO_Tankdeckel_Lampe",          "DS1:Fuel Cap Lamp"),
    (0x440, "Motor_06",    "MO_Text_Tankdeckelwarn",       "DS1:Fuel Cap Warning"),
    (0x494, "STS_01",      "STS_Standlicht",               "DS1:Parking Light"),
    (0x640, "Motor_07",    "MO_aktives_Getriebeheizen",    "DS1:Trans Heating Active"),
    (0x640, "Motor_07",    "MO_Ansaugluft_Temp",           "DS1:Air Intake Temp"),
    (0x640, "Motor_07",    "MO_Oel_Temp",                  "DS1:Oil Temp"),
    (0x640, "Motor_07",    "MO_Kuehlmittel_Temp",          "DS1:Coolant Temp"),
    (0x640, "Motor_07",    "MO_Getriebe_kuehlen",          "DS1:Trans Cooling"),
    (0x640, "Motor_07",    "MO_Heizungspumpenansteuerung", "DS1:Heating Pump Percent"),
    (0x644, "Gateway_11",  "BH_Blinker_li",                "DS1:Turn Signal Left"),
    (0x644, "Gateway_11",  "BH_Blinker_re",                "DS1:Turn Signal Right"),
    (0x644, "Gateway_11",  "BH_Fernlicht",                 "DS1:High Beam Active"),
    (0x644, "Gateway_11",  "BH_Lichthupe",                 "DS1:Flashers??"),
    (0x644, "Gateway_11",  "LV_Abblendlicht_Anzeige",      "DS1:Low Beam Active"),
    (0x644, "Gateway_11",  "BCM1_Licht_Dunkelheit_aktiv",  "DS1:Light Darkness Active??"),
    (0x644, "Gateway_11",  "LV_Nebelschlusslicht_Anzeige", "DS1:Rear Fog Light"),
    (0x644, "Gateway_11",  "BCM1_Linksverkehr",            "DS1:Left-hand Traffic??"),
    (0x644, "Gateway_11",  "BCM2_P_verriegelt",            "DS1:P Locked??"),
    (0x644, "Gateway_11",  "BCM1_Allwetterlicht_Anf",      "DS1:All-Weather Light??"),
    (0x647, "Motor_09",    "SCR_Reichweite",               "DS1:Estimated Range"),
    (0x647, "Motor_09",    "MO_Kraftstofffilter_Wasser",   "DS1:Fuel Filter Water Present"),
    (0x6B2, "Diagnose_01", "KBI_Kilometerstand",           "DS1:Odometer"),
]


def parse_dbc(text):
    msgs, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^BO_ (\d+) (\w+)\s*:\s*(\d+)", line)
        if m:
            cur = {"name": m.group(2), "dlc": int(m.group(3)), "signals": {}}
            msgs[int(m.group(1))] = cur
            continue
        m = re.match(r'^\s+SG_ (\w+)\s*(M|m\d+)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*'
                     r'\(([^,]+),([^)]+)\)\s*\[([^|]*)\|([^\]]*)\]\s*"([^"]*)"', line)
        if m and cur is not None:
            cur["signals"][m.group(1)] = {
                "start": int(m.group(3)), "length": int(m.group(4)),
                "byte_order": "little_endian" if m.group(5) == "1" else "big_endian",
                "signed": m.group(6) == "-", "multiplexed": bool(m.group(2)),
                "scale": float(m.group(7)), "offset": float(m.group(8)),
                "unit": m.group(11)}
        elif not line.startswith(" "):
            cur = None
    return msgs


def num(x):
    return int(x) if float(x).is_integer() else x


def build(msgs):
    out, errors = {}, []
    for mid, mname, sname, channel in SIGNALS:
        msg = msgs.get(mid)
        if not msg or msg["name"] != mname:
            errors.append("0x%03X %s: the .dbc has %s" % (mid, mname, msg["name"] if msg else "no such id"))
            continue
        sig = msg["signals"].get(sname)
        if not sig:
            errors.append("%s.%s: no such signal" % (mname, sname))
            continue
        if sig["byte_order"] != "little_endian" or sig["multiplexed"]:
            errors.append("%s.%s: only plain little-endian signals are decoded" % (mname, sname))
            continue
        m = out.setdefault(mid, {"id": mid, "name": mname, "signals": []})
        m["signals"].append({
            "channel": channel, "signal": sname,
            "start": sig["start"], "length": sig["length"],
            "byte_order": sig["byte_order"], "signed": sig["signed"],
            "scale": num(sig["scale"]), "offset": num(sig["offset"]),
            "unit": sig["unit"]})
    if errors:
        raise SystemExit("can_signals: does not match the .dbc:\n  " + "\n  ".join(errors))
    return [out[k] for k in sorted(out)]


def check(msgs, table):
    bad, n = [], 0
    for m in table.get("messages", []):
        msg = msgs.get(m["id"])
        if not msg or msg["name"] != m["name"]:
            bad.append("0x%03X %s: not in the .dbc under that id and name" % (m["id"], m["name"]))
            continue
        for s in m["signals"]:
            n += 1
            d = msg["signals"].get(s["signal"])
            if not d:
                bad.append("%s.%s: not in the .dbc" % (m["name"], s["signal"]))
                continue
            for k in ("start", "length", "byte_order", "signed", "unit"):
                if s[k] != d[k]:
                    bad.append("%s.%s %s: %r here, %r in the .dbc" % (m["name"], s["signal"], k, s[k], d[k]))
            for k in ("scale", "offset"):
                if float(s[k]) != d[k]:
                    bad.append("%s.%s %s: %r here, %r in the .dbc" % (m["name"], s["signal"], k, s[k], d[k]))
    return n, bad


def main():
    ap = argparse.ArgumentParser(description="Build can_signals.json from opendbc's vw_mlb.dbc.")
    ap.add_argument("dbc", help="path to opendbc's vw_mlb.dbc")
    ap.add_argument("--ref", default="", help="the opendbc commit the .dbc came from")
    ap.add_argument("--check", action="store_true", help="verify can_signals.json, write nothing")
    ap.add_argument("-o", "--out", default=OUT)
    a = ap.parse_args()
    raw = open(a.dbc, "rb").read()
    msgs = parse_dbc(raw.decode("latin-1"))
    if a.check:
        with open(a.out, encoding="utf-8") as f:
            n, bad = check(msgs, json.load(f))
        if bad:
            print("%d signals checked, %d mismatches:\n  %s" % (n, len(bad), "\n  ".join(bad)))
            sys.exit(1)
        print("%d signals checked: start bit, length, byte order, sign, scale, offset "
              "and unit all match %s" % (n, a.dbc))
        return
    messages = build(msgs)
    table = {
        "about": "CAN signals the dash decodes. Generated by tools/build_can_signals.py "
                 "from opendbc's vw_mlb.dbc - edit SIGNALS there, not this file.",
        "source": {"project": "https://github.com/commaai/opendbc",
                   "file": "opendbc/dbc/vw_mlb.dbc", "ref": a.ref,
                   "sha256": hashlib.sha256(raw).hexdigest(),
                   "license": "MIT, Copyright (c) 2020, Comma.ai, Inc. (see THIRD_PARTY_NOTICES)"},
        "messages": messages,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("wrote %s: %d messages, %d signals"
          % (a.out, len(messages), sum(len(m["signals"]) for m in messages)))


if __name__ == "__main__":
    main()
