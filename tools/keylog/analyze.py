#!/usr/bin/env python3
"""Turn collected keystroke logs into concrete ZMK timing recommendations.

Reads keylog-*.jsonl (from collector.py), for ONE device (default: totem),
and detects correction/misfire patterns whose fix is a timing parameter:

  * combo late-misfire   "o e <bksp><bksp> <esc>"  -> esc combo timeout-ms too low
  * combo accidental     "<esc> <bksp> o e"        -> timeout-ms too high / need idle
  * homerow accidental hold  "<GUI> <bksp> ..."    -> hold-tap tapping-term / prior-idle
  * key chatter          same key twice in <25 ms  -> debounce too low
  * plus corpus stats    (typing speed, correction rate, top erased bigrams,
                          hold-time and roll-gap distributions)

Then it prints a markdown report with current->proposed values and the
evidence behind each suggestion. Read-only: it never edits your keymap.

  ./run.sh analyze.py ~/notes/life-logging/key-logging/keylog-*.jsonl \
      --device totem --keymap ../../config/totem.keymap
"""
import argparse
import glob
import json
import re
import statistics as st
import sys
from collections import Counter, defaultdict

# ---- key groups -------------------------------------------------------------
BKSP = "KEY_BACKSPACE"
MODS = {"KEY_LEFTMETA", "KEY_RIGHTMETA", "KEY_LEFTALT", "KEY_RIGHTALT",
        "KEY_LEFTCTRL", "KEY_RIGHTCTRL", "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
# On the TOTEM/Corne base layer these modifiers originate only from home-row
# hold-taps, so a bare mod-down is a home-row hold decision.
HOMEROW_MODS = {"KEY_LEFTMETA", "KEY_RIGHTMETA", "KEY_LEFTALT", "KEY_RIGHTALT",
                "KEY_LEFTCTRL", "KEY_RIGHTCTRL"}

# Combos worth tuning: rolled letters -> produced key. Derived from the keymap
# (esc = home O+E, tab = top ,+.). Positions in the .keymap; letters here.
COMBOS = [
    {"name": "esc", "letters": {"KEY_O", "KEY_E"}, "out": "KEY_ESC",
     "knob": "esc combo timeout-ms"},
    {"name": "tab", "letters": {"KEY_COMMA", "KEY_DOT"}, "out": "KEY_TAB",
     "knob": "tab combo timeout-ms"},
]

# windows (ms)
ROLL_MS = 120          # two letters this close = an intended roll
CORRECTION_MS = 1500   # a correction follows within this
HOLD_ACCIDENT_MS = 600 # mod-then-backspace this close = accidental hold
CHATTER_MS = 25        # same key twice faster than this = switch chatter


def load(paths, device):
    evs = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("dev") == device:
                    evs.append(o)
    evs.sort(key=lambda o: o["t"])
    return evs


def downs(evs):
    """Fresh key presses (val==1), as (t_ms, key)."""
    return [(int(o["t"] * 1000), o["key"]) for o in evs if o.get("val") == 1]


def erase_bursts(evs):
    """List of (start_ms, end_ms, count) for runs of backspace (down+repeat)."""
    bursts, cur = [], None
    for o in evs:
        t = int(o["t"] * 1000)
        if o["key"] == BKSP and o["val"] in (1, 2):
            if cur and t - cur[1] < 400:
                cur = (cur[0], t, cur[2] + 1)
            else:
                if cur:
                    bursts.append(cur)
                cur = (t, t, 1)
        elif o["key"] == BKSP and o["val"] == 0:
            continue
    if cur:
        bursts.append(cur)
    return bursts


# ---- detectors --------------------------------------------------------------
def detect_combo_misfires(d):
    """Returns per-combo dict with late/accidental counts + failed roll gaps."""
    res = {c["name"]: {"late": 0, "accidental": 0, "gaps": []} for c in COMBOS}
    n = len(d)
    for i in range(n):
        t, k = d[i]
        for c in COMBOS:
            L, out = c["letters"], c["out"]
            # LATE: letter, other letter within ROLL, then >=2 bksp, then out
            if k in L and i + 1 < n:
                t2, k2 = d[i + 1]
                if k2 in L and k2 != k and (t2 - t) <= ROLL_MS:
                    j, bs = i + 2, 0
                    while j < n and d[j][0] - t2 <= CORRECTION_MS:
                        if d[j][1] == BKSP:
                            bs += 1
                        if d[j][1] == out and bs >= 2:
                            res[c["name"]]["late"] += 1
                            res[c["name"]]["gaps"].append(t2 - t)
                            break
                        if d[j][1] not in (BKSP, out) and bs == 0:
                            break
                        j += 1
            # ACCIDENTAL: out fired, quickly undone, then letters typed apart.
            # The undo must directly follow the out — if any ordinary key is
            # committed first, the out was intended (avoids bridging into a
            # later, unrelated sequence).
            if k == out and i + 1 < n:
                j, sawbs = i + 1, False
                while j < n and d[j][0] - t <= CORRECTION_MS:
                    kk = d[j][1]
                    if kk == BKSP:
                        sawbs = True
                    elif kk == out:
                        pass
                    elif sawbs and kk in L:
                        res[c["name"]]["accidental"] += 1
                        break
                    elif not sawbs:
                        break
                    j += 1
    return res


def detect_homerow_holds(evs, d):
    """Accidental home-row holds: a bare mod-down closely followed by a
    correction. Also collect mod hold-times (down->up)."""
    accidental = Counter()
    holdur = defaultdict(list)
    # hold durations
    pending = {}
    for o in evs:
        t = int(o["t"] * 1000)
        if o["key"] in HOMEROW_MODS and o["val"] == 1:
            pending[o["key"]] = t
        elif o["key"] in HOMEROW_MODS and o["val"] == 0 and o["key"] in pending:
            holdur.setdefault(o["key"], []).append(t - pending.pop(o["key"]))
    # accidental = mod down then backspace within HOLD_ACCIDENT_MS
    bursts = erase_bursts(evs)
    burst_starts = [b[0] for b in bursts]
    for t, k in d:
        if k in HOMEROW_MODS:
            if any(0 <= bs - t <= HOLD_ACCIDENT_MS for bs in burst_starts):
                accidental[k] += 1
    return accidental, holdur


def detect_chatter(d):
    hits = Counter()
    for i in range(1, len(d)):
        if d[i][1] == d[i - 1][1] and d[i][1] != BKSP:
            if d[i][0] - d[i - 1][0] < CHATTER_MS:
                hits[d[i][1]] += 1
    return hits


def erased_ngrams(evs, d, n=2, top=12):
    """Most common sequences typed immediately before a backspace burst."""
    bursts = erase_bursts(evs)
    grams = Counter()
    for start, _, _ in bursts:
        pre = [k for (t, k) in d if t < start and k != BKSP]
        if len(pre) >= n:
            grams["".join(_short(x) for x in pre[-n:])] += 1
    return grams.most_common(top)


def _short(key):
    m = re.match(r"KEY_(.+)", key)
    s = m.group(1) if m else key
    return {"BACKSPACE": "⌫", "SPACE": "␣", "ENTER": "⏎", "ESC": "⎋",
            "COMMA": ",", "DOT": ".", "SEMICOLON": ";", "APOSTROPHE": "'"}.get(
                s, s.lower() if len(s) == 1 else s)


def pctl(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[i]


# ---- current keymap values (best effort, for current->proposed) -------------
def keymap_values(path):
    v = {}
    if not path:
        return v
    try:
        txt = open(path).read()
    except OSError:
        return v
    m = re.search(r"esc\s*\{[^}]*?timeout-ms\s*=\s*<(\d+)>", txt, re.S)
    if m:
        v["esc combo timeout-ms"] = int(m.group(1))
    for beh in ("hml", "hmr"):
        b = re.search(beh + r":\s*" + beh + r"\s*\{(.*?)\};", txt, re.S)
        if b:
            body = b.group(1)
            for key in ("tapping-term-ms", "require-prior-idle-ms"):
                mm = re.search(key + r"\s*=\s*<(\d+)>", body)
                if mm:
                    v[f"{beh} {key}"] = int(mm.group(1))
    return v


# ---- report -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="keylog-*.jsonl (globs ok)")
    ap.add_argument("--device", default="totem")
    ap.add_argument("--keymap", default=None, help="path to <kb>.keymap for current values")
    args = ap.parse_args()

    paths = []
    for f in args.files:
        paths.extend(sorted(glob.glob(f)) or [f])
    evs = load(paths, args.device)
    d = downs(evs)
    cur = keymap_values(args.keymap)

    if len(d) < 50:
        print(f"# Keylog analysis — device: {args.device}\n")
        print(f"Only **{len(d)} keypresses** collected for `{args.device}` so far.")
        print("\nLet the collector run for a few days of real typing, then re-run.")
        print("Recommendations need a corpus (aim for 5k+ presses) to be trustworthy.")
        return

    total = len(d)
    bs = sum(1 for _, k in d if k == BKSP)
    intervals = [d[i][0] - d[i - 1][0] for i in range(1, total)
                 if 0 < d[i][0] - d[i - 1][0] < 2000]
    combo = detect_combo_misfires(d)
    accid, holdur = detect_homerow_holds(evs, d)
    chatter = detect_chatter(d)
    grams = erased_ngrams(evs, d)

    P = lambda x: f"{pctl(intervals, x)} ms" if intervals else "n/a"
    out = []
    w = out.append
    w(f"# Keylog analysis — device: `{args.device}`\n")
    w(f"- **{total}** keypresses, **{bs}** backspaces "
      f"(**{100*bs/total:.1f}%** correction rate)")
    if intervals:
        w(f"- inter-key interval: p50 **{P(50)}**, p90 **{P(90)}**, p95 **{P(95)}**")
        wpm = 60000 / (st.median(intervals) * 5) if st.median(intervals) else 0
        w(f"- rough typing speed: **~{wpm:.0f} wpm** (median-interval estimate)")
    w("")

    # --- recommendations ---
    recs = []

    w("## Combo misfires\n")
    any_combo = False
    for c in COMBOS:
        r = combo[c["name"]]
        if r["late"] or r["accidental"]:
            any_combo = True
            w(f"**{c['name']}** ({'+'.join(sorted(_short(x) for x in c['letters']))} "
              f"→ {_short(c['out'])}): "
              f"{r['late']}× late-misfire (typed the letters, erased, then {_short(c['out'])}), "
              f"{r['accidental']}× accidental-fire.")
            if r["late"] >= 3 and r["late"] > r["accidental"]:
                gp = pctl(r["gaps"], 85) or 0
                curv = cur.get(c["knob"])
                proposed = max((curv or 50) + 10, min(gp + 10, 80))
                recs.append((c["knob"], curv, proposed,
                             f"{r['late']} late-misfires; failed rolls landed "
                             f"at p85={gp}ms > current {curv or '50'}ms window"))
            if r["accidental"] >= 3 and r["accidental"] > r["late"]:
                curv = cur.get(c["knob"])
                proposed = max(30, (curv or 50) - 10)
                recs.append((c["knob"], curv, proposed,
                             f"{r['accidental']} accidental fires; combo triggering "
                             f"on non-intended rolls"))
    if not any_combo:
        w("_None detected._")
    w("")

    w("## Home-row modifier holds\n")
    if accid:
        for k, n in accid.most_common():
            hs = holdur.get(k, [])
            hp = f", hold p50={pctl(hs,50)}ms" if hs else ""
            w(f"- **{_short(k)}**: {n} accidental-hold corrections "
              f"(mod fired then backspace){hp}")
        worst = accid.most_common(1)[0][1]
        if worst >= 3 and intervals:
            p90 = pctl(intervals, 90)
            recs.append(("hml/hmr require-prior-idle-ms",
                         cur.get("hml require-prior-idle-ms"),
                         max(p90 + 10, (cur.get("hml require-prior-idle-ms") or 150)),
                         f"{worst} accidental holds; set prior-idle ≥ fast-roll p90 "
                         f"({p90}ms) so quick rolls can't trigger a hold"))
    else:
        w("_None detected._")
    w("")

    w("## Key chatter (switch double-fire)\n")
    if chatter:
        for k, n in chatter.most_common():
            w(f"- **{_short(k)}**: {n}× repeated within {CHATTER_MS}ms")
        recs.append(("CONFIG_ZMK_KSCAN_DEBOUNCE_*_MS", 7, 9,
                     "chatter observed; raise debounce a notch"))
    else:
        w("_None detected._")
    w("")

    w("## Most-erased sequences (what you retype most)\n")
    if grams:
        w(" ".join(f"`{g}`×{n}" for g, n in grams))
    else:
        w("_n/a_")
    w("")

    w("## → Recommended timing changes\n")
    recs = [r for r in recs if r[1] is None or r[2] != r[1]]   # drop no-ops
    if recs:
        w("| parameter | current | proposed | why |")
        w("|---|---|---|---|")
        for name, curv, prop, why in recs:
            w(f"| `{name}` | {curv if curv is not None else '?'} | **{prop}** | {why} |")
    else:
        w("_No confident change yet — either clean typing or not enough data._")
    w("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
