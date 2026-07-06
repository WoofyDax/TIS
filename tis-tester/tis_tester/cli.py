"""Command-line interface.

  tis-test interactive                     # curses UI (bench use)
  tis-test rx  --radio wifi --band 5GHz --channel 36 --bw 20 \\
               --rate HE-MCS0 --duration 30 --expected 1000
  tis-test tx  --radio wifi --band 2.4GHz --channel 6 --rate 11b-11M \\
               --power 19 --duration 10
  tis-test sweep --radio wifi --band 2.4GHz --channels 1,6,11 \\
               --rates HE-MCS0,HE-MCS7 --dwell 10
  tis-test list                            # show valid channels/rates

Add --mock to any command to run against the simulator.
"""

from __future__ import annotations
import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import rates, __version__
from .backends import make_backend, TestParams, BackendError
from .config import load_config
from .report import finish_run, record_run_sample, start_run, write_sweep_report
from .session import CsvLogger, fmt_stats, run_rx_session, run_tx_session, sweep


def _install_signal_handlers(handler) -> None:
    """Route terminal/SSH termination through normal backend cleanup."""
    seen = set()
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and sig not in seen:
            signal.signal(sig, handler)
            seen.add(sig)


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--radio", choices=["wifi", "bt"], default="wifi")
    sp.add_argument("--band", choices=list(rates.BANDS), default="2.4GHz")
    sp.add_argument("--channel", type=int, default=1,
                    help="Wi-Fi channel, or BLE RF channel 0-39")
    sp.add_argument("--bw", type=int, choices=rates.BANDWIDTHS_MHZ, default=None)
    sp.add_argument("--rate", default=None,
                    help="Wi-Fi rate name (HT-MCS7, 11b-1M, OFDM-6M, HE-MCS11) "
                         "or BT PHY (1M, 2M, coded-s8, coded-s2)")
    sp.add_argument("--power", type=int, default=15,
                    help=f"TX power dBm "
                         f"({rates.TX_POWER_MIN_DBM}-{rates.TX_POWER_MAX_DBM})")
    sp.add_argument("--expected", type=int, default=0,
                    help="Expected packet count from the instrument (PER basis)")
    sp.add_argument("--payload", default="prbs9",
                    choices=list(rates.BT_PAYLOADS), help="BT TX payload")
    sp.add_argument("--payload-len", type=int, default=37)
    sp.add_argument("--mock", action="store_true", help="Use the simulator")


