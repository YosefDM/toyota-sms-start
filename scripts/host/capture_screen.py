#!/usr/bin/env python3
"""
Capture one UI screen for the map / baseline library.

Writes to ui-map/<label>/:
  screen.png        — screenshot (visual audit; NOT committed — may show vehicle/account data)
  window.xml        — raw uiautomator dump (NOT committed)
  signature.json    — the semantic fingerprint: activity + anchor texts/ids + control nodes w/ bounds

The signature is what we diff to detect a real UI change, and what the guarded-tap logic uses to find a
button by identity (and tap its live center) instead of a blind coordinate.

Usage:  python capture_screen.py <label>
Env:    ADB_PATH (default 'adb'), ADB_SERIAL (default emulator-5554)
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path

ADB = os.environ.get("ADB_PATH", "adb")
SERIAL = os.environ.get("ADB_SERIAL", "emulator-5554")
OUT_ROOT = Path(__file__).resolve().parents[2] / "ui-map"


def adb(*args: str, binary: bool = False, timeout: int = 30):
    cp = subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)}: {cp.stderr.decode('utf-8','ignore').strip()}")
    return cp.stdout if binary else cp.stdout.decode("utf-8", "ignore")


def foreground_activity() -> str:
    out = adb("shell", "dumpsys", "activity", "activities")
    m = re.search(r"topResumedActivity=.*?(\S+/\S+)\s", out)
    return m.group(1) if m else ""


def dump_xml() -> str:
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return adb("shell", "cat", "/sdcard/ui.xml")


def parse_nodes(xml: str) -> list[dict]:
    nodes = []
    for m in re.finditer(r"<node\b([^>]*)/?>", xml):
        a = m.group(1)
        def attr(name):
            mm = re.search(rf'{name}="([^"]*)"', a)
            return mm.group(1) if mm else ""
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', a)
        if not b:
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        text = attr("text").strip()
        desc = attr("content-desc").strip()
        rid = attr("resource-id")
        clickable = attr("clickable") == "true"
        if not (text or desc or rid):
            continue
        nodes.append({
            "text": text, "desc": desc, "rid": rid,
            "class": attr("class").split(".")[-1],
            "clickable": clickable,
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
            "bounds": [x1, y1, x2, y2],
        })
    return nodes


def main():
    if len(sys.argv) < 2:
        print("usage: capture_screen.py <label>"); sys.exit(1)
    label = sys.argv[1]
    d = OUT_ROOT / label
    d.mkdir(parents=True, exist_ok=True)

    act = foreground_activity()
    xml = dump_xml()
    (d / "window.xml").write_text(xml, encoding="utf-8")
    nodes = parse_nodes(xml)
    png = adb("shell", "screencap", "-p", binary=True)
    # strip possible CRLF translation from exec of screencap over adb shell
    png = png.replace(b"\r\n", b"\n")
    (d / "screen.png").write_bytes(png)

    # anchors = stable identifying labels/ids on this screen (texts + the resource-id tails)
    anchor_texts = sorted({n["text"] for n in nodes if n["text"]})
    anchor_ids = sorted({n["rid"].split("/")[-1] for n in nodes if n["rid"]})
    sig = {
        "label": label,
        "activity": act,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "anchor_texts": anchor_texts,
        "anchor_ids": anchor_ids,
        "clickables": [n for n in nodes if n["clickable"]],
        "all_nodes": nodes,
    }
    (d / "signature.json").write_text(json.dumps(sig, indent=2), encoding="utf-8")

    print(f"[{label}] activity={act}")
    print(f"  texts({len(anchor_texts)}): {anchor_texts[:16]}")
    print(f"  ids({len(anchor_ids)}): {anchor_ids[:16]}")
    print(f"  clickables: {len(sig['clickables'])}  -> {d}")


if __name__ == "__main__":
    main()
