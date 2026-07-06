# TIS Tester — FCS960K-N / AIC8800D80

Automated Wi‑Fi and Bluetooth receiver testing for the FCS960K-N module on
the PAMIR Debian/Nix image. It provides a terminal UI, browser dashboard,
automation CLI, CSV logging, and Windows/macOS serial-console launchers.

## Implemented controls

- Wi‑Fi or Bluetooth selection
- 2.4/5 GHz band, channel, and 20/40 MHz bandwidth
- Legacy, HT, VHT, HE MCS, and BLE PHY selection
- 0–23 dBm requested TX power
- Continuous RX/TX start and stop
- RSSI, PER, CRC good/error, and packet-count display
- Automated channel/rate RX sweeps and timestamped CSV logs
- Per-run HTML reports from the browser dashboard, CLI, and serial dashboard
- TX antenna interlock and a 300-second default TX safety timeout
- Explicit stop/restore-normal command

Fields unsupported by a radio interface are shown as `n/a`; the software
does not manufacture measurements. In particular, the tested AIC Wi‑Fi RF
ioctl reports FCS-good and total packets but not RSSI, and standard BLE DTM
reports received packet count but not live RSSI or CRC-error count.

## Simulator and tests

```bash
nix develop
tis-test interactive --mock
tis-test web --host 127.0.0.1 --port 8080 --mock
pytest -q
```

The TUI uses `t` for TX, `r` for RX, and `z` to zero counters. TX remains
disabled until the operator confirms that an antenna or chamber RF cable is
connected. The web UI applies the same interlock.

## Laptop serial use

Laptop control supports Windows, macOS, and Linux. The tested Windows port is
**COM5**; macOS normally exposes the same USB adapter as
`/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`. The console is **1,500,000 baud,
8-N-1, no flow control** on every host. Find the local name without opening
the port:

```text
tis-test serial ports
```

Install laptop-side support with `python -m pip install -e ".[serial]"`.
For a double-click Mac installer and launcher, see
[MACOS_SETUP.md](MACOS_SETUP.md).

### Windows / COM5

The tested console is **COM5 at 1,500,000 baud, 8-N-1, no flow control**.
PuTTY's old saved 115200 setting is incorrect for this board. Only one
program can own COM5, so close PuTTY before a serial command.

```powershell
py -m pip install -e ".[serial]"
tis-test serial status --port COM5 --baud 1500000
tis-test serial dashboard --port COM5 --baud 1500000
```

`serial dashboard` starts the device TUI and then opens PuTTY at the correct
speed. `serial launch` remains as an alias. To stop a test and select normal
mode from the laptop:

```powershell
tis-test serial restore --port COM5 --baud 1500000
# If RF-test firmware was loaded, guarantee normal firmware is reloaded:
tis-test serial restore --port COM5 --baud 1500000 --reboot
```

Use `laptop-tools/send-to-pamir.ps1 -Port COM5 -Baud 1500000` for the first
offline transfer. The script unpacks the archive into `~/tis-tester`.

### macOS

```bash
cd /path/to/TIS/tis-tester
./laptop-tools/install-macos.command
./.venv-macos/bin/tis-test serial ports
./.venv-macos/bin/tis-test serial dashboard \
  --port /dev/cu.usbserial-0001 --baud 1500000
```

The Mac dashboard hands the port to pyserial miniterm. Exit miniterm with
Control-]. Auto-detection may omit `--port` when exactly one USB serial
adapter is connected. The cross-platform `serial send --file FILE.tar.gz`
command provides a SHA-256-verified first upload.

## Device-side commands

```bash
nix develop
tis-test diagnose
tis-test interactive
tis-test web --port 8080

tis-test rx --radio wifi --band 5GHz --channel 36 --bw 20 \
  --rate HE-MCS0 --duration 30 --expected 1000

tis-test rx --radio bt --channel 19 --rate 1M --expected 1500

tis-test tx --radio wifi --band 2.4GHz --channel 6 --rate 11b-11M \
  --power 17 --duration 10 --confirm-antenna

tis-test sweep --radio wifi --band 2.4GHz --channels 1,6,11 \
  --rates HE-MCS0,HT-MCS0,11b-1M --dwell 10

tis-test restore
tis-test restore --reboot
```

For a standalone immutable package instead of the development shell, run
`nix build` once and use `./result/bin/tis-test`.

Results default to `~/.local/share/tis-tester/`; configuration defaults to
`~/.config/tis-tester/tis_config.yaml`. The immutable root is not modified.

## Hardware backend facts established on PAMIR

The live board identifies as AIC8800D80 U02 and exposes:

- Wi‑Fi: `wlan0`, built-in `aic8800_fdrv`, SDIO function on `mmc2`
- Bluetooth: `hci0`, UART attached at 1,500,000 baud
- Bluetooth DTM vendor tool: `/root/aicrf-test-extract/usr/bin/bt_test`
- Normal firmware: `fmacfw_8800d80_u02.bin`
- RF-test firmware: `lmacfw_rf_8800d80_u02.bin`
- Mode selector: `/sys/devices/platform/aic-bsp/aicbsp_info/cpmode`
- Modes: `0 = normal`, `1 = RF test`

The Wi‑Fi backend implements AICSemi's native `SIOCDEVPRIVATE+1` ABI with a
strict allow-list: `set_tx`, `set_txstop`, `set_rx`, `set_rxstop`, and
`get_rx_result`. Calibration, EFUSE, MAC-writing, tone, and arbitrary private
commands cannot be invoked through this application.

### Important PAMIR image limitation

On the tested kernel, `aic8800_bsp` and `aic8800_fdrv` are built in. Changing
`cpmode` selects a firmware for the next genuine module power-up; it does not
reload the running firmware. Soft rfkill does not power-cycle it, and runtime
SDIO host unbind/bind is unsafe on this image. Therefore the application
fails closed unless RF firmware is already active. Do not add an unbind/bind
sequence to the config.

For production Wi‑Fi TIS, use a Quectel/AICSemi RF-test-enabled PAMIR image
or a kernel where the vendor driver is loadable with test mode enabled. BLE
DTM RX packet counting on this image must bypass BlueZ and use AIC's raw UART
`bt_test` path on `/dev/ttyS4`.

`tis-test restore` always attempts test-end/stop, selects mode 0, unblocks
rfkill, restarts NetworkManager/wpa_supplicant/Bluetooth, and verifies the
selectors. `--reboot` provides the guaranteed normal-firmware reload.

## Parameter coverage

| Item | Values |
|---|---|
| Bands | 2.4 GHz and 5 GHz |
| Wi‑Fi channels | 1–14; 36–64, 100–144, 149–165 |
| Bandwidth | 20 / 40 MHz |
| Legacy | 11b 1/2/5.5/11; OFDM 6–54 Mbps |
| MCS | HT 0–7, VHT 0–9, HE 0–11 |
| TX power control | 0–23 dBm requested |
| BLE PHY | AIC UART: validated 1M; HCI backend: 1M, 2M, Coded S=8/S=2 |
| BLE RF channels | 0–39 |

## Repository layout

```text
tis_tester/
  cli.py             CLI and serial entry points
  tui.py             curses bench UI
  webui.py           offline browser dashboard
  serial_console.py  Windows/macOS/Linux laptop launcher, upload, recovery
  aic_ioctl.py       allow-listed AIC8800 private ioctl
  recovery.py        stop and normal-mode restoration
  session.py         dwell, sweep, PER math, CSV logging
  rates.py           channels, bands, rates, PHYs
  backends/          Wi‑Fi, BLE HCI, and simulator backends
tests/                hardware-independent automated tests
```
