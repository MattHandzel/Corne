#!/usr/bin/env python3
"""Find layout/timing changes that reduce your real mistakes and effort.

The host sees the keyboard's *resolved output*, so this reconstructs what you
actually did — modeling gestures, not raw key adjacencies:

  * modifiers are state, not content (Ctrl held + Backspace = delete-WORD, a
    deliberate correction tool — NOT the mod "misfiring");
  * navigation (arrows/home/end) is cursor movement, not typing;
  * a correction = text you typed, then deleted, then retyped. The DIFF between
    what you erased and what you replaced it with is the actual error.

From those corrections it classifies errors (transposition / substitution /
doubled / combo-misfire) and, with the keymap geometry, reports ergonomics
(finger load, same-finger bigrams, hand balance) — then recommends concrete
timing/layout changes. `--apply-timing` writes the safe mechanical ones back.

  ./run.sh analyze                      # report for the totem
  ./run.sh analyze --device corne
  python3 analyze.py LOGS --keymap ../../config/totem.keymap --apply-timing
"""
import argparse
import glob
import json
import re
import statistics as st
from collections import Counter

# ---- key classes (evdev names) ---------------------------------------------
CTRL = {"KEY_LEFTCTRL", "KEY_RIGHTCTRL"}
ALT = {"KEY_LEFTALT", "KEY_RIGHTALT"}
GUI = {"KEY_LEFTMETA", "KEY_RIGHTMETA"}
SHIFT = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
MODS = CTRL | ALT | GUI | SHIFT
COMMANDISH = CTRL | ALT | GUI                      # mods that make a chord a command
NAV = {"KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END",
       "KEY_PAGEUP", "KEY_PAGEDOWN", "KEY_INSERT", "KEY_DELETE"}
BKSP = "KEY_BACKSPACE"

_PUNCT = {"SEMICOLON": ";", "COMMA": ",", "DOT": ".", "SLASH": "/",
          "APOSTROPHE": "'", "MINUS": "-", "EQUAL": "=", "LEFTBRACE": "[",
          "RIGHTBRACE": "]", "BACKSLASH": "\\", "GRAVE": "`", "SPACE": " "}


def to_char(key):
    """Content character for a key, or None if not text."""
    if not key.startswith("KEY_"):
        return None
    k = key[4:]
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return k.lower()
    return _PUNCT.get(k)


# ---- physical geometry: position -> (hand, finger); index spans 2 columns ---
def totem_geo():
    F, ROW, HOME = {}, {}, set()
    lr = [("L", "pinky"), ("L", "ring"), ("L", "middle"), ("L", "index"),
          ("L", "index"), ("R", "index"), ("R", "index"), ("R", "middle"),
          ("R", "ring"), ("R", "pinky")]
    for p, f in zip(range(0, 10), lr):
        F[p], ROW[p] = f, "top"
    for p, f in zip(range(10, 20), lr):
        F[p], ROW[p] = f, "home"
    bot = [("L", "pinky"), ("L", "pinky"), ("L", "ring"), ("L", "middle"),
           ("L", "index"), ("L", "index"), ("R", "index"), ("R", "index"),
           ("R", "middle"), ("R", "ring"), ("R", "pinky"), ("R", "pinky")]
    for p, f in zip(range(20, 32), bot):
        F[p], ROW[p] = f, "bottom"
    for p in (32, 33, 34):
        F[p], ROW[p] = ("L", "thumb"), "thumb"
    for p in (35, 36, 37):
        F[p], ROW[p] = ("R", "thumb"), "thumb"
    HOME.update([10, 11, 12, 13, 16, 17, 18, 19])
    return F, ROW, HOME


ROW_PEN = {"home": 0.0, "top": 1.0, "bottom": 1.6, "thumb": 0.4}
FIN_PEN = {"pinky": 1.7, "ring": 1.35, "middle": 1.1, "index": 1.0, "thumb": 0.6}


def pos_cost(pos, F, ROW):
    return round(ROW_PEN[ROW[pos]] * FIN_PEN[F[pos][1]], 2)


# ---- parse the base layer: symbol -> position ------------------------------
_ZKEY = {"SEMI": ";", "SEMICOLON": ";", "COMMA": ",", "DOT": ".", "APOS": "'",
         "APOSTROPHE": "'", "SLASH": "/", "FSLH": "/", "MINUS": "-",
         "SQT": "'", "GRAVE": "`", "SPACE": " "}


def zsym(tok):
    t = tok.upper()
    if len(t) == 1 and (t.isalpha() or t.isdigit()):
        return t.lower()
    return _ZKEY.get(t)


