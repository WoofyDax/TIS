"""Interactive curses UI.

Menu flow mirrors the bench spec sheet:

    TIS Testing - BT/WiFi in Receiver mode
      - Select: [WiFi] [BT]
      - Select: [Channel] [Band] [Bandwidth]
      - Transmit PWR: [0 to 23 dBm]
      - Select modulation & data rate: [MCS] [Legacy rate]
      - Select: [start/stop TX continuous] [start/stop RX continuous]
      - Display: RSSI, PER, CRC statistics, Packet Count
"""

from __future__ import annotations

import curses
import time

from . import rates
from .backends import BackendError, TestParams, make_backend
from .report import finish_run, record_run_sample, start_run
from .session import CsvLogger


def _menu(stdscr, title: str, options: list[str], idx: int = 0) -> int | None:
    """Arrow-key menu; returns index or None on Esc."""
    curses.curs_set(0)
    while True:
        stdscr.erase()
        h, _ = stdscr.getmaxyx()
        stdscr.addstr(1, 2, "TIS TESTING - FCS960K-N", curses.A_BOLD)
        stdscr.addstr(2, 2, "BT/WiFi must be in Receiver mode for TIS", curses.A_DIM)
        stdscr.addstr(4, 2, title, curses.A_UNDERLINE)
        top = 6
        for i, opt in enumerate(options[: h - top - 2]):
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            stdscr.addstr(top + i, 4, f" {opt} ", attr)
        stdscr.addstr(
            h - 1,
            2,
            "Up/Down select   Enter confirm   Esc back   q quit",
            curses.A_DIM,
        )
        stdscr.refresh()
        k = stdscr.getch()
        if k in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif k in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif k in (curses.KEY_ENTER, 10, 13):
            return idx
        elif k == 27:
            return None
        elif k in (ord("q"), ord("Q")):
            raise KeyboardInterrupt


def _prompt_int(stdscr, title: str, lo: int, hi: int, default: int) -> int | None:
    curses.echo()
    curses.curs_set(1)
    try:
        while True:
            stdscr.erase()
            stdscr.addstr(1, 2, "TIS TESTING - FCS960K-N", curses.A_BOLD)
            stdscr.addstr(
                4,
                2,
                f"{title}  [{lo}-{hi}]  (default {default})",
                curses.A_UNDERLINE,
            )
            stdscr.addstr(6, 4, "> ")
            stdscr.refresh()
            s = stdscr.getstr(6, 6, 10).decode().strip()
            if s == "":
                return default
            try:
                v = int(s)
                if lo <= v <= hi:
                    return v
            except ValueError:
                pass
    finally:
        curses.noecho()
        curses.curs_set(0)


