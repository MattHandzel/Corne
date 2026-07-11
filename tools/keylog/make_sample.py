#!/usr/bin/env python3
"""Emit a synthetic keylog to stdout to validate analyze.py's detectors.

Contains: normal typing + several esc-combo late-misfires ("o e <bksp><bksp>
<esc>" with ~65ms roll gaps, above the 50ms combo window) + a couple accidental
home-row holds + one chatter double-fire. Deterministic (no randomness).
"""
import json
import sys

t = 1_780_700_000.0
def ev(key, val=1, dt=0.11):
    global t
    t += dt
    sys.stdout.write(json.dumps(
        {"t": round(t, 3), "dev": "totem", "raw": "synthetic",
         "code": 0, "key": f"KEY_{key}", "val": val}) + "\n")

def tap(key, dt=0.11):
    ev(key, 1, dt); ev(key, 0, 0.02)

def word(s, dt=0.12):
    for ch in s:
        tap(ch.upper(), dt)
    tap("SPACE", dt)

# --- baseline corpus of ordinary typing (well above the 50-press floor) ---
for _ in range(6):
    for wd in ("the", "quick", "brown", "fox", "code", "note", "hello", "world"):
        word(wd)

# --- 5x esc-combo LATE-misfire: roll o+e in 65ms (> 50ms window) -> literal
#     "oe", user hits backspace twice, then presses esc deliberately ---------
for _ in range(5):
    ev("O", 1, 0.30); ev("O", 0, 0.02)
    ev("E", 1, 0.065); ev("E", 0, 0.02)          # 65ms roll gap
    ev("BACKSPACE", 1, 0.20); ev("BACKSPACE", 0, 0.02)
    ev("BACKSPACE", 1, 0.12); ev("BACKSPACE", 0, 0.02)
    ev("ESC", 1, 0.18); ev("ESC", 0, 0.02)

# --- 3x accidental home-row hold: GUI fires, then correction --------------
for _ in range(3):
    ev("LEFTMETA", 1, 0.25); ev("LEFTMETA", 0, 0.09)
    ev("BACKSPACE", 1, 0.15); ev("BACKSPACE", 0, 0.02)

# --- 1x chatter: T double-fires within 12ms -------------------------------
ev("T", 1, 0.30); ev("T", 0, 0.006)
ev("T", 1, 0.012); ev("T", 0, 0.02)
ev("BACKSPACE", 1, 0.15); ev("BACKSPACE", 0, 0.02)
