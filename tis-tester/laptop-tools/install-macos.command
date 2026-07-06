#!/bin/sh
# Double-clickable macOS installer for the laptop-side TIS serial tools.

set -eu

pause_on_error() {
    status=$?
    if [ "$status" -ne 0 ]; then
        printf '\nInstallation failed (status %s). Review the message above.\n' "$status"
        printf 'Press Return to close...'
        read -r _
    fi
}
trap pause_on_error EXIT

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VENV="$ROOT/.venv-macos"

printf '\nTIS Tester - macOS laptop installer\n'
printf 'Project: %s\n\n' "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
    printf 'Python 3 is required. Install Python 3.10 or newer from:\n'
    printf '  https://www.python.org/downloads/macos/\n'
    printf 'or run: brew install python\n'
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    printf 'Python 3.10 or newer is required; this Mac has: '
    python3 --version
    exit 1
fi

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e "${ROOT}[serial]"

printf '\nInstallation complete. '
"$VENV/bin/tis-test" --version
printf '\nDetected serial ports (listing only; no port is opened):\n'
"$VENV/bin/tis-test" serial ports || true
printf '\nDouble-click laptop-tools/tis-test-macos.command to open the launcher.\n'
printf 'Full instructions: MACOS_SETUP.md\n\n'
printf 'Press Return to close...'
read -r _
