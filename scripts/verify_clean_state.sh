#!/bin/sh
# The Python coordinator owns signal cleanup, deadlines, and the exact resource allowlist.
set -eu
SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_ROOT/del_05_clean_state.py" "$@"
