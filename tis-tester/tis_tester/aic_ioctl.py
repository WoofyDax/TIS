"""Minimal, allow-listed AIC8800 private-ioctl client.

This implements the small safe subset of AICSemi's ``wifi_test`` ABI used
for TIS.  Calibration, EFUSE, MAC-address, tone, and arbitrary private
commands are deliberately impossible through this class.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct

from .backends.base import BackendError

SIOCDEVPRIVATE = 0x89F0
TXRX_PARA = SIOCDEVPRIVATE + 1
MAX_DRV_CMD_SIZE = 1536
IFNAMSIZ = 16

SAFE_COMMANDS = {
    "set_tx", "set_txstop", "set_rx", "set_rxstop", "get_rx_result",
}


class _AndroidWifiPrivCmd(ctypes.Structure):
    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("used_len", ctypes.c_int),
        ("total_len", ctypes.c_int),
    ]


class _Ifru(ctypes.Union):
    _fields_ = [("data", ctypes.c_void_p), ("pad", ctypes.c_ubyte * 24)]


class _Ifreq(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * IFNAMSIZ), ("ifru", _Ifru)]


class AicPrivateIoctl:
    """Send only TIS-safe commands to ``aic8800_fdrv``."""

    def __init__(self, interface: str):
        encoded = interface.encode("ascii", "strict")
        if not encoded or len(encoded) >= IFNAMSIZ:
            raise ValueError(f"Invalid interface name: {interface!r}")
        self.interface = interface
        self._libc = None

    def command(self, name: str, *args: int) -> bytes:
        key = name.lower()
        if key not in SAFE_COMMANDS:
            raise BackendError(f"Blocked unsafe AIC private command: {name}")
        if any(not isinstance(v, int) for v in args):
            raise BackendError("AIC RF command arguments must be integers")
        if os.name == "nt":
            raise BackendError("AIC private ioctl is only available on Linux")
        if self._libc is None:
            self._libc = ctypes.CDLL(None, use_errno=True)

        text = " ".join([key, *(str(v) for v in args)]) + " "
        raw = text.encode("ascii")
        if len(raw) >= MAX_DRV_CMD_SIZE:
            raise BackendError("AIC RF command is too long")

        buf = ctypes.create_string_buffer(MAX_DRV_CMD_SIZE)
        ctypes.memmove(buf, raw, len(raw))
        priv = _AndroidWifiPrivCmd(
            ctypes.cast(buf, ctypes.c_void_p), len(raw), MAX_DRV_CMD_SIZE
        )
        ifr = _Ifreq()
        ifr.name = self.interface.encode("ascii")
        ifr.ifru.data = ctypes.addressof(priv)

        fd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rc = self._libc.ioctl(fd.fileno(), TXRX_PARA, ctypes.byref(ifr))
        finally:
            fd.close()
        if rc < 0:
            err = ctypes.get_errno()
            raise BackendError(
                f"AIC ioctl {key} failed on {self.interface}: "
                f"[{err}] {os.strerror(err)}. The RF-test firmware may not be active."
            )
        return bytes(buf.raw)

    def rx_result(self) -> tuple[int, int]:
        data = self.command("get_rx_result")
        fcs_ok, total = struct.unpack_from("<II", data)
        if total < fcs_ok:
            raise BackendError(
                f"Invalid AIC RX counters: fcs_ok={fcs_ok}, total={total}"
            )
        return fcs_ok, total - fcs_ok
