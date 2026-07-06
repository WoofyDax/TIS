"""Cross-platform laptop serial controller for PAMIR.

Only one process can own the USB serial port. Close PuTTY, ``screen``, or any
other serial terminal before calling a command. ``launch`` / ``dashboard``
starts the device TUI and then hands the port to PuTTY on Windows or pyserial
miniterm on macOS/Linux.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from .backends.base import BackendError


_AUTO_PORT_NAMES = {"", "auto", "default"}


def available_ports() -> list:
    """Return pyserial port records without opening any device."""
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise BackendError(
            "Serial support needs pyserial: pip install 'tis-tester[serial]'"
        ) from e
    return list(list_ports.comports())


def format_ports() -> str:
    ports = available_ports()
    if not ports:
        return (
            "No serial ports found. Connect the PAMIR USB serial cable and retry."
        )
    rows = []
    for item in ports:
        details = getattr(item, "description", None) or "serial device"
        manufacturer = getattr(item, "manufacturer", None)
        if manufacturer and manufacturer not in details:
            details += f"; {manufacturer}"
        rows.append(f"{item.device}\t{details}")
    return "\n".join(rows)


def resolve_port(port: str | None = "auto", records: list | None = None) -> str:
    """Resolve ``auto`` to one likely USB console, or fail without guessing."""
    requested = (port or "auto").strip()
    if requested.lower() not in _AUTO_PORT_NAMES:
        return requested

    records = available_ports() if records is None else records
    candidates = []
    preferred = []
    for item in records:
        device = str(item.device)
        low = device.lower()
        description = str(getattr(item, "description", "") or "").lower()
        identity = " ".join([
            description,
            str(getattr(item, "manufacturer", "") or "").lower(),
            str(getattr(item, "hwid", "") or "").lower(),
        ])
        is_usb = getattr(item, "vid", None) is not None or "usb" in identity
        if "bluetooth-incoming-port" in low or "bluetooth" in description:
            continue
        if sys.platform == "darwin":
            # /dev/cu.* is the correct outgoing/call-up endpoint on macOS.
            if low.startswith("/dev/cu.") and any(
                token in low for token in ("usb", "serial", "slab", "wch")
            ):
                candidates.append(device)
        elif sys.platform.startswith("win"):
            if re.fullmatch(r"COM\d+", device, re.IGNORECASE):
                candidates.append(device)
                if is_usb:
                    preferred.append(device)
        elif any(low.startswith(prefix) for prefix in
                 ("/dev/ttyusb", "/dev/ttyacm", "/dev/serial/")):
            candidates.append(device)

    # A Windows system often exposes a built-in COM1 alongside one USB UART.
    # Prefer the USB-described record, but still require an explicit choice
    # when two USB adapters are attached.
    if preferred:
        candidates = preferred

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        found = ", ".join(str(p.device) for p in records) or "none"
        raise BackendError(
            "Could not auto-detect the PAMIR USB serial port. "
            f"Ports found: {found}. Run 'tis-test serial ports', then pass --port."
        )
    raise BackendError(
        "More than one USB serial port is available: " + ", ".join(candidates) +
        ". Pass the PAMIR port explicitly with --port."
    )


class SerialConsole:
    def __init__(self, port: str = "auto", baud: int = 1_500_000,
                 timeout: float = 10.0):
        try:
            import serial
        except ImportError as e:
            raise BackendError(
                "Serial support needs pyserial: pip install 'tis-tester[serial]'"
            ) from e
        self._serial_module = serial
        self.port = resolve_port(port)
        self.baud, self.timeout = baud, timeout
        self.serial = None

    def open(self) -> None:
        try:
            self.serial = self._serial_module.Serial(
                self.port, self.baud, timeout=0.15, write_timeout=2,
                rtscts=False, dsrdtr=False,
            )
            self.serial.dtr = True
            self.serial.rts = True
            self.serial.reset_input_buffer()
        except Exception as e:
            raise BackendError(
                f"Could not open {self.port} at {self.baud}: {e}. "
                "Close PuTTY, screen, miniterm, and other serial programs first."
            ) from e

    def close(self) -> None:
        if self.serial and self.serial.is_open:
            self.serial.close()

    def interrupt(self) -> None:
        self.serial.write(b"\x03\r\n")
        self.serial.flush()
        time.sleep(0.5)

    def command(self, command: str, timeout: float | None = None) -> tuple[str, int]:
        if len(command.encode()) > 350:
            raise BackendError("Serial console commands are capped at 350 bytes")
        token = uuid.uuid4().hex[:12]
        marker = f"__TIS_{token}_RC="
        wire = f"{command}; printf '{marker}%s__\\n' $?\r\n".encode()
        self.serial.write(b"\x15" + wire)  # Ctrl-U clears any partial line
        self.serial.flush()
        deadline = time.monotonic() + (timeout or self.timeout)
        buf = bytearray()
        pattern = re.compile(re.escape(marker) + r"(\d+)__")
        while time.monotonic() < deadline:
            buf.extend(self.serial.read(self.serial.in_waiting or 1))
            text = buf.decode("utf-8", "replace")
            match = pattern.search(text)
            if match:
                return text, int(match.group(1))
        raise BackendError(
            f"No shell response on {self.port} at {self.baud} baud. "
            "The tested PAMIR console rate is 1500000."
        )


def status(port: str | None, baud: int) -> str:
    con = SerialConsole(port, baud)
    try:
        con.open()
        out, rc = con.command(
            "uname -a; cat /sys/devices/platform/aic-bsp/aicbsp_info/cpmode; "
            "ip -brief link; hciconfig hci0"
        )
        if rc:
            raise BackendError(f"Remote status command failed ({rc})")
        return out
    finally:
        con.close()


def restore(port: str | None, baud: int, reboot: bool = False) -> str:
    con = SerialConsole(port, baud, timeout=30)
    try:
        con.open()
        con.interrupt()
        cmd = "cd ~/tis-tester && nix develop -c python -m tis_tester.cli restore"
        if reboot:
            cmd += " --reboot"
        out, rc = con.command(cmd, timeout=30)
        if rc:
            raise BackendError(f"Remote restore failed ({rc})\n{out}")
        return out
    finally:
        con.close()


def send_archive(port: str | None, baud: int, archive: str | Path,
                 progress: Callable[[int, int], None] | None = None) -> str:
    """Upload, verify, and unpack a source archive over the console shell."""
    path = Path(archive).expanduser()
    if not path.is_file():
        raise BackendError(f"Archive not found: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    # Leaves ample room under command()'s 350-byte safety limit.
    chunks = [encoded[i:i + 192] for i in range(0, len(encoded), 192)]
    con = SerialConsole(port, baud, timeout=15)
    try:
        con.open()
        con.interrupt()
        _, rc = con.command("stty -echo; : > /tmp/tis-tester.b64")
        if rc:
            raise BackendError("PAMIR could not initialize the serial transfer")
        for index, chunk in enumerate(chunks, start=1):
            _, rc = con.command(
                f"printf '%s' '{chunk}' >> /tmp/tis-tester.b64",
                timeout=15,
            )
            if rc:
                raise BackendError(f"Serial transfer failed at chunk {index}")
            if progress:
                progress(index, len(chunks))
        verify = (
            "base64 -d /tmp/tis-tester.b64 > /tmp/tis-tester.tar.gz && "
            "rm -f /tmp/tis-tester.b64 && "
            f"test \"$(sha256sum /tmp/tis-tester.tar.gz | cut -d' ' -f1)\" = '{digest}'"
        )
        _, rc = con.command(verify, timeout=30)
        if rc:
            raise BackendError("Transferred archive failed SHA-256 verification")
        unpack = (
            "mkdir -p ~/tis-tester && "
            "tar xzf /tmp/tis-tester.tar.gz -C ~/tis-tester && "
            "rm -f /tmp/tis-tester.tar.gz"
        )
        _, rc = con.command(unpack, timeout=30)
        if rc:
            raise BackendError("Archive verified, but PAMIR could not unpack it")
        try:
            con.command("stty echo", timeout=3)
        except BackendError:
            pass
        return f"Installed {len(raw)} bytes in ~/tis-tester (SHA-256 {digest})"
    finally:
        try:
            if con.serial and con.serial.is_open:
                con.serial.write(b"stty echo\r\n")
                con.serial.flush()
        finally:
            con.close()


def _open_serial_terminal(port: str, baud: int) -> None:
    if os.name == "nt":
        putty = shutil.which("putty") or r"C:\Program Files\PuTTY\putty.exe"
        if os.path.isfile(putty) or shutil.which("putty"):
            subprocess.Popen([
                putty, "-serial", port, "-sercfg", f"{baud},8,n,1,N",
            ])
            return

    # pyserial is already a serial-extra dependency and supports macOS's
    # non-standard 1,500,000 baud rate more reliably than the bundled screen.
    command = [
        sys.executable, "-m", "serial.tools.miniterm",
        "--raw", "--eol", "LF", port, str(baud),
    ]
    try:
        rc = subprocess.call(command)
    except OSError as e:
        raise BackendError(f"Could not start the serial terminal: {e}") from e
    if rc:
        raise BackendError(f"Serial terminal exited with status {rc}")


def launch(port: str | None, baud: int, mock: bool = False,
           open_terminal: bool = True) -> None:
    con = SerialConsole(port, baud)
    resolved_port = con.port
    try:
        con.open()
        con.interrupt()
        suffix = " --mock" if mock else ""
        # Do not use exec: returning from the TUI must return to a usable shell.
        con.serial.write(
            ("\x15cd ~/tis-tester && export TERM=xterm && "
             f"nix develop -c python -m tis_tester.cli interactive{suffix}\r\n").encode()
        )
        con.serial.flush()
        time.sleep(1)
    finally:
        con.close()

    if open_terminal:
        _open_serial_terminal(resolved_port, baud)
