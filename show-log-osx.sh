#!/usr/bin/env bash

set -euo pipefail

LOG_FILE="$HOME/Library/Application Support/Tnasrevner/tnasrevner.log"
LINE_COUNT="${1:-200}"

if [[ ! -f "$LOG_FILE" ]]; then
    echo "Log not found: $LOG_FILE" >&2
    echo "Launch Tnasrevner once with ./launch-osx.sh first." >&2
    exit 1
fi

echo "Tnasrevner log: $LOG_FILE"
tail -n "$LINE_COUNT" "$LOG_FILE"
