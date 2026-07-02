"""Mock backend: simulates a DUT so the full UI / automation flow can be
exercised with no hardware attached (backend: mock in tis_config.yaml,
or --mock on the CLI)."""

from __future__ import annotations
import random
import time

from .base import RadioBackend, RxStats, TestParams, BackendError


class MockBackend(RadioBackend):
    name = "mock"

    def __init__(self, cfg: dict, radio: str = "wifi"):
        super().__init__(cfg)
        self.radio = radio
        self._t0 = 0.0
        self._pps = 600            # simulated packets/sec from "instrument"
        self._per = 0.05
        self._rssi = -72.0

    def start_tx(self, p: TestParams) -> None:
        if self.mode:
            self.stop()
        self.mode, self.params = "tx", p

    def start_rx(self, p: TestParams) -> None:
        if self.mode:
            self.stop()
        self.mode, self.params = "rx", p
        self._t0 = time.time()
        # deeper channels -> worse PER, just to make the demo lively
        self._per = min(0.6, 0.02 + 0.01 * (p.channel % 7))
        self._rssi = -60.0 - (p.tx_power_dbm % 5) * 3 - random.random() * 5

    def _counts(self) -> tuple[int, int]:
        dt = max(0.0, time.time() - self._t0)
        total = int(dt * self._pps)
        err = int(total * self._per)
        return total - err, err

    def stop(self) -> RxStats | None:
        if self.mode == "rx":
            st = self.poll_rx()
            self.mode = None
            return st
        self.mode = None
        return None

    def reset_rx_counters(self) -> None:
        self._t0 = time.time()

    def poll_rx(self) -> RxStats:
        if self.mode != "rx":
            raise BackendError("Receiver is not running")
        ok, err = self._counts()
        return RxStats(
            packets_ok=ok,
            packets_err=err,
            rssi_dbm=round(self._rssi + random.uniform(-1.5, 1.5), 1),
            expected_packets=self.params.expected_packets if self.params else None,
        )
