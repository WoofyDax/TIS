"""Browser dashboard.

Run on the DUT host:   tis-test web [--port 8080] [--mock]
Open from the laptop:  http://<device-ip>:8080

Pure Python stdlib (http.server) + one self-contained HTML page, so it works
on an offline bench machine with no extra packages. The controller wraps a
backend, runs a background poll thread during continuous RX, keeps a rolling
history for the charts, writes a dedicated CSV log per run, and generates
downloadable HTML reports when a run finishes.
"""

from __future__ import annotations
import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import rates
from .backends import make_backend, TestParams, BackendError, RxStats
from .session import CsvLogger
from .report import write_report


# ===========================================================================
# Controller
# ===========================================================================

class Controller:
    HISTORY = 300          # samples kept for charts (~5 min at 1 s)

    def __init__(self, cfg: dict, force_mock: bool = False):
        self.cfg = cfg
        self.force_mock = force_mock
        self.lock = threading.RLock()
        self.params = TestParams()
        self.backend = None
        self.mode: str | None = None
        self.last: RxStats | None = None
        self.history: list[dict] = []
        self.message = "Idle. Set parameters, then start RX or TX."
        self.logger: CsvLogger | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self.tx_armed_until = 0.0
        self._tx_timer: threading.Timer | None = None
        self._active_run: dict | None = None
        self._run_samples: list[dict] = []
        self.last_report_path: str | None = None
        self.last_log_path: str | None = None

    # ------------------------------------------------------------ backend
    def _ensure_backend(self):
        want = self.params.radio
        if self.backend is not None and getattr(self.backend, "_radio", None) == want:
            return
        if self.backend is not None:
            self.backend.close()
        self.backend = make_backend(want, self.cfg, force_mock=self.force_mock)
        self.backend._radio = want
        self.backend.open()

    # ------------------------------------------------------------- params
    def set_params(self, d: dict) -> None:
        with self.lock:
            if self.mode:
                raise BackendError("Stop the running test before changing parameters")
            import copy
            snapshot = copy.deepcopy(self.params)
            try:
                self._apply_params(d)
                self._validate(self.params)
            except (BackendError, ValueError):
                self.params = snapshot
                raise

    def _apply_params(self, d: dict) -> None:
            p = self.params
            if "radio" in d:
                p.radio = str(d["radio"])
                if p.radio == "bt":
                    p.band = "2.4GHz"
                    p.bandwidth_mhz = 1
                    if p.rate not in rates.BT_PHYS:
                        p.rate = "1M"
                    if p.channel not in rates.BLE_CHANNELS:
                        p.channel = 19
                elif p.rate.lower() not in rates.WIFI_RATE_BY_NAME:
                    p.rate = "HE-MCS0"
            if "band" in d and p.radio == "wifi":
                p.band = str(d["band"])
                if p.channel not in rates.channels_for_band(p.band):
                    p.channel = sorted(rates.channels_for_band(p.band))[0]
                r = rates.WIFI_RATE_BY_NAME.get(p.rate.lower())
                if r and p.band not in r.bands:
                    p.rate = "HE-MCS0"
            for k, cast in (("channel", int), ("bandwidth_mhz", int),
                            ("tx_power_dbm", int), ("payload_len", int)):
                if k in d:
                    setattr(p, k, cast(d[k]))
            for k in ("rate", "payload"):
                if k in d:
                    setattr(p, k, str(d[k]))
            if "expected_packets" in d:
                v = int(d["expected_packets"] or 0)
                p.expected_packets = v or None

    def _validate(self, p: TestParams) -> None:
        if p.radio not in ("wifi", "bt"):
            raise BackendError("Radio must be 'wifi' or 'bt'")
        if p.radio == "wifi":
            rates.channel_to_freq(p.band, p.channel)
            if p.bandwidth_mhz not in rates.BANDWIDTHS_MHZ:
                raise BackendError(
                    f"Wi-Fi bandwidth must be one of {rates.BANDWIDTHS_MHZ} MHz"
                )
            r = rates.lookup_wifi_rate(p.rate)
            if p.band not in r.bands:
                raise BackendError(f"{r.name} is not valid on {p.band}")
            if r.family == "legacy-b" and p.bandwidth_mhz != 20:
                raise BackendError("802.11b rates are 20 MHz only")
        else:
            if p.bandwidth_mhz != 1:
                raise BackendError("BLE DTM bandwidth is fixed by the selected PHY")
            if p.channel not in rates.BLE_CHANNELS:
                raise BackendError("BLE RF channel must be 0-39")
            if p.rate not in rates.BT_PHYS:
                raise BackendError(f"BT PHY must be one of {list(rates.BT_PHYS)}")
            if p.payload not in rates.BT_PAYLOADS:
                raise BackendError(f"BT payload must be one of {list(rates.BT_PAYLOADS)}")
            if not (0 <= p.payload_len <= 255):
                raise BackendError("BLE payload length must be 0-255")
        if p.expected_packets is not None and p.expected_packets <= 0:
            raise BackendError("Expected packets must be positive or left blank")
        if not (rates.TX_POWER_MIN_DBM <= p.tx_power_dbm <= rates.TX_POWER_MAX_DBM):
            raise BackendError(f"TX power must be {rates.TX_POWER_MIN_DBM}"
                               f"-{rates.TX_POWER_MAX_DBM} dBm")

    # ------------------------------------------------------------- control
    def start_rx(self) -> None:
        with self.lock:
            if self.mode:
                raise BackendError("Stop the running test before starting RX")
            self._validate(self.params)
            self._ensure_backend()
            self._stop_poll()
            self._begin_run("rx")
            try:
                self.backend.start_rx(self.params)
            except Exception:
                self._discard_run()
                try:
                    self.backend.stop()
                except Exception:
                    pass
                raise
            self.mode = "rx"
            self.last = None
            self.history.clear()
            self.message = "RX continuous running"
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def start_tx(self) -> None:
        with self.lock:
            if self.mode:
                raise BackendError("Stop the running test before starting TX")
            if time.time() > self.tx_armed_until:
                raise BackendError(
                    "TX interlock is not armed. Confirm the antenna/chamber RF cable first."
                )
            self._validate(self.params)
            self._ensure_backend()
            self._stop_poll()
            if self.mode:
                self.backend.stop()
            self._begin_run("tx")
            try:
                self.backend.start_tx(self.params)
            except Exception:
                self._discard_run()
                try:
                    self.backend.stop()
                except Exception:
                    pass
                raise
            self.mode = "tx"
            self.message = "TX continuous running"
            self.tx_armed_until = 0.0
            limit = float(self.cfg["general"].get("max_tx_duration_s", 300))
            self._tx_timer = threading.Timer(limit, self._tx_timeout)
            self._tx_timer.daemon = True
            self._tx_timer.start()

    def arm_tx(self) -> None:
        with self.lock:
            if self.mode:
                raise BackendError("Stop the running test before arming TX")
            self.tx_armed_until = time.time() + 60.0
            self.message = "TX armed for 60 seconds — RF load confirmed"

    def _tx_timeout(self) -> None:
        try:
            self.stop()
        except BackendError:
            return
        with self.lock:
            self.message = "TX auto-stopped at the safety time limit"

    def stop(self) -> None:
        self._stop_poll()
        timer = self._tx_timer
        self._tx_timer = None
        if timer and timer is not threading.current_thread():
            timer.cancel()
        with self.lock:
            if self.backend and self.mode:
                try:
                    final = self.backend.stop()
                except BackendError as e:
                    self.message = f"ERROR: stop failed; host stack restored: {e}"
                    self.mode = None
                    self.backend.close()
                    self.backend = None
                    self._finish_run(self.message, None)
                    raise
                if final is not None:
                    self.last = final
                    self._record(final, event="final")
                    self.message = "RX stopped - final stats logged"
                else:
                    self.message = "TX stopped"
                self._finish_run(self.message, final)
            self.mode = None

    def restore(self) -> None:
        try:
            self.stop()
        except BackendError:
            # stop() already closed the failed backend and restored its host
            # stack; continue with the explicit normal-mode recovery.
            pass
        with self.lock:
            if self.backend:
                self.backend.close()
                self.backend = None
            if self.force_mock:
                self.message = "Mock device restored to normal mode"
                return
            from .recovery import restore_normal
            report = restore_normal(self.cfg, reboot=False)
            self.message = ("Normal mode selected; reboot for guaranteed firmware reload"
                            if report["ok"] else
                            "ERROR: restore incomplete: " + "; ".join(report["errors"]))

    def zero(self) -> None:
        with self.lock:
            if self.backend and self.mode == "rx":
                self.backend.reset_rx_counters()
                self.history.clear()
                self._run_samples.clear()
                self.message = "Counters zeroed"

    def close(self) -> None:
        try:
            self.stop()
        except BackendError:
            pass
        with self.lock:
            self._close_logger()
            if self.backend:
                self.backend.close()
                self.backend = None

    def _stop_poll(self) -> None:
        self._poll_stop.set()
        t = self._poll_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._poll_thread = None

    def _poll_loop(self) -> None:
        interval = float(self.cfg["general"]["poll_interval_s"])
        while not self._poll_stop.wait(interval):
            with self.lock:
                if self.mode != "rx" or not self.backend:
                    return
                try:
                    st = self.backend.poll_rx()
                    self.last = st
                    self._record(st)
                except BackendError as e:
                    self.message = f"ERROR: RX poll failed; test stopped: {e}"
                    failed_backend = self.backend
                    self.mode = None
                    self.backend = None
                    failed_backend.close()
                    try:
                        self._finish_run(self.message, None)
                    except OSError as report_error:
                        self.message += f"; report failed: {report_error}"
                    self._poll_stop.set()
                    return

    def _begin_run(self, mode: str) -> None:
        self._close_logger()
        name = f"{self.params.radio}_{mode}_{datetime_stamp()}.csv"
        self.logger = CsvLogger(self.cfg["general"]["results_dir"], name=name)
        self.last_log_path = str(self.logger.path)
        self._run_samples = []
        self._active_run = {
            "mode": mode,
            "params": TestParams(**asdict(self.params)),
            "started_at": time.time(),
            "generated_at": None,
            "ended_at": None,
            "message": None,
            "samples": self._run_samples,
            "final_stats": None,
            "log_path": self.last_log_path,
            "source": "tis-test web",
        }

    def _finish_run(self, message: str, final: RxStats | None) -> None:
        if not self._active_run:
            return
        self._active_run["ended_at"] = time.time()
        self._active_run["generated_at"] = time.time()
        self._active_run["message"] = message
        self._active_run["final_stats"] = final
        self.last_report_path = str(write_report(self.cfg["general"]["results_dir"], self._active_run))
        self._close_logger()
        self._active_run = None

    def _close_logger(self) -> None:
        if self.logger is not None:
            self.logger.close()
            self.logger = None

    def _discard_run(self) -> None:
        """Drop a prepared run when the radio failed to start."""
        self._close_logger()
        self._active_run = None
        self._run_samples = []

    def _record(self, st: RxStats, event: str = "sample") -> None:
        if self.logger is not None:
            self.logger.log(self.params, st, event=event)
        self._run_samples.append({
            "timestamp": st.timestamp,
            "rssi_dbm": st.rssi_dbm,
            "per": st.per,
            "packets_ok": st.packets_ok,
            "packets_err": st.packets_err,
            "packets_total": st.packets_total,
            "expected_packets": st.expected_packets,
            "event": event,
        })
        self.history.append({
            "t": st.timestamp,
            "rssi": st.rssi_dbm,
            "per": st.per,
            "ok": st.packets_ok, "err": st.packets_err,
            "total": st.packets_total,
        })
        del self.history[:-self.HISTORY]

    # -------------------------------------------------------------- state
    def state(self) -> dict:
        with self.lock:
            st = self.last
            return {
                "mode": self.mode,
                "tx_armed": time.time() <= self.tx_armed_until,
                "message": self.message,
                "params": asdict(self.params),
                "stats": None if st is None else {
                    "rssi": st.rssi_dbm,
                    "per": st.per,
                    "ok": st.packets_ok,
                    "err": st.packets_err,
                    "total": st.packets_total,
                    "expected": st.expected_packets,
                },
                "history": self.history[-self.HISTORY:],
                "log": self.last_log_path,
                "report": None if not self.last_report_path else {
                    "path": self.last_report_path,
                    "download_url": "/api/report/latest",
                },
                "options": self._options(),
            }

    def _options(self) -> dict:
        p = self.params
        if p.radio == "wifi":
            chans = sorted(rates.channels_for_band(p.band))
            pool = rates.wifi_rates_for(p.band)
            groups = {}
            for r in pool:
                key = ("MCS" if r.family in ("HT", "VHT", "HE") else "Legacy")
                groups.setdefault(key, []).append(r.name)
            return {"bands": list(rates.BANDS),
                    "channels": chans,
                    "channel_freqs": {c: rates.channel_to_freq(p.band, c) for c in chans},
                    "bandwidths": list(rates.BANDWIDTHS_MHZ),
                    "rate_groups": groups}
        kind = self.cfg["bt"].get("backend", "aic_uart")
        phys = list(rates.BT_PHYS)
        if kind == "aic_uart" and not self.force_mock:
            phys = list(self.cfg["bt"].get("validated_phys", ["1M"]))
        return {"bands": ["2.4GHz"],
                "channels": list(range(40)),
                "channel_freqs": {c: 2402 + 2 * c for c in range(40)},
                "bandwidths": [1],
                "rate_groups": {"PHY": phys},
                "payloads": list(rates.BT_PAYLOADS)}


