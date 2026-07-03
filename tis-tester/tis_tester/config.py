"""Configuration handling.

Search order (first hit wins):
  1. --config PATH on the command line
  2. $TIS_TESTER_CONFIG
  3. $XDG_CONFIG_HOME/tis-tester/tis_config.yaml  (~/.config/tis-tester/...)
  4. ./tis_config.yaml (current directory)
  5. built-in defaults

Nothing is ever written to /etc or /usr -- safe on immutable-root systems.
Logs/results default to $XDG_DATA_HOME/tis-tester (~/.local/share/tis-tester).
"""

from __future__ import annotations
import copy
import os
from pathlib import Path

try:
    import yaml
    _HAVE_YAML = True
except ImportError:          # PyYAML optional; JSON-ish defaults still work
    _HAVE_YAML = False


DEFAULTS: dict = {
    "general": {
        "results_dir": None,          # None -> XDG data dir
        "poll_interval_s": 1.0,       # stats refresh period
        "max_tx_duration_s": 300,     # fail-safe cap for continuous TX
    },
    "wifi": {
        "backend": "ioctl",           # "ioctl", "shell", or "mock"
        "interface": "wlan0",
        "require_rf_mode": True,
        "mode_selector": "/sys/devices/platform/aic-bsp/aicbsp_info/cpmode",
        "module_testmode": "/sys/module/aic8800_bsp/parameters/testmode",
        "packet_interval_us": 1000,
        # Shell command templates for the vendor RF-test tool.
        # Placeholders: {iface} {freq} {channel} {band} {bw} {bw_code}
        #               {rate_code} {rate_name} {power}
        # Defaults below match the AICSemi/Quectel `aic_rftest` console tool;
        # adjust to whatever your driver drop ships (some expose the same
        # verbs through /sys/kernel/debug/rwnx/rftest instead).
        "commands": {
            "init":      [],  # e.g. ["modprobe aic8800_fdrv testmode=1"]
            "deinit":    [],
            "tx_start":  "aic_rftest -i {iface} tx_start {freq} {bw_code} {rate_code} {power}",
            "tx_stop":   "aic_rftest -i {iface} tx_stop",
            "rx_start":  "aic_rftest -i {iface} rx_start {freq} {bw_code}",
            "rx_stop":   "aic_rftest -i {iface} rx_stop",
            "rx_result": "aic_rftest -i {iface} rx_result",
            "rx_reset":  "aic_rftest -i {iface} rx_reset",
        },
        # Regexes used to scrape the rx_result output.
        "rx_result_regex": {
            "fcs_ok":  r"(?:fcs_ok|rx_ok|crc_ok)\s*[:=]\s*(\d+)",
            "fcs_err": r"(?:fcs_err|rx_err|crc_err)\s*[:=]\s*(\d+)",
            "rssi":    r"rssi\s*[:=]\s*(-?\d+)",
        },
        # bw MHz -> code expected by the vendor tool (0=20M, 1=40M is common)
        "bw_codes": {20: 0, 40: 1},
        "rate_code_overrides": {},    # e.g. {"HE-MCS11": 523}
        "restore_commands": [
            "rfkill unblock all",
            "systemctl restart aic8800-bt.service bluetooth.service "
            "wpa_supplicant.service NetworkManager.service",
            "rfkill unblock all",
            "hciconfig hci0 up",
        ],
    },
    "bt": {
        "backend": "aic_uart",        # "aic_uart", "hci", or "mock"
        "hci_dev": "hci0",
        "use_enhanced_test": True,    # HCI LE Rx/Tx Test v2 (PHY selectable)
        "default_payload": "prbs9",
        "default_payload_len": 37,
        "tool_path": "/root/aicrf-test-extract/usr/bin/bt_test",
        "uart_dev": "/dev/ttyS4",
        "uart_baud": 1500000,
        "startup_delay_s": 0.5,
        "service_stop": [
            "aic8800-bt.service",
            "bluetooth.service",
        ],
        "service_start": [
            "aic8800-bt.service",
            "bluetooth.service",
        ],
        "rfkill_block": "bluetooth",
        "rfkill_unblock": "bluetooth",
        "service_log": "/tmp/tis_bt_test_service.log",
        # Optional vendor HCI command template for TX power in test mode,
        # e.g. "hcitool -i {dev} cmd 0x3f 0x0011 {power:02x}". Empty = skip.
        "vendor_tx_power_cmd": "",
        # Optional vendor command that returns live RSSI during LE RX test
        # (standard HCI does not expose it). Empty = RSSI shown as n/a.
        "vendor_rssi_cmd": "",
        "vendor_rssi_regex": r"rssi\s*[:=]\s*(-?\d+)",
    },
    "serial": {
        "port": "COM5",
        "baud": 1500000,
    },
}


def _xdg(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env) or (Path.home() / fallback))


def default_results_dir() -> Path:
    d = _xdg("XDG_DATA_HOME", ".local/share") / "tis-tester"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config_file(explicit: str | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("TIS_TESTER_CONFIG"):
        candidates.append(Path(os.environ["TIS_TESTER_CONFIG"]))
    candidates.append(_xdg("XDG_CONFIG_HOME", ".config") / "tis-tester" / "tis_config.yaml")
    candidates.append(Path.cwd() / "tis_config.yaml")
    for c in candidates:
        if c.is_file():
            return c
    if explicit:
        raise FileNotFoundError(f"Config file not found: {explicit}")
    return None


def load_config(explicit: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = find_config_file(explicit)
    if path is not None:
        if not _HAVE_YAML:
            raise RuntimeError(
                f"Found config {path} but PyYAML is not installed. "
                "Install python3-yaml / add pyyaml to the nix shell.")
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user)
        cfg["_config_path"] = str(path)
    else:
        cfg["_config_path"] = "(built-in defaults)"
    if not cfg["general"].get("results_dir"):
        cfg["general"]["results_dir"] = str(default_results_dir())
    return cfg
