"""Bluetooth Direct Test Mode backend using AIC's raw UART RF tool.

This bypasses BlueZ/hcitool and talks to the controller through the vendor
`bt_test` UART path, which is required on the AIC8800D80/FCS960K-N for BLE
DTM RX packet counting.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .base import RadioBackend, RxStats, TestParams, BackendError, run_argv, run_shell
from .. import rates


class BtAicUartBackend(RadioBackend):
    name = "bt-aic-uart"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        b = cfg["bt"]
        self.tool = b.get("tool_path", "/root/aicrf-test-extract/usr/bin/bt_test")
        self.uart_dev = b.get("uart_dev", "/dev/ttyS4")
        self.uart_baud = int(b.get("uart_baud", 1500000))
        self.startup_delay_s = float(b.get("startup_delay_s", 0.5))
        self.service_stop = list(b.get("service_stop", [
            "aic8800-bt.service",
            "bluetooth.service",
        ]))
        self.service_start = list(b.get("service_start", [
            "aic8800-bt.service",
            "bluetooth.service",
        ]))
        self.rfkill_block = b.get("rfkill_block", "bluetooth")
        self.rfkill_unblock = b.get("rfkill_unblock", "bluetooth")
        self.service_log = Path(b.get("service_log", "/tmp/tis_bt_test_service.log"))
        self._proc: subprocess.Popen[str] | None = None
        self._service_log_fh = None
        self._last_rx_count = 0

    @staticmethod
    def _extract_event_bytes(out: str) -> list[int]:
        candidates: list[list[int]] = []
        for line in out.splitlines():
            if "EVENT(" not in line:
                continue
            _, _, tail = line.partition(":")
            hexes = re.findall(r"\b[0-9A-Fa-f]{2}\b", tail)
            if hexes:
                candidates.append([int(tok, 16) for tok in hexes])
        if not candidates:
            return []
        return candidates[-1]

    @classmethod
    def _parse_test_end(cls, out: str) -> int:
        data = cls._extract_event_bytes(out)
        if not data:
            raise BackendError(f"Could not parse bt_test EVENT response:\n{out.strip()}")
        if len(data) < 9:
            raise BackendError(f"Short bt_test EVENT response: {data}")
        status = data[6]
        if status != 0x00:
            raise BackendError(f"LE Test End returned status 0x{status:02x}")
        return data[7] | (data[8] << 8)

    def _run_bt_test(self, *args: str, timeout: float = 10.0) -> str:
        return run_argv([self.tool, *args], timeout=timeout)

    def _service_cmd(self, action: str, services: list[str]) -> None:
        if services:
            run_argv(["systemctl", action, *services], timeout=20.0)

    def _rfkill(self, action: str, target: str) -> None:
        if target:
            run_argv(["rfkill", action, target], timeout=10.0)

    def _kill_proc(self) -> None:
        if not self._proc:
            return
        proc = self._proc
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _stop_existing_tool(self) -> None:
        try:
            run_shell(f"pkill -f '{self.tool}'", timeout=5.0)
        except BackendError:
            pass

    def open(self) -> None:
        if not os.path.exists(self.tool):
            raise BackendError(f"bt_test tool not found: {self.tool}")
        if not os.path.exists(self.uart_dev):
            raise BackendError(f"UART device not found: {self.uart_dev}")
        self._stop_existing_tool()
        self._rfkill("block", self.rfkill_block)
        self._service_cmd("stop", self.service_stop)
        self._rfkill("unblock", self.rfkill_unblock)
        self.service_log.parent.mkdir(parents=True, exist_ok=True)
        self._service_log_fh = open(self.service_log, "a+", encoding="utf-8", errors="replace")
        self._proc = subprocess.Popen(
            [self.tool, "-s", "uart", str(self.uart_baud), self.uart_dev],
            stdout=self._service_log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        time.sleep(self.startup_delay_s)
        if self._proc.poll() is not None:
            raise BackendError("bt_test service exited immediately")

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
        self._kill_proc()
        if self._service_log_fh is not None:
            self._service_log_fh.close()
            self._service_log_fh = None
        try:
            self._rfkill("block", self.rfkill_block)
        except Exception:
            pass
        try:
            self._rfkill("unblock", self.rfkill_unblock)
        except Exception:
            pass
        try:
            self._service_cmd("start", self.service_start)
        except Exception:
            pass
        self.mode = None
        self.params = None

    def _validate(self, p: TestParams) -> None:
        if p.channel not in rates.BLE_CHANNELS:
            raise BackendError("BLE RF channel must be 0-39")
        if p.rate not in rates.BT_PHYS:
            raise BackendError(f"BT PHY must be one of {list(rates.BT_PHYS)}")
        if p.rate != "1M":
            raise BackendError(
                "The current AIC bt_test DTM path has only been validated for BLE 1M PHY."
            )
        if p.payload not in rates.BT_PAYLOADS:
            raise BackendError(f"payload must be one of {list(rates.BT_PAYLOADS)}")
        if not (0 <= p.payload_len <= 255):
            raise BackendError("payload_len must be 0-255")

    def _ensure_open(self) -> None:
        if not self._proc or self._proc.poll() is not None:
            raise BackendError("bt_test service is not running")

    def start_tx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        self._ensure_open()
        out = self._run_bt_test(
            "-H", "le_tx",
            "chnl", str(p.channel),
            "len", str(p.payload_len),
            "le_phy", "1",
            "mod_idx", "0",
            timeout=10.0,
        )
        if "status 0x00" not in out.lower() and "0x00" not in out.lower():
            # bt_test output varies; nonzero exit already raises.
            pass
        self.mode, self.params = "tx", p

    def start_rx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        self._ensure_open()
        self._run_bt_test(
            "-H", "le_rx",
            "chnl", str(p.channel),
            "len", str(p.payload_len),
            "le_phy", "1",
            "mod_idx", "0",
            timeout=10.0,
        )
        self.mode, self.params = "rx", p
        self._last_rx_count = 0

    def _test_end(self) -> int:
        last_error: Exception | None = None
        for _ in range(3):
            out = self._run_bt_test("-c", "01", "1F", "20", "00", timeout=10.0)
            try:
                return self._parse_test_end(out)
            except BackendError as e:
                last_error = e
                time.sleep(0.25)
        if self.service_log.is_file():
            try:
                text = self.service_log.read_text(encoding="utf-8", errors="replace")
                return self._parse_test_end(text)
            except Exception as e:
                last_error = e
        raise BackendError(str(last_error) if last_error else "LE Test End did not return an EVENT response")

    def stop(self) -> RxStats | None:
        if self.mode == "tx":
            self._test_end()
            self.mode = None
            self.params = None
            return None
        if self.mode == "rx":
            count = self._test_end()
            self._last_rx_count = count
            st = RxStats(
                packets_ok=count,
                expected_packets=self.params.expected_packets if self.params else None,
            )
            self.mode = None
            self.params = None
            return st
        return None

    def reset_rx_counters(self) -> None:
        if self.mode == "rx" and self.params:
            self._test_end()
            params = self.params
            self.mode = None
            self.params = None
            self.start_rx(params)

    def poll_rx(self) -> RxStats:
        if self.mode != "rx":
            raise BackendError("Receiver is not running")
        count = self._test_end()
        params = self.params
        self.mode = None
        self.params = None
        self.start_rx(params)
        self._last_rx_count = count
        return RxStats(
            packets_ok=count,
            expected_packets=params.expected_packets if params else None,
        )
