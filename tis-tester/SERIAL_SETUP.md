# PAMIR serial setup (Windows and macOS)

The tested console is **1,500,000 baud, 8 data bits, no parity, one stop bit,
no flow control**. It appears as `COM5` on the tested Windows laptop and as a
`/dev/cu.*` port on macOS. Only one serial program can hold the port at once.
See [MACOS_SETUP.md](MACOS_SETUP.md) for the complete Mac walkthrough.

## 1. Verify the console

Open PuTTY with connection type `Serial`, line `COM5`, speed `1500000`.
Log in and confirm the prompt is `root@lapis`. Close PuTTY without typing
`exit` before running a PowerShell or `tis-test serial` command.

## 2. Transfer offline

From the folder containing `tis-tester.tar.gz`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\send-to-pamir.ps1 -Port COM5 -Baud 1500000
```

The script verifies SHA-256 before unpacking to `~/tis-tester`.

On macOS, create the archive and use the cross-platform uploader:

```bash
tar --exclude='.venv-macos' --exclude='*.tar.gz' \
    -czf ../tis-tester.tar.gz .
./.venv-macos/bin/tis-test serial send --port /dev/cu.usbserial-0001 \
    --baud 1500000 --file ../tis-tester.tar.gz
```

## 3. Simulator check on PAMIR

```bash
cd ~/tis-tester
nix develop
export TERM=xterm
tis-test rx --mock --duration 3
tis-test interactive --mock
pytest -q
```

## 4. Laptop serial launcher

Install the package on Windows with the serial extra:

```powershell
py -m pip install -e ".[serial]"
tis-test serial status --port COM5 --baud 1500000
tis-test serial dashboard --port COM5 --baud 1500000
```

`dashboard` hands COM5 to PuTTY after starting the serial-safe dashboard TUI.
`launch` remains as an alias. The dashboard includes an antenna/RF-load
interlock before TX, automatically stops TX at the configured safety limit,
and writes an HTML report path after each completed run.

On macOS, install and launch with:

```bash
./laptop-tools/install-macos.command
./.venv-macos/bin/tis-test serial ports
./.venv-macos/bin/tis-test serial dashboard \
    --port /dev/cu.usbserial-0001 --baud 1500000
```

The Mac launcher uses pyserial miniterm; press Control-] to leave it. If only
one USB serial adapter exists, `--port` can be omitted.

## 5. Normal-mode recovery

From PAMIR:

```bash
tis-test restore
tis-test restore --reboot   # guaranteed normal-firmware reload
```

Or, after closing PuTTY/miniterm, from the laptop:

```powershell
tis-test serial restore --port COM5 --baud 1500000 --reboot
```

Replace `COM5` with the `/dev/cu.*` port on macOS.

The restore flow attempts both Wi‑Fi and BLE test-stop commands, selects
`cpmode=0`, unblocks the radios, restarts networking/Bluetooth services, and
verifies the normal-mode selector. Do not use SDIO unbind/bind on this PAMIR
kernel; the built-in driver can block in that path.

## 6. Hardware support on this image

- BLE RX packet counting on this image uses AIC's raw UART `bt_test` path on
  `/dev/ttyS4`, not the BlueZ `hci0` DTM path.
- The native AIC Wi‑Fi RF-test ioctl is implemented in the application.
- Wi‑Fi RF-test firmware must already be active. This image's AIC drivers are
  built in and cannot safely switch firmware at runtime. Obtain a vendor
  RF-test-enabled boot image (or loadable driver build) for Wi‑Fi TIS.
- Wi‑Fi RF mode reports FCS-good and total packet count, not RSSI.
- Standard BLE DTM reports received packet count, not live RSSI/CRC errors.

Unsupported measurements remain `n/a`; they are never synthesized.
