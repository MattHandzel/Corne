#!/usr/bin/env python3
"""Labeled, timestamped, PASSIVE keystroke collector for keyboard-layout tuning.

Reads every typing-capable evdev device WITHOUT grabbing it (so it can never
interfere with normal typing) and appends one JSON object per key event to a
daily log. Each event is labeled by which physical keyboard produced it
(totem / corne / laptop / ...), which is the whole point: the analyzer tunes
the TOTEM/Corne ZMK timing separately from the laptop.

Because the host sees the keyboard's *resolved* output, correction patterns
like "oe <backspace><backspace> <esc>" are directly observable and map onto
ZMK knobs (that one = the esc combo's timeout-ms being too tight).

Output: ~/notes/life-logging/key-logging/keylog-YYYYMMDD.jsonl  (mode 0600)
  {"t": 1780700000.123, "dev": "totem", "raw": "ZMK Project Matt's TOTEM Keyboard",
   "code": 18, "key": "KEY_E", "val": 1}          # val: 1=down 0=up 2=autorepeat

Runs as an ordinary user (must be in the `input` group). No root required.
"""
import json
import os
import re
import selectors
import sys
import time
from datetime import datetime

import evdev
from evdev import ecodes

RESCAN_SEC = 3.0                 # how often to look for hotplugged keyboards
OUT_DIR = os.path.expanduser("~/notes/life-logging/key-logging")


def label_for(name: str) -> str:
    """Map a device name to a stable, coarse keyboard label."""
    n = name.lower()
    if "totem" in n:
        return "totem"
    if "corne" in n or "matt's keyboard" in n:
        return "corne"
    if "kanata" in n:
        return "laptop"                      # kanata's remapped virtual output
    if "at translated set 2" in n:
        return "laptop-internal"
    if "1025174b" in n and "keyboard" in n:
        return "laptop-acer"
    return "other:" + re.sub(r"[^a-z0-9]+", "-", n).strip("-")


def is_typing_keyboard(dev: evdev.InputDevice) -> bool:
    """True only for devices that can emit letters (excludes mice, lids,
    power buttons, consumer-control-only remotes, etc.)."""
    caps = dev.capabilities()
    keys = caps.get(ecodes.EV_KEY, [])
    return ecodes.KEY_A in keys and ecodes.KEY_Z in keys


def keyname(code: int) -> str:
    k = ecodes.KEY.get(code) or ecodes.BTN.get(code)
    if isinstance(k, (list, tuple)):
        k = k[0]
    return k or f"CODE_{code}"


class Collector:
    def __init__(self):
        self.sel = selectors.DefaultSelector()
        self.open_devs: dict[str, evdev.InputDevice] = {}   # path -> device
        self._out_path = None
        self._out = None

    # ---- output file (daily roll, 0600) -------------------------------------
    def _outfile(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"keylog-{datetime.now():%Y%m%d}.jsonl")
        if path != self._out_path:
            if self._out:
                self._out.close()
            # 0600: keystrokes can include secrets; keep it owner-only.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._out = os.fdopen(fd, "a", buffering=1)
            self._out_path = path
        return self._out

    def write(self, obj):
        self._outfile().write(json.dumps(obj, separators=(",", ":")) + "\n")

    # ---- device management --------------------------------------------------
    def rescan(self):
        seen = set()
        for path in evdev.list_devices():
            seen.add(path)
            if path in self.open_devs:
                continue
            try:
                dev = evdev.InputDevice(path)
                if not is_typing_keyboard(dev):
                    dev.close()
                    continue
                self.sel.register(dev.fd, selectors.EVENT_READ, dev)
                self.open_devs[path] = dev
                print(f"[keylog] + {label_for(dev.name):16s} {dev.name!r} ({path})",
                      file=sys.stderr, flush=True)
            except (OSError, PermissionError) as e:
                print(f"[keylog] skip {path}: {e}", file=sys.stderr)
        # drop devices that vanished
        for path in list(self.open_devs):
            if path not in seen:
                self._drop(self.open_devs[path], path)

    def _path_of(self, dev):
        for p, d in self.open_devs.items():
            if d is dev:
                return p
        return None

    def _drop(self, dev, path=None):
        if path is None:
            path = self._path_of(dev)
        try:
            self.sel.unregister(dev.fd)
        except (KeyError, ValueError, OSError):
            pass
        try:
            dev.close()
        except OSError:
            pass
        if path is not None:
            self.open_devs.pop(path, None)
        print(f"[keylog] - {dev.name!r} ({path})", file=sys.stderr, flush=True)

    # ---- main loop ----------------------------------------------------------
    def run(self):
        self.rescan()
        next_scan = time.monotonic() + RESCAN_SEC
        while True:
            timeout = max(0.0, next_scan - time.monotonic())
            for key, _ in self.sel.select(timeout=timeout):
                dev = key.data
                try:
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY:
                            continue
                        self.write({
                            "t": round(ev.timestamp(), 3),
                            "dev": label_for(dev.name),
                            "raw": dev.name,
                            "code": ev.code,
                            "key": keyname(ev.code),
                            "val": ev.value,
                        })
                except OSError:
                    # device yanked mid-read (BT drop / unplug)
                    self._drop(dev)
            if time.monotonic() >= next_scan:
                self.rescan()
                next_scan = time.monotonic() + RESCAN_SEC


if __name__ == "__main__":
    try:
        Collector().run()
    except KeyboardInterrupt:
        pass
