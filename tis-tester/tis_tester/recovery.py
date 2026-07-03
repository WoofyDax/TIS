"""Best-effort stop and normal-mode restoration for the PAMIR image."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _write_selector(path: str | None, value: str, report: dict) -> None:
    if not path:
        return
    p = Path(path)
    try:
        p.write_text(value + "\n")
        report["actions"].append(f"{p} <- {value}")
    except OSError as e:
        report["errors"].append(f"Could not write {p}: {e}")


def _run(command: str, report: dict, required: bool = False) -> None:
    proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                          timeout=20)
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    report["actions"].append(command + (f" -> {text}" if text else ""))
    if required and proc.returncode:
        report["errors"].append(
            f"Command failed ({proc.returncode}): {command}: {text}"
        )


def restore_normal(cfg: dict, reboot: bool = False) -> dict:
    """Select normal firmware, restore services, verify, optionally reboot.

    Selecting mode 0 is always attempted before service work.  On the tested
    built-in-driver image, a reboot is required if RF firmware was truly
    loaded; callers can request that explicitly with ``reboot=True``.
    """
    w = cfg["wifi"]
    report = {"ok": False, "actions": [], "errors": [],
              "reboot_requested": bool(reboot), "verification": {}}

    # Stop both possible test paths. Failures are expected when a radio is idle.
    try:
        from .aic_ioctl import AicPrivateIoctl
        io = AicPrivateIoctl(w["interface"])
        for command in ("set_txstop", "set_rxstop"):
            try:
                io.command(command)
                report["actions"].append(command)
            except Exception as e:
                report["actions"].append(f"{command} skipped: {e}")
    except Exception as e:
        report["actions"].append(f"Wi-Fi stop unavailable: {e}")

    try:
        b = cfg.get("bt", {})
        if b.get("backend") == "aic_uart":
            tool = b.get("tool_path", "/root/aicrf-test-extract/usr/bin/bt_test")
            _run(f"{tool} -c 01 1F 20 00", report)
            _run(f"pkill -f '{tool}'", report)
            if b.get("rfkill_block"):
                _run(f"rfkill block {b['rfkill_block']}", report)
            if b.get("rfkill_unblock"):
                _run(f"rfkill unblock {b['rfkill_unblock']}", report)
        else:
            _run("hcitool -i hci0 cmd 0x08 0x001f", report)
    except Exception as e:
        report["actions"].append(f"BT test-end unavailable: {e}")

    _write_selector(w.get("module_testmode"), "0", report)
    _write_selector(w.get("mode_selector"), "0", report)

    for command in w.get("restore_commands") or []:
        try:
            _run(command, report, required=True)
        except Exception as e:
            report["errors"].append(f"Restore command error: {command}: {e}")

    selector = Path(w.get("mode_selector") or "")
    if selector.is_file():
        try:
            text = selector.read_text()
            m = re.search(r"Current:\s*(\d+)", text)
            report["verification"]["selected_mode"] = int(m.group(1)) if m else None
            if m and m.group(1) != "0":
                report["errors"].append("Driver did not accept normal-mode selector")
        except OSError as e:
            report["errors"].append(f"Could not verify mode selector: {e}")

    report["verification"]["wlan_exists"] = Path(
        f"/sys/class/net/{w['interface']}"
    ).exists()
    report["verification"]["hci_exists"] = Path("/sys/class/bluetooth/hci0").exists()
    if not report["verification"]["wlan_exists"]:
        report["errors"].append(f"Wi-Fi interface {w['interface']} is missing")
    if not report["verification"]["hci_exists"]:
        report["errors"].append("Bluetooth interface hci0 is missing")

    if reboot:
        report["actions"].append("systemctl reboot")
        report["ok"] = not report["errors"]
        subprocess.Popen(["systemctl", "reboot"])
        return report

    report["ok"] = not report["errors"]
    return report