def _params(a) -> TestParams:
    is_bt = a.radio == "bt"
    return TestParams(radio=a.radio, band="2.4GHz" if is_bt else a.band,
                      channel=a.channel,
                      bandwidth_mhz=a.bw if a.bw is not None else (1 if is_bt else 20),
                      rate=a.rate or ("1M" if is_bt else "HE-MCS0"),
                      tx_power_dbm=a.power,
                      payload=a.payload, payload_len=a.payload_len,
                      expected_packets=a.expected or None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tis-test",
                                 description="TIS test controller for the "
                                             "FCS960K-N (AIC8800D80 U02)")
    ap.add_argument("--config", help="Path to tis_config.yaml")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("interactive", help="Curses serial-safe dashboard UI")
    sp.add_argument("--mock", action="store_true")

    sp = sub.add_parser("web", help="Browser dashboard (open from your laptop)")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--mock", action="store_true")

    for name, hlp in (("rx", "Continuous RX with live RSSI/PER/CRC/packet stats"),
                      ("tx", "Continuous TX")):
        sp = sub.add_parser(name, help=hlp)
        _add_common(sp)
        sp.add_argument("--duration", type=float, default=0,
                        help="Seconds (0 = until Ctrl-C)")
        if name == "tx":
            sp.add_argument(
                "--confirm-antenna", action="store_true",
                help="Confirm an antenna or chamber RF cable is connected",
            )

    sp = sub.add_parser("sweep", help="Automated RX sweep (channels x rates)")
    _add_common(sp)
    sp.add_argument("--channels", required=True,
                    help="Comma list, e.g. 1,6,11 or 36,40,44")
    sp.add_argument("--rates", default=None,
                    help="Comma list of rates (default: the --rate value)")
    sp.add_argument("--dwell", type=float, default=10.0,
                    help="Seconds per point")

    sp = sub.add_parser("bt-scan", help="Scan BLE channels/PHYs to find live test traffic")
    sp.add_argument("--channels", default="0-39",
                    help="Range/list, e.g. 0-39 or 19,20,21")
    sp.add_argument("--rates", default=None,
                    help="Comma list of BT PHYs (default: validated PHYs)")
    sp.add_argument("--dwell", type=float, default=1.5,
                    help="Seconds per point")
    sp.add_argument("--expected", type=int, default=0,
                    help="Expected packets if your tester sends fixed bursts")
    sp.add_argument("--mock", action="store_true", help="Use the simulator")

    sub.add_parser("list", help="Print valid channels / rates / PHYs")

    sp = sub.add_parser("diagnose", help="Read-only hardware/mode preflight")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("restore", help="Stop tests and select normal device mode")
    sp.add_argument("--reboot", action="store_true",
                    help="Reboot after selecting mode 0 (guaranteed firmware reload)")

    sp = sub.add_parser("serial", help="Laptop-side COM-port control")
    sp.add_argument("action", choices=["status", "launch", "dashboard", "restore"])
    sp.add_argument("--port", default="COM5")
    sp.add_argument("--baud", type=int, default=1500000)
    sp.add_argument("--mock", action="store_true")
    sp.add_argument("--reboot", action="store_true")
    sp.add_argument("--no-putty", action="store_true")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.cmd in ("interactive", "web", "bt-scan"):
        def interrupt_cleanup(*_):
            raise KeyboardInterrupt
        _install_signal_handlers(interrupt_cleanup)

    if a.cmd == "diagnose":
        w = cfg["wifi"]
        selector = Path(w.get("mode_selector") or "")
        report = {
            "config": cfg["_config_path"],
            "wifi_interface": w["interface"],
            "wifi_exists": Path(f"/sys/class/net/{w['interface']}").exists(),
            "bt_exists": Path("/sys/class/bluetooth/hci0").exists(),
            "mode_selector": str(selector),
            "mode": selector.read_text().strip() if selector.is_file() else "unavailable",
        }
        if a.json:
            print(json.dumps(report, indent=2))
        else:
            for key, value in report.items():
                print(f"{key}: {value}")
        return 0

    if a.cmd == "restore":
        from .recovery import restore_normal
        report = restore_normal(cfg, reboot=a.reboot)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 2

    if a.cmd == "serial":
        from . import serial_console
        try:
            if a.action == "status":
                print(serial_console.status(a.port, a.baud))
            elif a.action == "restore":
                print(serial_console.restore(a.port, a.baud, reboot=a.reboot))
            else:
                serial_console.launch(a.port, a.baud, mock=a.mock,
                                      open_putty=not a.no_putty)
        except BackendError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if a.cmd == "list":
        print("Bands:", ", ".join(rates.BANDS))
        for b in rates.BANDS:
            print(f"  {b} channels:", ", ".join(map(str, sorted(rates.channels_for_band(b)))))
        print("Bandwidths:", ", ".join(f"{x} MHz" for x in rates.BANDWIDTHS_MHZ))
        print("Wi-Fi rates:")
        for fam in ("legacy-b", "legacy-ag", "HT", "VHT", "HE"):
            names = [r.name for r in rates.ALL_WIFI_RATES if r.family == fam]
            print(f"  {fam:>9}: {', '.join(names)}")
        print("BT PHYs:", ", ".join(f"{k} ({v.name})" for k, v in rates.BT_PHYS.items()))
        print(f"TX power: {rates.TX_POWER_MIN_DBM}-{rates.TX_POWER_MAX_DBM} dBm")
        print("Config:", cfg["_config_path"])
        print("Results dir:", cfg["general"]["results_dir"])
        return 0

    if a.cmd == "interactive":
        from .tui import run_tui
        run_tui(cfg, force_mock=a.mock)
        return 0

    if a.cmd == "web":
        from .webui import serve
        serve(cfg, host=a.host, port=a.port, force_mock=a.mock)
        return 0

    if a.cmd == "bt-scan":
        def parse_channels(spec: str) -> list[int]:
            spec = spec.strip()
            if "-" in spec and "," not in spec:
                lo, hi = spec.split("-", 1)
                return list(range(int(lo), int(hi) + 1))
            return [int(x) for x in spec.split(",") if x.strip()]

        channels = parse_channels(a.channels)
        default_phys = (cfg["bt"].get("validated_phys", ["1M"])
                        if cfg["bt"].get("backend") == "aic_uart"
                        else list(rates.BT_PHYS))
        rate_list = ([x.strip() for x in a.rates.split(",") if x.strip()]
                     if a.rates else list(default_phys))
        base = TestParams(
            radio="bt", band="2.4GHz", channel=channels[0], bandwidth_mhz=1,
            rate=rate_list[0], expected_packets=a.expected or None
        )
        backend = make_backend("bt", cfg, force_mock=a.mock)
        try:
            backend.open()
        except BackendError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        logger = CsvLogger(cfg["general"]["results_dir"])
        print(f"# logging to {logger.path}")
        rows = []
        try:
            for rate in rate_list:
                for ch in channels:
                    p = TestParams(
                        radio="bt", band="2.4GHz", channel=ch, bandwidth_mhz=1,
                        rate=rate, expected_packets=a.expected or None
                    )
                    print(f"[scan] bt ch{ch} {rate} dwell {a.dwell}s")
                    freq = 2402 + 2 * ch
                    try:
                        final = run_rx_session(
                            backend, p, a.dwell,
                            float(cfg["general"]["poll_interval_s"]), logger
                        )
                        per_pct = None if final.per is None else final.per * 100.0
                        rows.append({
                            "channel": p.channel,
                            "freq_mhz": freq,
                            "rate": p.rate,
                            "rssi_dbm": final.rssi_dbm,
                            "per_pct": per_pct,
                            "packets_ok": final.packets_ok,
                            "packets_err": final.packets_err,
                            "packets_total": final.packets_total,
                        })
                        print(f"   -> ch{p.channel} {p.rate}: packets={final.packets_total} "
                              f"per={per_pct if per_pct is not None else 'n/a'}")
                    except BackendError as e:
                        rows.append({
                            "channel": p.channel,
                            "freq_mhz": freq,
                            "rate": p.rate,
                            "rssi_dbm": None,
                            "per_pct": None,
                            "packets_ok": 0,
                            "packets_err": 0,
                            "packets_total": 0,
                            "error": str(e),
                        })
                        print(f"   -> ch{p.channel} {p.rate}: unsupported/error: {e}")
            report = write_sweep_report(
                cfg["general"]["results_dir"],
                "tis-test bt-scan",
                "Bluetooth Scan Report",
                rows,
                str(logger.path),
            )
            best = max(rows, key=lambda r: r["packets_total"], default=None)
            if best:
                print(f"# best: ch{best['channel']} {best['rate']} packets={best['packets_total']}")
            print(f"# report: {report}")
        finally:
            backend.close()
            logger.close()
        return 0

    # non-interactive commands ------------------------------------------------
    if a.cmd == "tx" and not a.mock and not a.confirm_antenna:
        print("error: TX is interlocked. Connect the antenna/chamber RF cable "
              "and repeat with --confirm-antenna.", file=sys.stderr)
        return 2
    stop = {"flag": False}
    _install_signal_handlers(lambda *_: stop.__setitem__("flag", True))
    should_stop = lambda: stop["flag"]

    backend = make_backend(a.radio, cfg, force_mock=a.mock)
    try:
        backend.open()
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    poll = float(cfg["general"]["poll_interval_s"])
    logger = None
    report_path = None

    try:
        if a.cmd == "tx":
            p = _params(a)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            logger = CsvLogger(
                cfg["general"]["results_dir"],
                name=f"{p.radio}_tx_{stamp}.csv",
            )
            run = start_run(p, "tx", "tis-test cli", str(logger.path))
            print(f"# logging to {logger.path}")
            print(f"# TX continuous: {p.radio} {p.band} ch{p.channel} "
                  f"{p.rate} bw{p.bandwidth_mhz} {p.tx_power_dbm} dBm "
                  f"(Ctrl-C to stop)")
            max_tx = float(cfg["general"].get("max_tx_duration_s", 300))
            duration = a.duration or max_tx
            if duration > max_tx:
                raise BackendError(f"TX duration exceeds safety cap ({max_tx:g}s)")
            run_tx_session(backend, p, duration, should_stop)
            print("# TX stopped")
            report_path = finish_run(run, cfg["general"]["results_dir"],
                                     "TX stopped", None)

        elif a.cmd == "rx":
            p = _params(a)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            logger = CsvLogger(
                cfg["general"]["results_dir"],
                name=f"{p.radio}_rx_{stamp}.csv",
            )
            run = start_run(p, "rx", "tis-test cli", str(logger.path))
            print(f"# logging to {logger.path}")
            print(f"# RX continuous: {p.radio} {p.band} ch{p.channel} "
                  f"{p.rate} bw{p.bandwidth_mhz} (Ctrl-C to stop)")
            final = run_rx_session(
                backend, p, a.duration or None, poll, logger,
                on_sample=lambda st: (
                    record_run_sample(run, st),
                    print("\r" + fmt_stats(st), end="", flush=True),
                ),
                should_stop=should_stop)
            record_run_sample(run, final, event="final")
            report_path = finish_run(
                run,
                cfg["general"]["results_dir"],
                "RX stopped (final stats logged)",
                final,
            )
            print("\n# final:", fmt_stats(final) if final else "n/a")

        elif a.cmd == "sweep":
            base = _params(a)
            logger = CsvLogger(cfg["general"]["results_dir"])
            print(f"# logging to {logger.path}")
            channels = [int(x) for x in a.channels.split(",") if x.strip()]
            rate_list = ([x.strip() for x in a.rates.split(",")]
                         if a.rates else [a.rate])
            results = []
            for p, final in sweep(backend, base, channels, rate_list,
                                  a.dwell, poll, logger):
                print("   ->", fmt_stats(final) if final else "n/a")
                results.append((p, final))
                if should_stop():
                    break
            print(f"# sweep complete: {len(results)} points, "
                  f"results in {logger.path}")
        if report_path is not None:
            print(f"# report: {report_path}")
    except BackendError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2
    finally:
        backend.close()
        if logger is not None:
            logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
