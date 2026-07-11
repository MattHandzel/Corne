#!/usr/bin/env bash
# Convenience runner. collector.py needs python-evdev; analyze.py + make_sample.py
# are stdlib-only.
#   ./run.sh analyze                 # analyze today's totem log
#   ./run.sh analyze --device corne  # analyze the corne
#   ./run.sh report                  # same as analyze, all logs
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
pyenv="$HOME/.local/state/keylog/pyenv/bin/python3"
logs="$HOME/notes/life-logging/key-logging/keylog-*.jsonl"
km="$here/../../config/totem.keymap"

cmd=${1:-report}; shift || true
case "$cmd" in
  collector)
    exec "${pyenv:-python3}" "$here/collector.py" "$@" ;;
  analyze|report)
    # shellcheck disable=SC2086
    exec python3 "$here/analyze.py" $logs --keymap "$km" "$@" ;;
  sample)
    exec python3 "$here/make_sample.py" "$@" ;;
  *)
    echo "usage: run.sh {collector|analyze|report|sample} [args]" >&2; exit 2 ;;
esac
