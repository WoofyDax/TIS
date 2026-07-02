"""Test session orchestration: runs TX/RX sessions, polls stats, logs CSV."""

from __future__ import annotations
import csv
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .backends import RadioBackend, RxStats, TestParams


def fmt_stats(st: RxStats) -> str:
    per = st.per
    rssi = f"{st.rssi_dbm:>6.1f} dBm" if st.rssi_dbm is not None else "   n/a"
    per_s = f"{per * 100:6.2f} %" if per is not None else "   n/a"
    return (f"RSSI {rssi} | PER {per_s} | "
            f"CRC ok {st.packets_ok:>8} err {st.packets_err:>6} | "
            f"packets {st.packets_total:>8}")


class CsvLogger:
    FIELDS = ["timestamp", "radio", "band", "channel", "bandwidth_mhz", "rate",
              "tx_power_dbm", "rssi_dbm", "per", "packets_ok", "packets_err",
              "packets_total", "expected_packets", "event"]

    def __init__(self, results_dir: str | Path, name: str | None = None):
        d = Path(results_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = d / (name or f"tis_{stamp}.csv")
        self._fh = open(self.path, "w", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        self._w.writeheader()

    def log(self, p: TestParams, st: RxStats, event: str = "sample") -> None:
        self._w.writerow({
            "timestamp": datetime.fromtimestamp(st.timestamp).isoformat(timespec="milliseconds"),
            "radio": p.radio, "band": p.band, "channel": p.channel,
            "bandwidth_mhz": p.bandwidth_mhz, "rate": p.rate,
            "tx_power_dbm": p.tx_power_dbm,
            "rssi_dbm": st.rssi_dbm,
            "per": round(st.per, 6) if st.per is not None else "",
            "packets_ok": st.packets_ok, "packets_err": st.packets_err,
            "packets_total": st.packets_total,
            "expected_packets": st.expected_packets or "",
            "event": event,
        })
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def run_rx_session(backend: RadioBackend, p: TestParams,
                   duration_s: float | None,
                   poll_interval_s: float,
                   logger: CsvLogger | None = None,
                   on_sample: Callable[[RxStats], None] | None = None,
                   should_stop: Callable[[], bool] | None = None) -> RxStats:
    """Run a continuous-RX dwell; returns final stats."""
    backend.start_rx(p)
    t_end = time.time() + duration_s if duration_s else None
    try:
        while True:
            time.sleep(poll_interval_s)
            st = backend.poll_rx()
            if logger:
                logger.log(p, st)
            if on_sample:
                on_sample(st)
            if should_stop and should_stop():
                break
            if t_end and time.time() >= t_end:
                break
    finally:
        final = backend.stop() or RxStats(expected_packets=p.expected_packets)
    if logger and final:
        logger.log(p, final, event="final")
    return final


def run_tx_session(backend: RadioBackend, p: TestParams,
                   duration_s: float | None,
                   should_stop: Callable[[], bool] | None = None) -> None:
    """Run continuous TX for duration (None = until should_stop/Ctrl-C)."""
    backend.start_tx(p)
    t_end = time.time() + duration_s if duration_s else None
    try:
        while True:
            time.sleep(0.2)
            if should_stop and should_stop():
                break
            if t_end and time.time() >= t_end:
                break
    finally:
        backend.stop()


def sweep(backend: RadioBackend, base: TestParams, channels: list[int],
          rates_list: list[str], dwell_s: float, poll_interval_s: float,
          logger: CsvLogger | None = None,
          progress: Callable[[str], None] = print) -> Iterator[tuple[TestParams, RxStats]]:
    """Automated RX sweep across channels x rates (TIS characterization)."""
    for rate in rates_list:
        for ch in channels:
            p = TestParams(**{**asdict(base), "channel": ch, "rate": rate})
            progress(f"[sweep] {p.radio} {p.band} ch{ch} {rate} "
                     f"bw{p.bandwidth_mhz} — dwell {dwell_s}s")
            final = run_rx_session(backend, p, dwell_s, poll_interval_s, logger)
            yield p, final
