#!/bin/sh
# Double-clickable macOS menu for the PAMIR serial controller.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TIS="$ROOT/.venv-macos/bin/tis-test"

if [ ! -x "$TIS" ]; then
    printf 'TIS Tester is not installed yet.\n'
    printf 'First double-click laptop-tools/install-macos.command.\n\n'
    printf 'Press Return to close...'
    read -r _
    exit 1
fi

printf '\nTIS Tester - macOS serial launcher\n'
printf 'Only one application may use the serial port at a time.\n\n'
printf '  1) List serial ports\n'
printf '  2) Open TIS serial dashboard\n'
printf '  3) Read device status\n'
printf '  4) Restore normal mode\n'
printf '  5) Restore normal mode and reboot\n'
printf '  6) Upload a tis-tester.tar.gz archive\n'
printf '  7) Open local mock web dashboard\n'
printf '  q) Quit\n\n'
printf 'Selection: '
read -r choice

case "$choice" in
    1)
        "$TIS" serial ports
        ;;
    2|3|4|5|6)
        "$TIS" serial ports || true
        printf '\nSerial port (Return = auto-detect): '
        read -r selected_port
        run_serial() {
            action=$1
            shift
            if [ -n "$selected_port" ]; then
                "$TIS" serial "$action" --port "$selected_port" \
                    --baud 1500000 "$@"
            else
                "$TIS" serial "$action" --baud 1500000 "$@"
            fi
        }
        case "$choice" in
            2)
                run_serial dashboard
                ;;
            3)
                run_serial status
                ;;
            4)
                run_serial restore
                ;;
            5)
                run_serial restore --reboot
                ;;
            6)
                printf 'Archive path: '
                read -r archive
                run_serial send --file "$archive"
                ;;
        esac
        ;;
    7)
        printf 'Opening http://127.0.0.1:8080 (stop with Control-C)...\n'
        (sleep 1; open http://127.0.0.1:8080) &
        "$TIS" web --host 127.0.0.1 --port 8080 --mock
        ;;
    q|Q)
        exit 0
        ;;
    *)
        printf 'Unknown selection.\n'
        ;;
esac

printf '\nPress Return to close...'
read -r _
