# macOS laptop setup

The radio test backends still run on the PAMIR Debian/Nix device. The Mac is
the serial console, dashboard terminal, uploader, and recovery controller.
Both Intel and Apple Silicon Macs are supported through Python 3.10 or newer.

The board's tested console settings are **1,500,000 baud, 8-N-1, no flow
control**. On macOS the port normally looks like `/dev/cu.usbserial-*`,
`/dev/cu.usbmodem*`, `/dev/cu.SLAB_USBtoUART*`, or
`/dev/cu.wchusbserial*`. Use the `/dev/cu.*` endpoint, not the matching
`/dev/tty.*` endpoint.

## Install

1. Install [Python 3.10 or newer](https://www.python.org/downloads/macos/) if
   `python3 --version` reports an older version or is missing.
2. In Finder, open `tis-tester/laptop-tools` and double-click
   `install-macos.command`. If Gatekeeper asks, Control-click the file, choose
   **Open**, then confirm **Open**.
3. Double-click `tis-test-macos.command` for the menu.

The installer creates an isolated `.venv-macos` inside the project. It does
not change the system Python. Terminal users can perform the same install:

```bash
cd /path/to/TIS/tis-tester
python3 -m venv .venv-macos
./.venv-macos/bin/python -m pip install --upgrade pip
./.venv-macos/bin/python -m pip install -e '.[serial]'
```

## Find the serial port

Connect the USB serial cable, close any program already using it, and run:

```bash
./.venv-macos/bin/tis-test serial ports
```

Listing ports does not open or send anything to the device. If exactly one
USB serial adapter is connected, later commands can omit `--port`. If several
are listed, specify the PAMIR device explicitly:

```bash
PORT=/dev/cu.usbserial-0001   # replace with the value printed on your Mac
./.venv-macos/bin/tis-test serial status --port "$PORT" --baud 1500000
```

## Open the serial dashboard

```bash
./.venv-macos/bin/tis-test serial dashboard --port "$PORT" --baud 1500000
```

This starts the dashboard on PAMIR, releases the port, and opens pyserial
miniterm in the same Terminal window. Use the dashboard's displayed keys to
operate it. After leaving the dashboard, press **Control-]** to exit miniterm.
The terminal is intentionally used instead of macOS `screen`, because pyserial
handles the board's non-standard 1,500,000-baud rate directly.

Only one application can own the port. Close miniterm, `screen`, CoolTerm,
Serial, Arduino Serial Monitor, or other serial tools before running another
`tis-test serial` command.

## First offline upload

From the `tis-tester` source directory, make an archive that excludes the Mac
virtual environment, then upload it. The transfer is SHA-256 verified before
it is unpacked on PAMIR:

```bash
cd /path/to/TIS/tis-tester
tar --exclude='.venv-macos' --exclude='*.tar.gz' \
    -czf ../tis-tester.tar.gz .
./.venv-macos/bin/tis-test serial send --port "$PORT" --baud 1500000 \
    --file ../tis-tester.tar.gz
```

The PAMIR console must already be logged in to a shell for status, upload,
dashboard, and restore commands. If it is sitting at `login:`, first open a
plain terminal with:

```bash
./.venv-macos/bin/python -m serial.tools.miniterm \
    --raw --eol LF "$PORT" 1500000
```

Log in, then leave miniterm with **Control-]** without typing `exit`.

## Restore normal mode

Close the serial dashboard/miniterm, then run:

```bash
./.venv-macos/bin/tis-test serial restore --port "$PORT" --baud 1500000
```

After a Wi-Fi RF-firmware session, use the reboot form to guarantee normal
firmware is loaded:

```bash
./.venv-macos/bin/tis-test serial restore --port "$PORT" --baud 1500000 --reboot
```

## Local software-only check

This does not contact the board. It opens the dashboard against the simulator:

```bash
./.venv-macos/bin/tis-test web --host 127.0.0.1 --port 8080 --mock
```

Open <http://127.0.0.1:8080>. This verifies the Mac installation and UI, but
it is not an RF measurement or hardware qualification.
