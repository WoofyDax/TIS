"""Laptop-side serial console launcher for PAMIR.

Only one process can own a Windows COM port. Close PuTTY before calling
``status`` or ``restore``. ``launch`` / ``dashboard`` starts the TUI and then
hands COM5 to PuTTY for interactive use.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid

from .backends.base import BackendError


class SerialConsole:
    def __init__(self, port: str = "COM5", baud: int = 1_500_000,
                 timeout: float = 10.0):
        try:
            import serial
        except ImportError as e:
            raise BackendError(
                "Serial support needs pyserial: pip install 'tis-tester[serial]'"
            ) from e
        self._serial_module = serial
        self.port, self.baud, self.timeout = port, baud, timeout
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
                f"Could not open {self.port} at {self.baud}: {e}. Close PuTTY first."
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


def status(port: str, baud: int) -> str:
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


def restore(port: str, baud: int, reboot: bool = False) -> str:
    con = SerialConsole(port, baud, timeout=30)
    try:
        con.open()
        con.interrupt()
        cmd = "cd ~/tis-tester && nix develop -c ./result/bin/tis-test restore"
        if reboot:
            cmd += " --reboot"
        out, rc = con.command(cmd, timeout=30)
        if rc:
            raise BackendError(f"Remote restore failed ({rc})\n{out}")
        return out
    finally:
        con.close()


def launch(port: str, baud: int, mock: bool = False,
           open_putty: bool = True) -> None:
    con = SerialConsole(port, baud)
    try:
        con.open()
        con.interrupt()
        suffix = " --mock" if mock else ""
        # Do not use exec: returning from the TUI must return to a usable shell.
        con.serial.write(
            ("\x15cd ~/tis-tester && export TERM=xterm && "
             f"nix develop -c tis-test interactive{suffix}\r\n").encode()
        )
        con.serial.flush()
        time.sleep(1)
    finally:
        con.close()

    if open_putty and os.name == "nt":
        putty = shutil.which("putty") or r"C:\Program Files\PuTTY\putty.exe"
        if os.path.isfile(putty) or shutil.which("putty"):
            subprocess.Popen([
                putty, "-serial", port, "-sercfg",
                f"{baud},8,n,1,N",
            ])