# ===========================================================================
# HTTP server
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    ctrl: Controller = None      # injected

    def log_message(self, *a):   # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(self.ctrl.state())
        elif self.path == "/api/report/latest":
            path = self.ctrl.last_report_path
            if not path:
                self._send(404, b"no report available", "text/plain")
                return
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send(404, b"report file is unavailable", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{Path(path).name}\"",
            )
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 65536:
            return self._json({"ok": False, "error": "request body too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"ok": False, "error": "JSON object required"}, 400)
        try:
            if self.path == "/api/params":
                self.ctrl.set_params(body)
            elif self.path == "/api/control":
                action = body.get("action")
                {"start_rx": self.ctrl.start_rx,
                 "start_tx": self.ctrl.start_tx,
                 "arm_tx": self.ctrl.arm_tx,
                 "stop": self.ctrl.stop,
                 "zero": self.ctrl.zero,
                 "restore": self.ctrl.restore}[action]()
            else:
                return self._send(404, b"not found", "text/plain")
            self._json({"ok": True, "state": self.ctrl.state()})
        except KeyError:
            self._json({"ok": False, "error": "unknown action"}, 400)
        except (BackendError, ValueError) as e:
            self.ctrl.message = f"ERROR: {e}"
            self._json({"ok": False, "error": str(e), "state": self.ctrl.state()}, 400)


def serve(cfg: dict, host: str = "0.0.0.0", port: int = 8080,
          force_mock: bool = False) -> None:
    ctrl = Controller(cfg, force_mock=force_mock)
    Handler.ctrl = ctrl
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"# dashboard:  http://{host}:{port}   (device IP from `ip addr`)")
    print(f"# results:    {cfg['general']['results_dir']}")
    print("# Ctrl-C to quit")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.close()
        httpd.server_close()


def datetime_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


# ===========================================================================
# Front panel page (self-contained: no CDN, works offline in the chamber)
# ===========================================================================

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TIS bench — FCS960K-N</title>
<style>
:root{
  --bezel:#20242b; --panel:#171a20; --well:#0c0e12; --line:#333a45;
  --ink:#c7ced9; --dim:#6d7684; --phos:#ffb454; --phos-dim:#8a5f2a;
  --ok:#7ad48a; --err:#e5655e; --accent:#5aa7d6;
  --mono:ui-monospace,'Cascadia Mono','JetBrains Mono',Menlo,Consolas,monospace;
  --sans:system-ui,'Segoe UI',Roboto,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bezel);color:var(--ink);font:14px/1.45 var(--sans);
     min-height:100vh;padding:18px}