def _live_screen(
    stdscr, backend, p: TestParams, cfg, logger: CsvLogger | None, allow_tx: bool
) -> None:
    """Start/stop TX-continuous and RX-continuous; live stats display."""
    poll = float(cfg["general"]["poll_interval_s"])
    last = None
    msg = ""
    next_poll = 0.0
    tx_started_at = None
    max_tx = float(cfg["general"].get("max_tx_duration_s", 300))
    run = None
    active_logger = logger
    latest_log_path = None
    latest_report_path = None

    def _open_run(mode: str) -> None:
        nonlocal run, active_logger, latest_log_path, latest_report_path
        if active_logger is not None:
            active_logger.close()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        active_logger = CsvLogger(
            cfg["general"]["results_dir"], name=f"{p.radio}_{mode}_{stamp}.csv"
        )
        latest_log_path = str(active_logger.path)
        latest_report_path = None
        run = start_run(p, mode, "tis-test serial dashboard", latest_log_path)

    def _close_run(message_text: str, final_stats=None) -> None:
        nonlocal run, active_logger, latest_report_path
        if final_stats is not None and run is not None:
            record_run_sample(run, final_stats, event="final")
        if run is not None:
            latest_report_path = str(
                finish_run(run, cfg["general"]["results_dir"], message_text, final_stats)
            )
        if active_logger is not None:
            active_logger.close()
            active_logger = None
        run = None

    def _abort_run() -> None:
        nonlocal run, active_logger
        if active_logger is not None:
            active_logger.close()
            active_logger = None
        run = None

    stdscr.nodelay(True)
    try:
        while True:
            now = time.time()
            if backend.mode == "tx" and tx_started_at is not None:
                if now - tx_started_at >= max_tx:
                    backend.stop()
                    tx_started_at = None
                    msg = f"TX auto-stopped at {max_tx:g}s safety limit"
                    _close_run(msg, None)
            if backend.mode == "rx" and now >= next_poll:
                try:
                    last = backend.poll_rx()
                    if active_logger is not None:
                        active_logger.log(p, last)
                    if run is not None:
                        record_run_sample(run, last)
                except BackendError as e:
                    msg = str(e)
                next_poll = now + poll

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.addstr(1, 2, "TIS TESTING - serial dashboard", curses.A_BOLD)
            stdscr.addstr(
                3,
                2,
                f"Radio: {p.radio.upper()}   Band: {p.band}   "
                f"Ch: {p.channel}   BW: {p.bandwidth_mhz} MHz",
            )
            stdscr.addstr(
                4,
                2,
                f"Rate: {p.rate}   TX pwr: {p.tx_power_dbm} dBm"
                + (f"   Expected pkts: {p.expected_packets}" if p.expected_packets else ""),
            )
            state = {"tx": "TX CONTINUOUS", "rx": "RX CONTINUOUS", None: "IDLE"}[
                backend.mode
            ]
            stdscr.addstr(
                6,
                2,
                f"State: {state}",
                curses.A_REVERSE if backend.mode else curses.A_NORMAL,
            )

            stdscr.addstr(8, 2, "- Display -", curses.A_UNDERLINE)
            if last is not None:
                per = last.per
                rows = [
                    (
                        "RSSI",
                        f"{last.rssi_dbm:.1f} dBm" if last.rssi_dbm is not None else "n/a",
                    ),
                    ("PER", f"{per * 100:.2f} %" if per is not None else "n/a"),
                    ("CRC statistic", f"ok {last.packets_ok}   err "
                     f"{last.packets_err if last.packets_err is not None else 'n/a'}"),
                    ("Packet count", f"{last.packets_total}"),
                ]
            else:
                rows = [
                    ("RSSI", "-"),
                    ("PER", "-"),
                    ("CRC statistic", "-"),
                    ("Packet count", "-"),
                ]
            for i, (k, v) in enumerate(rows):
                stdscr.addstr(9 + i, 4, f"{k:<14}: {v}")

            if msg:
                stdscr.addnstr(14, 2, msg, w - 4, curses.A_DIM)
            if latest_report_path:
                stdscr.addnstr(
                    15, 2, f"Latest report: {latest_report_path}", w - 4, curses.A_DIM
                )
            stdscr.addstr(
                h - 2,
                2,
                "[t] start/stop TX cont.  [r] start/stop RX cont.  [z] zero counters",
                curses.A_DIM,
            )
            stdscr.addnstr(
                h - 1,
                2,
                f"[Esc] back to setup   log: {latest_log_path or 'n/a'}",
                w - 4,
                curses.A_DIM,
            )
            stdscr.refresh()

            k = stdscr.getch()
            if k == -1:
                time.sleep(0.05)
                continue
            try:
                if k in (ord("t"), ord("T")):
                    if backend.mode == "tx":
                        backend.stop()
                        tx_started_at = None
                        msg = "TX stopped"
                        _close_run(msg, None)
                    elif not allow_tx:
                        msg = "TX interlock: return to setup and confirm RF load"
                    else:
                        _open_run("tx")
                        try:
                            backend.start_tx(p)
                        except Exception:
                            _abort_run()
                            raise
                        tx_started_at = time.time()
                        msg = "TX continuous started"
                        last = None
                elif k in (ord("r"), ord("R")):
                    if backend.mode == "rx":
                        final = backend.stop()
                        if final:
                            if active_logger is not None:
                                active_logger.log(p, final, event="final")
                            last = final
                        msg = "RX stopped (final stats logged)"
                        _close_run(msg, final)
                    else:
                        _open_run("rx")
                        try:
                            backend.start_rx(p)
                        except Exception:
                            _abort_run()
                            raise
                        msg = "RX continuous started"
                        last = None
                elif k in (ord("z"), ord("Z")) and backend.mode == "rx":
                    backend.reset_rx_counters()
                    if run is not None:
                        run["samples"].clear()
                    msg = "Counters zeroed"
                elif k == 27:
                    mode_before = backend.mode
                    final = backend.stop()
                    if mode_before == "rx":
                        _close_run("RX stopped (returned to setup)", final)
                    elif mode_before == "tx":
                        _close_run("TX stopped (returned to setup)", None)
                    return
                elif k in (ord("q"), ord("Q")):
                    mode_before = backend.mode
                    final = backend.stop()
                    if mode_before == "rx":
                        _close_run("RX stopped (quit)", final)
                    elif mode_before == "tx":
                        _close_run("TX stopped (quit)", None)
                    raise KeyboardInterrupt
            except BackendError as e:
                msg = f"ERROR: {e}"
    finally:
        if active_logger is not None:
            active_logger.close()
        stdscr.nodelay(False)


