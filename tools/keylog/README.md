# keylog — data-driven ZMK timing tuning

Collect your real keystrokes (labeled per keyboard), then let the analyzer tell
you which ZMK timing parameters to change and to what — from evidence, not guesses.

## Why this works

The host sees the keyboard's **resolved output**, so misfires are directly
observable as correction patterns that map onto ZMK knobs:

| what you did | what it means | fix |
|---|---|---|
| `o e ⌫⌫ ⎋` | rolled the esc combo too slowly; it came out as letters | ↑ esc combo `timeout-ms` |
| `⎋ ⌫ o e` | esc combo fired when you meant the letters | ↓ `timeout-ms` / add `require-prior-idle-ms` |
| `⌘ ⌫ …` | a home-row mod fired on a fast roll (accidental hold) | ↑ `tapping-term-ms` / `require-prior-idle-ms` |
| same key twice in <25ms | switch chatter / bounce | ↑ `CONFIG_ZMK_KSCAN_DEBOUNCE_*_MS` |

## Pieces

- **`collector.py`** — passive evdev reader. Opens every typing keyboard
  **without grabbing** (so it can never interfere with typing), labels each
  event (`totem` / `corne` / `laptop` / …), and appends JSONL to
  `~/notes/life-logging/key-logging/keylog-YYYYMMDD.jsonl` (mode `0600`).
- **`analyze.py`** — reads the logs for one device, runs the detectors above,
  and prints a markdown report with `current → proposed` values + evidence.
  Read-only; never edits your keymap.
- **`make_sample.py`** — synthetic log to validate the analyzer.
- **`run.sh`** — convenience wrapper.

## Running

The collector runs as a **systemd user service** (already installed):

```
systemctl --user status keylog-collector      # is it running?
journalctl --user -u keylog-collector -f       # what devices it sees
```

Analyze once you have a corpus (aim for 5k+ presses on a device — a few days):

```
cd tools/keylog
./run.sh analyze                    # today+all logs, device=totem
./run.sh analyze --device corne
./run.sh analyze --device laptop
```

Validate the analyzer end-to-end:

```
./run.sh sample > /tmp/s.jsonl && python3 analyze.py /tmp/s.jsonl --keymap ../../config/totem.keymap
```

## Labels

`totem` (USB + BT names both map here), `corne` (incl. its ZMK name
"Matt's Keyboard"), `laptop` (kanata's remapped output),
`laptop-internal` / `laptop-acer` (built-ins), `other:<slug>` for anything else.

## Privacy

Raw keystrokes are captured (needed to see *which* keys you corrected),
including anything you type into password fields. The log is owner-only
(`0600`) and never leaves the machine. To wipe: `rm ~/notes/life-logging/key-logging/keylog-*.jsonl`.

## Making it canonical in NixOS (optional)

The service currently lives at `~/.config/systemd/user/keylog-collector.service`
using a pinned env at `~/.local/state/keylog/pyenv`. To manage it in home-manager
instead, add roughly:

```nix
systemd.user.services.keylog-collector = {
  Unit.Description = "Keyboard-layout keylog collector";
  Service = {
    ExecStart = "${pkgs.python3.withPackages (p: [p.evdev])}/bin/python3 "
      + "${config.home.homeDirectory}/Projects/zmk-config-corne/tools/keylog/collector.py";
    Restart = "always";
  };
  Install.WantedBy = ["default.target"];
};
```