def parse_base_positions(keymap_text):
    """Return {char: position} for the default layer (tap value of each key)."""
    m = re.search(r"default_layer\s*\{.*?bindings\s*=\s*<(.*?)>\s*;",
                  keymap_text, re.S)
    if not m:
        return {}, 0
    body = re.sub(r"//.*", "", m.group(1))
    sym2pos, pos = {}, 0
    for chunk in body.split("&"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        beh = parts[0]
        key = None
        if beh == "kp" and len(parts) >= 2:
            key = parts[-1]
        elif beh in ("hml", "hmr", "hmbackspace", "lt") and len(parts) >= 2:
            key = parts[-1]                       # tap side = last param
        c = zsym(key) if key else None
        if c and c not in sym2pos:
            sym2pos[c] = pos
        pos += 1
    return sym2pos, pos


# ---- token stream: gestures, not raw keys ----------------------------------
ESC_CHAR = "\x1b"


class Tok:
    __slots__ = ("t", "kind", "char", "mods")

    def __init__(self, t, kind, char=None, mods=frozenset()):
        self.t, self.kind, self.char, self.mods = t, kind, char, mods


def build_stream(evs):
    """Modifier-aware token stream. kinds: content/delete1/delword/nav/
    boundary/shortcut/other."""
    held, out = set(), []
    for o in evs:
        k, v = o["key"], o.get("val")
        if k in MODS:
            if v == 1:
                held.add(k)
            elif v == 0:
                held.discard(k)
            continue
        if v != 1:                                # only fresh presses = tokens
            continue
        t = int(o["t"] * 1000)
        cmd = held & COMMANDISH
        if k == BKSP:
            out.append(Tok(t, "delword" if (held & CTRL) else "delete1"))
        elif k in NAV:
            out.append(Tok(t, "nav"))
        elif k in ("KEY_SPACE", "KEY_ENTER", "KEY_TAB", "KEY_ESC"):
            # keep ESC distinguishable: combo diagnostics need to tell an Esc
            # from an Enter/Tab. (Only `content` chars feed erased/retyped, so
            # tagging boundaries here is free.)
            out.append(Tok(t, "boundary", {"KEY_SPACE": " ", "KEY_ESC": ESC_CHAR}.get(k, "")))
        elif cmd:
            out.append(Tok(t, "shortcut", k, frozenset(cmd)))
        else:
            c = to_char(k)
            out.append(Tok(t, "content", c) if c is not None else Tok(t, "other"))
    return out


def correction_episodes(stream):
    """Each deletion burst -> (erased text, retyped text)."""
    eps, i, n = [], 0, len(stream)
    while i < n:
        if stream[i].kind in ("delete1", "delword"):
            j, dels, delword = i, 0, False
            while j < n and stream[j].kind in ("delete1", "delword") \
                    and (j == i or stream[j].t - stream[j - 1].t < 900):
                delword = delword or stream[j].kind == "delword"
                dels += 1
                j += 1
            pre = []                              # contiguous content before burst
            k = i - 1
            while k >= 0 and stream[k].kind == "content" and len(pre) < 16:
                pre.append(stream[k].char)
                k -= 1
            pre.reverse()
            post, m = [], j                       # contiguous content after burst
            while m < n and stream[m].kind == "content" and len(post) < 16:
                post.append(stream[m].char)
                m += 1
            s = "".join(pre)
            if delword:
                erased = s[s.rfind(" ") + 1:]
            else:
                erased = "".join(pre[-dels:]) if dels <= len(pre) else s
            eps.append((stream[i].t, erased, "".join(post), delword))
            i = max(m, j)
        else:
            i += 1
    return eps


def classify(erased, retyped):
    """What kind of mistake turned `erased` into `retyped`?"""
    e, r = erased.strip(), retyped.strip()
    if not e:
        return "unknown"
    # retyped often continues past the fix; compare on the erased length
    r_head = r[:max(len(e), 1)]
    if e == r_head:
        return "restart"                          # deleted then typed the same
    if {"oe", "eo"} & {e[-2:], r[:2]} or e in ("oe", "eo"):
        return "combo-misfire(esc)"
    if e in (",.", ".,"):
        return "combo-misfire(tab)"
    if len(e) == len(r_head) and sum(a != b for a, b in zip(e, r_head)) == 1:
        return "substitution"
    if sorted(e) == sorted(r_head) and e != r_head:
        return "transposition"
    if abs(len(e) - len(r_head)) == 1 and (e in r_head or r_head in e):
        return "doubled/dropped"
    return "rewrite"


def combo_timing(stream, combo_chars):
    """For a 2-key combo, split every occurrence of its two letters landing
    back-to-back as LETTERS into:

      misfires — you meant the combo (you deleted them and hit the combo's key)
      legit    — you actually wanted those letters (e.g. the 'oe' in 'does')

    and record, for each, the roll gap between the two keys and the idle gap
    before them. Those two numbers are exactly what ZMK's `timeout-ms` and
    `require-prior-idle-ms` gate on, so they tell us WHICH knob blocked it --
    rather than assuming it was the timeout.
    """
    cs = set(combo_chars)
    mis, legit, n = [], [], len(stream)
    for i in range(1, n - 1):
        a, b = stream[i], stream[i + 1]
        if a.kind != "content" or b.kind != "content":
            continue
        if {a.char, b.char} != cs:
            continue
        roll = b.t - a.t
        prior = a.t - stream[i - 1].t
        intended, seen_del = False, False
        for j in range(i + 2, min(i + 8, n)):
            tk = stream[j]
            if tk.kind in ("delete1", "delword"):
                seen_del = True
            elif tk.kind == "boundary" and tk.char == ESC_CHAR:
                intended = seen_del          # deleted the letters, then hit Esc
                break
            elif tk.kind != "content":
                break
        (mis if intended else legit).append((roll, prior))
    return mis, legit


def pctl(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


# ---- current keymap values (for current->proposed + apply) -----------------
def keymap_values(txt):
    v = {}
    m = re.search(r"esc\s*\{[^}]*?timeout-ms\s*=\s*<(\d+)>", txt, re.S)
    if m:
        v["esc combo timeout-ms"] = int(m.group(1))
    m = re.search(r"esc\s*\{[^}]*?require-prior-idle-ms\s*=\s*<(\d+)>", txt, re.S)
    if m:
        v["esc combo require-prior-idle-ms"] = int(m.group(1))
    for beh in ("hml", "hmr"):
        b = re.search(beh + r":\s*" + beh + r"\s*\{(.*?)\};", txt, re.S)
        if b:
            for key in ("tapping-term-ms", "require-prior-idle-ms"):
                mm = re.search(key + r"\s*=\s*<(\d+)>", b.group(1))
                if mm:
                    v[f"{beh} {key}"] = int(mm.group(1))
    return v


def apply_timing(txt, changes):
    """changes: {'esc combo timeout-ms': val, 'hml require-prior-idle-ms': val, ...}"""
    applied = []
    for name, val in changes.items():
        if name == "esc combo timeout-ms":
            txt, n = re.subn(r"(esc\s*\{[^}]*?[^-]timeout-ms\s*=\s*<)\d+(>)",
                             rf"\g<1>{val}\g<2>", txt, flags=re.S)
        elif name == "esc combo require-prior-idle-ms":
            txt, n = re.subn(r"(esc\s*\{[^}]*?require-prior-idle-ms\s*=\s*<)\d+(>)",
                             rf"\g<1>{val}\g<2>", txt, flags=re.S)
        elif name.endswith("require-prior-idle-ms") or name.endswith("tapping-term-ms"):
            beh, key = name.split(" ", 1)
            b = re.search(beh + r":\s*" + beh + r"\s*\{.*?\};", txt, re.S)
            if not b:
                n = 0
            else:
                new = re.sub(key + r"\s*=\s*<\d+>", f"{key} = <{val}>", b.group(0))
                txt = txt[:b.start()] + new + txt[b.end():]
                n = new != b.group(0)
        else:
            n = 0
        if n:
            applied.append((name, val))
    return txt, applied


# ---- report ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--device", default="totem")
    ap.add_argument("--keymap", default=None)
    ap.add_argument("--apply-timing", action="store_true",
                    help="write safe timing changes into the keymap (+ .bak)")
    args = ap.parse_args()

    paths = []
    for f in args.files:
        paths.extend(sorted(glob.glob(f)) or [f])
    evs = []
    for p in paths:
        try:
            for line in open(p):
                line = line.strip()
                if line:
                    o = json.loads(line)
                    if o.get("dev") == args.device:
                        evs.append(o)
        except (OSError, json.JSONDecodeError):
            continue
    evs.sort(key=lambda o: o["t"])

    km_txt = open(args.keymap).read() if args.keymap else ""
    cur = keymap_values(km_txt)
    sym2pos, ncells = parse_base_positions(km_txt)
    F, ROW, HOME = totem_geo()

    stream = build_stream(evs)
    content = [tk for tk in stream if tk.kind == "content"]
    if len(content) < 200:
        print(f"# Keylog analysis — `{args.device}`\n\nOnly **{len(content)}** "
              f"content keypresses so far. Let it run a few days (aim 5k+), "
              f"then re-run — the error stats need volume to be trustworthy.")
        return

    out, w = [], lambda s="": out.append(s)
    eps = correction_episodes(stream)
    real_eps = [e for e in eps if e[1] and classify(e[1], e[2]) != "restart"]
    cls = Counter(classify(e[1], e[2]) for e in eps if e[1])
    nav = sum(1 for tk in stream if tk.kind == "nav")
    delword = sum(1 for e in eps if e[3])
    corr_rate = 100 * len(real_eps) / max(1, len(content))

    w(f"# Keylog analysis — `{args.device}`\n")
    w(f"- **{len(content)}** content keypresses, **{len(eps)}** correction "
      f"bursts (**{len(real_eps)}** real typos ⇒ **{corr_rate:.1f}%** typo rate)")
    w(f"- delete-word (Ctrl+⌫) used **{delword}×** — a deliberate tool, not counted as a misfire")
    ivs = [content[i].t - content[i-1].t for i in range(1, len(content))
           if 0 < content[i].t - content[i-1].t < 1000]
    if ivs:
        w(f"- typing flow: p50 **{pctl(ivs,50)}ms**, p90 **{pctl(ivs,90)}ms** between letters")
    w("")

    # ---- what you actually mistype ----
    w("## What you actually mistype\n")
    w("Error types: " + ", ".join(f"**{k}** {v}" for k, v in cls.most_common()
                                  if k not in ("restart", "unknown")) + "\n")
    top_err = Counter(e[1] for e in real_eps if 1 <= len(e[1]) <= 8)
    if top_err:
        w("Most-erased strings (the real typos, modifiers excluded):\n")
        w(" ".join(f"`{s}`×{n}" for s, n in top_err.most_common(15)))
    w("")

    recs = {}   # name -> (proposed, why)

    # ---- esc combo: diagnose WHICH knob blocked each misfire ----------------
    # A combo can fail two independent ways: the roll was slower than
    # `timeout-ms`, OR `require-prior-idle-ms` disarmed it because you were
    # mid-flow. Blaming the timeout by default is how you end up "fixing" the
    # wrong parameter -- so attribute every misfire, then sweep the trade-off.
    cur_to = cur.get("esc combo timeout-ms", 50)
    cur_rpi = cur.get("esc combo require-prior-idle-ms", 150)
    mis, legit = combo_timing(stream, ("o", "e"))
    if mis:
        w("## Esc combo (o+e) — why it actually misfires\n")
        by_to = sum(1 for roll, _ in mis if roll > cur_to)
        by_rpi = sum(1 for roll, pr in mis if pr < cur_rpi and roll <= cur_to)
        w(f"**{len(mis)}** misfires. Blocked by `require-prior-idle-ms`"
          f"({cur_rpi}): **{by_rpi}** — by `timeout-ms`({cur_to}): **{by_to}**.\n")
        w("| roll o→e | prior idle | blocked by |")
        w("|---|---|---|")
        for roll, pr in sorted(mis, key=lambda x: x[1]):
            why = []
            if roll > cur_to:
                why.append(f"timeout (>{cur_to})")
            if pr < cur_rpi:
                why.append(f"require-prior-idle (<{cur_rpi})")
            w(f"| {roll}ms | {pr}ms | {' + '.join(why) or '— (should have fired)'} |")
        w("")
        w("Trade-off — *catches* = misfires that would now correctly fire Esc; "
          "*false* = real `oe`/`eo` letter pairs (as in \"does\") that would "
          "wrongly turn into Esc:\n")
        w("| require-prior-idle | timeout | catches | false Esc |")
        w("|---|---|---|---|")
        best = None
        for rpi in (75, 100, 125, 150):
            for to in (50, 75, 100):
                c = sum(1 for roll, pr in mis if roll <= to and pr >= rpi)
                f = sum(1 for roll, pr in legit if roll <= to and pr >= rpi)
                w(f"| {rpi} | {to} | {c}/{len(mis)} | {f} |")
                score = c - 0.5 * f          # a false Esc is half as bad as a miss
                if best is None or score > best[0]:
                    best = (score, rpi, to, c, f)
        w("")
        _, brpi, bto, bc, bf = best
        if brpi != cur_rpi:
            recs["esc combo require-prior-idle-ms"] = (brpi,
                f"{by_rpi}/{len(mis)} misfires were disarmed by the idle gate, "
                f"not the timeout; {brpi}ms catches {bc}/{len(mis)} ({bf} false)")
        if bto != cur_to:
            recs["esc combo timeout-ms"] = (bto,
                f"headroom for slow rolls; catches {bc}/{len(mis)} ({bf} false)")

    # substitution errors between adjacent keys -> possible mispress hotspots
    subs = Counter()
    for t, e, r, dw in eps:
        if e and classify(e, r) == "substitution":
            rr = r.strip()[:len(e.strip())]
            for a, b in zip(e.strip(), rr):
                if a != b:
                    subs[frozenset((a, b))] += 1
    if subs:
        w("## Substitution hotspots (wrong-key presses)\n")
        w(" ".join(f"`{'/'.join(sorted(p))}`×{n}" for p, n in subs.most_common(10)))
        w("")

    # ---- ergonomics from geometry ----
    sfb = Counter()
    if sym2pos and ncells >= 30:
        w("## Ergonomics (layout vs your real frequency)\n")
        uni = Counter(tk.char for tk in content if tk.char in sym2pos)
        tot = sum(uni.values())
        load = Counter()
        hand = Counter()
        homep = 0
        for ch, c in uni.items():
            p = sym2pos[ch]
            load[F[p]] += c
            hand[F[p][0]] += c
            if p in HOME:
                homep += c
        w(f"- hand balance: **L {100*hand['L']/tot:.0f}% / R {100*hand['R']/tot:.0f}%**, "
          f"home-row **{100*homep/tot:.0f}%** of presses")
        w("- finger load: " + ", ".join(
            f"{h[0]}{f[:3]} {100*c/tot:.0f}%" for (h, f), c in load.most_common()
            if f != "thumb"))
        # same-finger bigrams
        sfb = Counter()
        seq = [tk for tk in content]
        for i in range(1, len(seq)):
            a, b = seq[i-1].char, seq[i].char
            if a in sym2pos and b in sym2pos and a != b \
                    and seq[i].t - seq[i-1].t < 400:
                if F[sym2pos[a]] == F[sym2pos[b]]:
                    sfb[a + b] += 1
        nb = sum(1 for i in range(1, len(seq))
                 if seq[i].char in sym2pos and seq[i-1].char in sym2pos)
        sfb_tot = sum(sfb.values())
        w(f"- **same-finger bigrams: {100*sfb_tot/max(1,nb):.1f}%** "
          f"(<1% is excellent, >5% hurts). worst: "
          + " ".join(f"`{k}`×{v}" for k, v in sfb.most_common(8)))
        # high-effort keys: frequent letters on costly positions
        eff = [(ch, c, pos_cost(sym2pos[ch], F, ROW)) for ch, c in uni.items()]
        worst = sorted(((c/tot)*cost, ch, cost) for ch, c, cost in eff if cost >= 1.5)
        worst = list(reversed(worst))[:6]
        if worst:
            w("- highest-effort keys (frequent × awkward position): "
              + " ".join(f"`{ch}`(cost {cost})" for _, ch, cost in worst))
        w("")

    # ---- recommendations ----
    w("## → Recommended timing changes (safe to auto-apply)\n")
    tbl = [(n, cur.get(n), v, why) for n, (v, why) in recs.items()
           if cur.get(n) is None or v != cur.get(n)]
    if tbl:
        w("| parameter | current | proposed | why |")
        w("|---|---|---|---|")
        for n, c, v, why in tbl:
            w(f"| `{n}` | {c if c is not None else '?'} | **{v}** | {why} |")
    else:
        w("_No confident timing change — your combo/hold timing looks fine._")
    w("")

    w("## → Layout ideas (manual — moving a base key touches several layers)\n")
    ideas = []
    if sym2pos and sfb:
        top = sfb.most_common(1)[0]
        ideas.append(f"Your worst same-finger bigram is `{top[0]}` ({top[1]}×). "
                     f"Moving one of those letters off that finger's column removes it.")
    if not ideas:
        ideas.append("_Nothing egregious in the letter placement yet._")
    for s in ideas:
        w("- " + s)
    w("")

    print("\n".join(out))

    if args.apply_timing:
        changes = {n: v for n, (v, _) in recs.items()
                   if cur.get(n) is None or v != cur.get(n)}
        if not changes:
            print("\n[apply] nothing to apply.")
            return
        new, applied = apply_timing(km_txt, changes)
        if applied:
            open(args.keymap + ".bak", "w").write(km_txt)
            open(args.keymap, "w").write(new)
            print(f"\n[apply] wrote {args.keymap} (backup: {args.keymap}.bak)")
            print("[apply] " + ", ".join(f"{n}→{v}" for n, v in applied))
            print("[apply] review with `git diff`, then build+flash when ready.")
        else:
            print("\n[apply] no parameters matched in the keymap.")


if __name__ == "__main__":
    main()