def _setup_flow(stdscr, cfg, force_mock: bool) -> None:
    while True:
        i = _menu(stdscr, "Select radio", ["WiFi", "BT (BLE Direct Test Mode)"])
        if i is None:
            return
        radio = "wifi" if i == 0 else "bt"
        p = TestParams(radio=radio)

        if radio == "wifi":
            i = _menu(stdscr, "Select band", list(rates.BANDS))
            if i is None:
                continue
            p.band = rates.BANDS[i]
            chans = sorted(rates.channels_for_band(p.band))
            labels = [f"ch {c}  ({rates.channel_to_freq(p.band, c)} MHz)" for c in chans]
            i = _menu(stdscr, "Select channel", labels)
            if i is None:
                continue
            p.channel = chans[i]
            i = _menu(stdscr, "Select bandwidth", [f"{b} MHz" for b in rates.BANDWIDTHS_MHZ])
            if i is None:
                continue
            p.bandwidth_mhz = rates.BANDWIDTHS_MHZ[i]
            i = _menu(
                stdscr,
                "Modulation & data rate",
                ["MCS (HT / VHT / HE)", "Legacy rate (11b / 11a/g)"],
            )
            if i is None:
                continue
            pool = [
                r
                for r in rates.wifi_rates_for(p.band)
                if (r.family in ("HT", "VHT", "HE")) == (i == 0)
            ]
            j = _menu(stdscr, "Select rate", [r.name for r in pool])
            if j is None:
                continue
            p.rate = pool[j].name
        else:
            p.band = "2.4GHz"
            p.bandwidth_mhz = 1
            ch = _prompt_int(
                stdscr, "BLE RF channel (freq = 2402 + 2*ch MHz)", 0, 39, 19
            )
            if ch is None:
                continue
            p.channel = ch
            kind = cfg["bt"].get("backend", "aic_uart")
            keys = list(rates.BT_PHYS)
            if kind == "aic_uart" and not force_mock:
                keys = list(cfg["bt"].get("validated_phys", ["1M"]))
            i = _menu(stdscr, "Select PHY / data rate", [rates.BT_PHYS[k].name for k in keys])
            if i is None:
                continue
            p.rate = keys[i]

        pw = _prompt_int(
            stdscr, "Transmit power (dBm)", rates.TX_POWER_MIN_DBM, rates.TX_POWER_MAX_DBM, 15
        )
        if pw is None:
            continue
        p.tx_power_dbm = pw

        exp = _prompt_int(
            stdscr,
            "Expected packet count for PER (0 = derive from CRC counters)",
            0,
            10_000_000,
            0,
        )
        p.expected_packets = exp or None

        safety = _menu(
            stdscr,
            "TX safety interlock",
            [
                "RX only (TX disabled)",
                "Antenna or chamber RF cable connected - enable TX",
            ],
        )
        if safety is None:
            continue
        allow_tx = safety == 1 or force_mock

        backend = make_backend(radio, cfg, force_mock=force_mock)
        try:
            backend.open()
            _live_screen(stdscr, backend, p, cfg, None, allow_tx)
        except BackendError as e:
            stdscr.erase()
            stdscr.addstr(2, 2, f"Backend error: {e}", curses.A_BOLD)
            stdscr.addstr(4, 2, "Press any key...")
            stdscr.getch()
        finally:
            backend.close()


def run_tui(cfg, force_mock: bool = False) -> None:
    try:
        curses.wrapper(_setup_flow, cfg, force_mock)
    except KeyboardInterrupt:
        pass
