#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

find_python() {
    if [[ -n "${PYTHON_BIN:-}" ]] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        if "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
            printf '%s\n' "$PYTHON_BIN"
            return
        fi
        echo "PYTHON_BIN must point to Python 3.11+." >&2
        exit 1
    fi

    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
                printf '%s\n' "$candidate"
                return
            fi
        fi
    done

    echo "Python 3.11+ required." >&2
    echo "Install it using your Linux distribution package manager." >&2
    exit 1
}

PYTHON_BIN="$(find_python)"
echo "Using: $($PYTHON_BIN --version)"

if [[ -x .venv/bin/python ]] && ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "Existing .venv uses unsupported Python; recreating it."
    rm -rf .venv
fi

if [[ ! -d .venv ]]; then
    "$PYTHON_BIN" -m venv .venv
else
    echo "Using existing .venv"
fi

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirement-dev.txt
"$VENV_PYTHON" -c 'from PySide6.QtWidgets import QApplication; from tnasrevner.gui import MainWindow; print("GUI import: OK")'

echo
echo "Install complete. Run:"
echo "  source .venv/bin/activate"
echo "  python -m tnasrevner.gui"
