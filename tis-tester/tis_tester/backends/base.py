"""Backend abstraction shared by Wi-Fi and BT test drivers."""

from __future__ import annotations
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class BackendError(RuntimeError):
    pass


@dataclass
class RxStats:
    """Snapshot of receiver statistics.

    packets_ok / packets_err are cumulative CRC/FCS good/bad counters as
    reported by the DUT. PER is computed either against expected_packets
    (when the chamber instrument transmits a known burst count -- the
    normal TIS procedure) or, if unknown, as err/(ok+err).
    """
    packets_ok: int = 0
    # None means the radio API does not expose a bad-CRC counter.  BLE DTM
    # reports received packets only; representing that as zero would invent
    # a measurement and produce a misleading CRC/PER display.
    packets_err: int | None = 0
    rssi_dbm: float | None = None
    expected_packets: int | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def packets_total(self) -> int:
        return self.packets_ok + (self.packets_err or 0)

    @property
    def per(self) -> float | None:
        if self.expected_packets and self.expected_packets > 0:
            return max(0.0, 1.0 - self.packets_ok / self.expected_packets)
        if self.packets_err is not None and self.packets_total > 0:
            return self.packets_err / self.packets_total
        return None


@dataclass
class TestParams:
    radio: str = "wifi"                 # "wifi" | "bt"
    band: str = "2.4GHz"
    channel: int = 1                    # wifi channel or BLE RF channel 0-39
    bandwidth_mhz: int = 20
    rate: str = "HE-MCS0"               # wifi rate name, or BT PHY key
    tx_power_dbm: int = 10              # 0..23
    payload: str = "prbs9"              # BT only
    payload_len: int = 37               # BT only
    expected_packets: int | None = None # for PER against a known TX count


class RadioBackend(ABC):
    """One backend instance == one radio (Wi-Fi or BT) in test mode."""

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode: str | None = None    # None | "tx" | "rx"
        self.params: TestParams | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None: ...
    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    # -- control -----------------------------------------------------------
    @abstractmethod
    def start_tx(self, p: TestParams) -> None: ...

    @abstractmethod
    def start_rx(self, p: TestParams) -> None: ...

    @abstractmethod
    def stop(self) -> RxStats | None:
        """Stop TX or RX. When stopping RX, return the final stats."""

    @abstractmethod
    def poll_rx(self) -> RxStats:
        """Non-destructive read of live RX counters."""

    def reset_rx_counters(self) -> None: ...


# ---------------------------------------------------------------------------

def run_shell(cmd: str, timeout: float = 10.0) -> str:
    """Run one shell command, return stdout+stderr text; raise on failure."""
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"Timed out: {cmd}") from e
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise BackendError(f"Command failed ({proc.returncode}): {cmd}\n{out.strip()}")
    return out


def run_argv(argv: list[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise BackendError(f"Tool not found: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"Timed out: {shlex.join(argv)}") from e
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise BackendError(f"Command failed ({proc.returncode}): "
                           f"{shlex.join(argv)}\n{out.strip()}")
    return out
