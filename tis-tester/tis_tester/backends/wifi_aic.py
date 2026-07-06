"""Wi-Fi RF-test backend for the AIC8800D80 U02 (FCS960K-N).

The AIC8800 exposes non-signalling RF test (continuous TX / continuous RX
with FCS statistics) through the vendor test firmware. The userspace entry
point differs between driver drops (aic_rftest console tool, debugfs node,
or private ioctls), so this backend shells out through *command templates*
defined in tis_config.yaml. Only the templates need to change to match your
driver; the rest of the application is agnostic.

Cumulative counters: many vendor tools reset FCS counters on rx_start.
We track a session baseline so displayed counters always start at zero for
each RX session and survive tools that report absolute totals.
"""

from __future__ import annotations
import re
from pathlib import Path

from .base import RadioBackend, RxStats, TestParams, BackendError, run_shell
from .. import rates


class WifiAicBackend(RadioBackend):
    name = "wifi-aic-shell"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        w = cfg["wifi"]
        self.kind = w.get("backend", "ioctl")
        self.iface = w["interface"]
        self.cmds = w["commands"]
        self.rx_regex = {k: re.compile(v, re.I)
                         for k, v in w["rx_result_regex"].items()}
        self.bw_codes = {int(k): int(v) for k, v in w["bw_codes"].items()}
        self.rate_overrides = {k.lower(): int(v)
                               for k, v in w.get("rate_code_overrides", {}).items()}
        self._base_ok = 0
        self._base_err = 0
        self._last = RxStats()
        self._ioctl = None
        self.require_rf_mode = bool(w.get("require_rf_mode", True))
        self.mode_selector = w.get("mode_selector")
        self.packet_interval_us = int(w.get("packet_interval_us", 1000))

    # ------------------------------------------------------------------ util
    def _fmt(self, template: str, p: TestParams) -> str:
        rate = rates.lookup_wifi_rate(p.rate)
        rate_code = self.rate_overrides.get(rate.name.lower(), rate.code)
        return template.format(
            iface=self.iface,
            freq=rates.channel_to_freq(p.band, p.channel),
            channel=p.channel,
            band=p.band,
            bw=p.bandwidth_mhz,
            bw_code=self.bw_codes.get(p.bandwidth_mhz, 0),
            rate_code=rate_code,
            rate_name=rate.name,
            power=p.tx_power_dbm,
        )

    def _run(self, key: str, p: TestParams | None = None) -> str:
        tpl = self.cmds.get(key)
        if not tpl:
            raise BackendError(f"wifi.commands.{key} is not configured "
                               f"in tis_config.yaml")
        if isinstance(tpl, list):
            out = ""
            for t in tpl:
                out += run_shell(self._fmt(t, p) if p else t)
            return out
        return run_shell(self._fmt(tpl, p) if p else tpl)

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        if self.kind == "ioctl":
            if self.require_rf_mode and self.mode_selector:
                try:
                    text = Path(self.mode_selector).read_text()
                except OSError as e:
                    raise BackendError(
                        f"Cannot read AIC mode selector {self.mode_selector}: {e}"
                    ) from e
                if not re.search(r"Current:\s*1\b", text):
                    raise BackendError(
                        "AIC Wi-Fi RF-test firmware is not selected. This PAMIR "
                        "image uses a built-in driver and cannot safely switch it "
                        "at runtime; boot a vendor RF-test-enabled image first."
                    )
            try:
                from ..aic_ioctl import AicPrivateIoctl
                self._ioctl = AicPrivateIoctl(self.iface)
            except (OSError, ValueError) as e:
                raise BackendError(str(e)) from e
            return
        for cmd in self.cmds.get("init") or []:
            run_shell(cmd)

    def close(self) -> None:
        super().close()
        if self.kind == "ioctl":
            return
        for cmd in self.cmds.get("deinit") or []:
            try:
                run_shell(cmd)
            except BackendError:
                pass

    # --------------------------------------------------------------- control
    def _validate(self, p: TestParams) -> None:
        try:
            rates.channel_to_freq(p.band, p.channel)
            rate = rates.lookup_wifi_rate(p.rate)
        except ValueError as e:
            raise BackendError(str(e)) from e
        if p.bandwidth_mhz not in self.bw_codes:
            raise BackendError(
                f"Bandwidth must be one of {sorted(self.bw_codes)} MHz"
            )
        if p.band not in rate.bands:
            raise BackendError(f"{rate.name} is not valid on {p.band}")
        if rate.family == "legacy-b" and p.bandwidth_mhz != 20:
            raise BackendError("802.11b rates are 20 MHz only")
        if not (rates.TX_POWER_MIN_DBM <= p.tx_power_dbm <= rates.TX_POWER_MAX_DBM):
            raise BackendError(f"TX power must be "
                               f"{rates.TX_POWER_MIN_DBM}-{rates.TX_POWER_MAX_DBM} dBm")

    def start_tx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        if self.kind == "ioctl":
            rate = rates.lookup_wifi_rate(p.rate)
            phy_mode = {"legacy-b": 0, "legacy-ag": 0,
                        "HT": 2, "VHT": 4, "HE": 5}[rate.family]
            length = (1024 if rate.family.startswith("legacy") else
                      4096 if p.bandwidth_mhz == 20 else 8192)
            self._ioctl.command(
                "set_tx", p.channel, self.bw_codes[p.bandwidth_mhz],
                phy_mode, rate.index if not rate.family.startswith("legacy")
                else rate.code, length, self.packet_interval_us,
                p.tx_power_dbm,
            )
        else:
            self._run("tx_start", p)
        self.mode, self.params = "tx", p

    def start_rx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        if self.kind == "ioctl":
            self._ioctl.command(
                "set_rx", p.channel, self.bw_codes[p.bandwidth_mhz]
            )
        else:
            self._run("rx_start", p)
        self.mode, self.params = "rx", p
        # establish counter baseline for tools reporting absolute totals
        try:
            snap = self._read_counters()
            self._base_ok, self._base_err = snap
        except BackendError:
            self._base_ok = self._base_err = 0
        self._last = RxStats(expected_packets=p.expected_packets)

    def stop(self) -> RxStats | None:
        if self.mode == "tx":
            if self.kind == "ioctl":
                self._ioctl.command("set_txstop")
            else:
                self._run("tx_stop")
            self.mode = None
            return None
        if self.mode == "rx":
            final = self.poll_rx()
            if self.kind == "ioctl":
                self._ioctl.command("set_rxstop")
            else:
                self._run("rx_stop")
            self.mode = None
            return final
        return None

    def reset_rx_counters(self) -> None:
        if self.kind == "ioctl":
            ok, err = self._read_counters()
            self._base_ok, self._base_err = ok, err
            return
        if self.cmds.get("rx_reset"):
            try:
                self._run("rx_reset", self.params)
                self._base_ok = self._base_err = 0
                return
            except BackendError:
                pass
        # fallback: software baseline
        ok, err = self._read_counters()
        self._base_ok, self._base_err = ok, err

    # ----------------------------------------------------------------- stats
    def _read_counters(self) -> tuple[int, int]:
        if self.kind == "ioctl":
            self._last_rssi = None
            return self._ioctl.rx_result()
        out = self._run("rx_result", self.params)
        m_ok = self.rx_regex["fcs_ok"].search(out)
        m_err = self.rx_regex["fcs_err"].search(out)
        if not (m_ok and m_err):
            raise BackendError(f"Could not parse rx_result output:\n{out.strip()}")
        m_rssi = self.rx_regex.get("rssi")
        rssi = None
        if m_rssi:
            m = m_rssi.search(out)
            if m:
                rssi = float(m.group(1))
        self._last_rssi = rssi
        return int(m_ok.group(1)), int(m_err.group(1))

    def poll_rx(self) -> RxStats:
        if self.mode != "rx":
            raise BackendError("Receiver is not running")
        ok, err = self._read_counters()
        st = RxStats(
            packets_ok=max(0, ok - self._base_ok),
            packets_err=max(0, err - self._base_err),
            rssi_dbm=getattr(self, "_last_rssi", None),
            expected_packets=self.params.expected_packets if self.params else None,
        )
        self._last = st
        return st
