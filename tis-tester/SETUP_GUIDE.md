# Device and web-dashboard setup

See [README.md](README.md) for the verified hardware limitations,
[SERIAL_SETUP.md](SERIAL_SETUP.md) for serial operation, and
[MACOS_SETUP.md](MACOS_SETUP.md) for the Mac laptop installer and launcher.

## Install on PAMIR

```bash
cd ~/tis-tester
nix develop
tis-test diagnose
tis-test rx --mock --duration 3
```

Plain Debian fallback:

```bash
python3 -m venv ~/tis-venv
~/tis-venv/bin/pip install ~/tis-tester
~/tis-venv/bin/tis-test diagnose
```

All configuration and results remain in user-writable XDG directories; the
immutable root is not modified.

## Browser dashboard

```bash
tis-test web --host 0.0.0.0 --port 8080
```

Open `http://<PAMIR-IP>:8080` on the laptop. The page is self-contained and
requires no internet connection. TX requires explicit RF-load confirmation
and auto-stops at the configured safety limit.

## Hardware preflight

```bash
tis-test diagnose
hciconfig hci0
cat /sys/devices/platform/aic-bsp/aicbsp_info/cpmode
```

BLE RX packet counting on the current image uses AIC's raw UART `bt_test`
path on `/dev/ttyS4`, not the BlueZ `hci0` DTM path. Wi‑Fi testing fails
closed unless the AIC RF-test firmware is already active. Do not use runtime
SDIO unbind/bind on this kernel.

## Restore normal operation

```bash
tis-test restore
tis-test restore --reboot
```

Use `--reboot` after an RF-firmware session to guarantee the normal firmware
is reloaded. After reboot, verify `wlan0` is managed/connected and `hci0` is
UP/RUNNING.