.rig{max-width:1060px;margin:0 auto}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
header h1{font:600 17px var(--sans);letter-spacing:.14em;text-transform:uppercase;margin:0}
header .sub{color:var(--dim);font:12px var(--mono)}
.lamp{display:inline-flex;align-items:center;gap:7px;margin-left:auto;
      font:12px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.lamp i{width:11px;height:11px;border-radius:50%;background:#3a3f48;
        box-shadow:inset 0 0 3px #000}
.lamp.rx i{background:var(--ok);box-shadow:0 0 9px var(--ok)}
.lamp.tx i{background:var(--err);box-shadow:0 0 9px var(--err)}
.grid{display:grid;grid-template-columns:330px 1fr;gap:14px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}
.panel h2{margin:0 0 10px;font:600 11px var(--sans);letter-spacing:.16em;
          text-transform:uppercase;color:var(--dim)}
label{display:block;font:11px var(--sans);letter-spacing:.08em;text-transform:uppercase;
      color:var(--dim);margin:10px 0 4px}
select,input{width:100%;background:var(--well);color:var(--ink);border:1px solid var(--line);
      border-radius:4px;padding:7px 8px;font:13px var(--mono)}
select:focus,input:focus,button:focus{outline:2px solid var(--accent);outline-offset:1px}
.seg{display:flex;gap:6px}
.seg button{flex:1;background:var(--well);border:1px solid var(--line);color:var(--dim);
      border-radius:4px;padding:8px 0;font:600 13px var(--sans);cursor:pointer}
.seg button.on{color:var(--phos);border-color:var(--phos-dim);
      box-shadow:inset 0 0 0 1px var(--phos-dim)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ctl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}
.ctl button{padding:12px 0;border-radius:4px;border:1px solid var(--line);cursor:pointer;
      font:600 13px var(--sans);letter-spacing:.05em;background:var(--well);color:var(--ink)}
.ctl .rx.live{background:#12301b;border-color:var(--ok);color:var(--ok)}
.ctl .tx.live{background:#33191a;border-color:var(--err);color:var(--err)}
.ctl .wide{grid-column:1/-1}
button:disabled{opacity:.4;cursor:default}
/* readouts */
.readouts{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:560px){.readouts{grid-template-columns:repeat(2,1fr)}}
.ro{background:var(--well);border:1px solid var(--line);border-radius:5px;padding:10px 12px}
.ro .k{font:10px var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.ro .v{font:600 26px/1.25 var(--mono);color:var(--phos);
       text-shadow:0 0 12px rgba(255,180,84,.25);white-space:nowrap}
.ro .u{font:12px var(--mono);color:var(--phos-dim);margin-left:3px}
.ro .s{font:11px var(--mono);color:var(--dim)}
.ro.per.bad .v{color:var(--err);text-shadow:0 0 12px rgba(229,101,94,.3)}
.ro.per.good .v{color:var(--ok);text-shadow:0 0 12px rgba(122,212,138,.3)}
canvas{width:100%;height:120px;display:block;background:var(--well);
       border:1px solid var(--line);border-radius:5px;margin-top:10px}
.legend{display:flex;gap:16px;font:11px var(--mono);color:var(--dim);margin-top:6px}
.legend b{font-weight:600}
.legend .per b{color:var(--accent)} .legend .rssi b{color:var(--phos)}
.msg{margin-top:12px;font:12px var(--mono);color:var(--dim);min-height:1.4em}
.msg.err{color:var(--err)}
footer{margin-top:12px;font:11px var(--mono);color:var(--dim);word-break:break-all}
@media(prefers-reduced-motion:no-preference){.ro .v{transition:color .2s}}
</style></head><body>
<div class="rig">
<header>
  <h1>TIS bench</h1>
  <span class="sub">FCS960K-N · AIC8800D80 U02 · receiver-mode sensitivity</span>
  <span class="lamp" id="lamp"><i></i><span id="lampTxt">idle</span></span>
</header>

<div class="grid">
  <!-- ===================== setup panel ===================== -->
  <section class="panel" aria-label="Test setup">
    <h2>Setup</h2>
    <label>Radio</label>
    <div class="seg" id="radioSeg">
      <button data-v="wifi">Wi-Fi</button><button data-v="bt">BT (DTM)</button>
    </div>
    <div class="row2">
      <div><label>Band</label><select id="band"></select></div>
      <div><label>Bandwidth</label><select id="bw"></select></div>
    </div>
    <label>Channel</label><select id="channel"></select>
    <label>Modulation &amp; data rate</label><select id="rate"></select>
    <div class="row2">
      <div><label>TX power (0–23 dBm)</label>
        <input id="power" type="number" min="0" max="23" step="1"></div>
      <div><label>Expected packets (PER)</label>
        <input id="expected" type="number" min="0" step="1" placeholder="0 = from CRC"></div>
    </div>
    <div class="row2" id="btExtras" hidden>
      <div><label>TX payload</label><select id="payload"></select></div>
      <div><label>Payload length</label>
        <input id="plen" type="number" min="0" max="255"></div>
    </div>
    <div class="ctl">
      <button class="rx" id="btnRx">Start RX continuous</button>
      <button class="tx" id="btnTx">Start TX continuous</button>
      <button class="wide" id="btnZero" disabled>Zero counters</button>
      <button class="wide" id="btnReport" disabled>Download latest report</button>
      <button class="wide" id="btnRestore">Restore normal mode</button>
    </div>
    <div class="msg" id="msg"></div>
  </section>

  <!-- ===================== display panel ===================== -->
  <section class="panel" aria-label="Live measurements">
    <h2>Display</h2>
    <div class="readouts">
      <div class="ro"><div class="k">RSSI</div>
        <div class="v" id="rssi">——</div><div class="s">dBm</div></div>
      <div class="ro per" id="perBox"><div class="k">PER</div>
        <div class="v" id="per">——</div><div class="s" id="perBasis">%</div></div>
      <div class="ro"><div class="k">CRC statistic</div>
        <div class="v" id="crc" style="font-size:19px">——</div>
        <div class="s">ok / err</div></div>
      <div class="ro"><div class="k">Packet count</div>
        <div class="v" id="pkts">——</div><div class="s" id="pktRate">&nbsp;</div></div>
    </div>
    <canvas id="chart" width="700" height="120" role="img"
            aria-label="PER and RSSI history"></canvas>
    <div class="legend">
      <span class="per"><b>▬</b> PER %</span>
      <span class="rssi"><b>▬</b> RSSI dBm</span>
      <span id="cfgline"></span>
    </div>
  </section>
</div>
<footer id="foot"></footer>
</div>

<script>
const $=id=>document.getElementById(id);
let S=null, busy=false;

async function api(path, body){
  const r=await fetch(path,{method:body?'POST':'GET',
    headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined});
  return r.json();
}

function opt(sel, values, labels, current){
  sel.innerHTML='';
  values.forEach((v,i)=>{
    const o=document.createElement('option');
    o.value=v; o.textContent=labels?labels[i]:v;
    if(String(v)===String(current)) o.selected=true;
    sel.appendChild(o);
  });
}

function render(st){
  S=st;
  const p=st.params, o=st.options, running=!!st.mode;

  // lamp
  const lamp=$('lamp');
  lamp.className='lamp '+(st.mode||'');
  $('lampTxt').textContent=st.mode==='rx'?'rx continuous':
                            st.mode==='tx'?'tx continuous':'idle';

  // setup controls
  document.querySelectorAll('#radioSeg button').forEach(b=>{
    b.classList.toggle('on', b.dataset.v===p.radio);
    b.disabled=running;
  });
  opt($('band'), o.bands, null, p.band);
  opt($('channel'), o.channels,
      o.channels.map(c=>`ch ${c}  (${o.channel_freqs[c]} MHz)`), p.channel);
  opt($('bw'), o.bandwidths, o.bandwidths.map(b=>b+' MHz'), p.bandwidth_mhz);
  const rsel=$('rate'); rsel.innerHTML='';
  for(const [g,names] of Object.entries(o.rate_groups)){
    const og=document.createElement('optgroup'); og.label=g;
    names.forEach(n=>{const x=document.createElement('option');
      x.value=n;x.textContent=n;if(n===p.rate)x.selected=true;og.appendChild(x);});
    rsel.appendChild(og);
  }
  if(document.activeElement!==$('power')) $('power').value=p.tx_power_dbm;
  if(document.activeElement!==$('expected'))
      $('expected').value=p.expected_packets??'';
  const isBt=p.radio==='bt';
  $('btExtras').hidden=!isBt;
  $('band').disabled=running||isBt; $('bw').disabled=running||isBt;
  ['channel','rate','power','expected'].forEach(id=>$(id).disabled=running);
  if(isBt&&o.payloads){opt($('payload'),o.payloads,null,p.payload);
    if(document.activeElement!==$('plen'))$('plen').value=p.payload_len;}
  $('payload').disabled=running; $('plen').disabled=running;

  // buttons
  $('btnRx').textContent=st.mode==='rx'?'Stop RX continuous':'Start RX continuous';
  $('btnRx').classList.toggle('live',st.mode==='rx');
  $('btnTx').textContent=st.mode==='tx'?'Stop TX continuous':'Start TX continuous';
  $('btnTx').classList.toggle('live',st.mode==='tx');
  $('btnRx').disabled=st.mode==='tx'; $('btnTx').disabled=st.mode==='rx';
  $('btnZero').disabled=st.mode!=='rx';
  $('btnReport').disabled=!st.report;
  $('btnRestore').disabled=running;

  // readouts
  const s=st.stats;
  $('rssi').textContent=s&&s.rssi!=null?s.rssi.toFixed(1):'——';
  const perBox=$('perBox');
  if(s&&s.per!=null){
    const pct=s.per*100;
    $('per').textContent=pct.toFixed(2);
    perBox.classList.toggle('bad',pct>10);
    perBox.classList.toggle('good',pct<=10);
    $('perBasis').textContent=s.expected?`% of ${s.expected} expected`:'% of CRC total';
  }else{$('per').textContent='——';perBox.className='ro per';
        $('perBasis').textContent='%';}
  $('crc').textContent=s?`${s.ok} / ${s.err==null?'n/a':s.err}`:'——';
  $('pkts').textContent=s?s.total:'——';
  const h=st.history;
  if(h.length>=2){
    const a=h[h.length-2],b=h[h.length-1],dt=b.t-a.t;
    $('pktRate').textContent=dt>0?((b.total-a.total)/dt).toFixed(0)+' pkt/s':' ';
  }else $('pktRate').innerHTML='&nbsp;';

  $('cfgline').textContent=
    `${p.radio.toUpperCase()} ${p.band} ch${p.channel} ${p.rate} `+
    (isBt?'':`bw${p.bandwidth_mhz} `)+`${p.tx_power_dbm} dBm`;
  const m=$('msg'); m.textContent=st.message||'';
  m.classList.toggle('err',(st.message||'').startsWith('ERROR'));
  $('foot').textContent='log: '+(st.log||'n/a');
  chart(h);
}

function chart(h){
  const c=$('chart'),x=c.getContext('2d'),W=c.width,H=c.height;
  x.clearRect(0,0,W,H);
  x.strokeStyle='#262b33';x.lineWidth=1;
  for(let i=1;i<4;i++){x.beginPath();x.moveTo(0,H*i/4);x.lineTo(W,H*i/4);x.stroke();}
  if(h.length<2)return;
  const n=h.length, X=i=>i*(W-8)/(n-1)+4;
  // PER 0..100%
  x.strokeStyle='#5aa7d6';x.lineWidth=1.6;x.beginPath();
  h.forEach((s,i)=>{const v=s.per==null?0:Math.min(1,s.per);
    const y=H-4-v*(H-8);i?x.lineTo(X(i),y):x.moveTo(X(i),y);});
  x.stroke();
  // RSSI mapped -100..-30 dBm
  x.strokeStyle='#ffb454';x.lineWidth=1.2;x.beginPath();let started=false;
  h.forEach((s,i)=>{if(s.rssi==null)return;
    const t=Math.max(0,Math.min(1,(s.rssi+100)/70));
    const y=H-4-t*(H-8);
    started?x.lineTo(X(i),y):x.moveTo(X(i),y);started=true;});
  if(started)x.stroke();
}

async function send(path,body){
  if(busy)return; busy=true;
  try{const r=await api(path,body); if(r.state)render(r.state);
      if(!r.ok&&r.error){$('msg').textContent='ERROR: '+r.error;$('msg').classList.add('err');}}
  finally{busy=false;}
}

// wire up
document.querySelectorAll('#radioSeg button').forEach(b=>
  b.onclick=()=>send('/api/params',{radio:b.dataset.v}));
$('band').onchange=e=>send('/api/params',{band:e.target.value});
$('channel').onchange=e=>send('/api/params',{channel:+e.target.value});
$('bw').onchange=e=>send('/api/params',{bandwidth_mhz:+e.target.value});
$('rate').onchange=e=>send('/api/params',{rate:e.target.value});
$('power').onchange=e=>send('/api/params',{tx_power_dbm:+e.target.value});
$('expected').onchange=e=>send('/api/params',{expected_packets:+(e.target.value||0)});
$('payload').onchange=e=>send('/api/params',{payload:e.target.value});
$('plen').onchange=e=>send('/api/params',{payload_len:+e.target.value});
$('btnRx').onclick=()=>send('/api/control',{action:S.mode==='rx'?'stop':'start_rx'});
$('btnTx').onclick=async()=>{
  if(S.mode==='tx'){await send('/api/control',{action:'stop'});return;}
  if(!confirm('Confirm an antenna or chamber RF cable is connected before TX.'))return;
  await send('/api/control',{action:'arm_tx'});
  await send('/api/control',{action:'start_tx'});
};
$('btnZero').onclick=()=>send('/api/control',{action:'zero'});
$('btnReport').onclick=()=>{ if(S.report) window.location=S.report.download_url; };
$('btnRestore').onclick=()=>send('/api/control',{action:'restore'});

async function tick(){
  try{render(await api('/api/state'));}catch(e){
    $('msg').textContent='Connection lost — retrying…';
    $('msg').classList.add('err');}
}
tick(); setInterval(tick,1000);
</script>
</body></html>
"""
