"""Per-run HTML report generation for completed TIS tests."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import mean

from .backends import RxStats, TestParams


def _stamp(ts: float | None) -> str:
    if not ts:
        return "n/a"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _duration_s(started_at: float | None, ended_at: float | None) -> str:
    if not started_at or not ended_at or ended_at < started_at:
        return "n/a"
    return f"{ended_at - started_at:.1f} s"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "test"


def _fmt_num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def start_run(params: TestParams, mode: str, source: str,
              log_path: str | None = None) -> dict:
    return {
        "mode": mode,
        "params": TestParams(**asdict(params)),
        "started_at": time.time(),
        "generated_at": None,
        "ended_at": None,
        "message": None,
        "samples": [],
        "final_stats": None,
        "log_path": log_path,
        "source": source,
    }


def record_run_sample(run: dict, st: RxStats, event: str = "sample") -> None:
    run["samples"].append({
        "timestamp": st.timestamp,
        "rssi_dbm": st.rssi_dbm,
        "per": st.per,
        "packets_ok": st.packets_ok,
        "packets_err": st.packets_err,
        "packets_total": st.packets_total,
        "expected_packets": st.expected_packets,
        "event": event,
    })


def finish_run(run: dict, results_dir: str | Path, message: str,
               final_stats: RxStats | None) -> Path:
    run["ended_at"] = time.time()
    run["generated_at"] = time.time()
    run["message"] = message
    run["final_stats"] = final_stats
    return write_report(results_dir, run)


def _rx_summary(samples: list[dict], final_stats: RxStats | None) -> dict:
    per_values = [s["per"] for s in samples if s.get("per") is not None]
    rssi_values = [s["rssi_dbm"] for s in samples if s.get("rssi_dbm") is not None]
    source = final_stats
    if source is None and samples:
        last = samples[-1]
        source = RxStats(
            packets_ok=last.get("packets_ok", 0),
            packets_err=last.get("packets_err", 0),
            rssi_dbm=last.get("rssi_dbm"),
            expected_packets=last.get("expected_packets"),
            timestamp=last.get("timestamp", 0.0),
        )
    return {
        "sample_count": len(samples),
        "final_rssi_dbm": None if source is None else source.rssi_dbm,
        "final_per_pct": None if source is None or source.per is None else source.per * 100.0,
        "packets_ok": None if source is None else source.packets_ok,
        "packets_err": None if source is None else source.packets_err,
        "packets_total": None if source is None else source.packets_total,
        "expected_packets": None if source is None else source.expected_packets,
        "avg_per_pct": None if not per_values else mean(per_values) * 100.0,
        "max_per_pct": None if not per_values else max(per_values) * 100.0,
        "avg_rssi_dbm": None if not rssi_values else mean(rssi_values),
        "min_rssi_dbm": None if not rssi_values else min(rssi_values),
        "max_rssi_dbm": None if not rssi_values else max(rssi_values),
    }


def _chart_points(values: list[float | None], floor: float, ceiling: float) -> str:
    points: list[str] = []
    if not values:
        return ""
    span = max(1.0, ceiling - floor)
    width = 860.0
    height = 170.0
    step = width / max(1, len(values) - 1)
    for idx, value in enumerate(values):
        if value is None:
            continue
        clipped = max(floor, min(ceiling, value))
        norm = (clipped - floor) / span
        x = idx * step
        y = height - norm * height
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def render_report(run: dict) -> str:
    params: TestParams = run["params"]
    samples: list[dict] = list(run.get("samples") or [])
    final_stats: RxStats | None = run.get("final_stats")
    summary = _rx_summary(samples, final_stats)
    params_json = json.dumps(asdict(params), indent=2)
    samples_tail = samples[-25:]
    per_points = _chart_points([None if s.get("per") is None else s["per"] * 100.0 for s in samples],
                               0.0, 100.0)
    rssi_points = _chart_points([s.get("rssi_dbm") for s in samples], -100.0, -20.0)
    title = (f"TIS Report - {params.radio.upper()} {run['mode'].upper()} "
             f"ch{params.channel} {params.rate}")

    rows = []
    for sample in samples_tail:
        rows.append(
            "<tr>"
            f"<td>{html.escape(_stamp(sample.get('timestamp')))}</td>"
            f"<td>{html.escape(_fmt_num(sample.get('rssi_dbm')))}</td>"
            f"<td>{html.escape(_fmt_num(None if sample.get('per') is None else sample['per'] * 100.0, 2))}</td>"
            f"<td>{html.escape(str(sample.get('packets_ok', 'n/a')))}</td>"
            f"<td>{html.escape(_fmt_num(sample.get('packets_err'), 0))}</td>"
            f"<td>{html.escape(str(sample.get('packets_total', 'n/a')))}</td>"
            f"<td>{html.escape(str(sample.get('event', 'sample')))}</td>"
            "</tr>"
        )
    sample_rows = "\n".join(rows) or (
        "<tr><td colspan=\"7\" class=\"empty\">No live RX samples were captured for this run.</td></tr>"
    )
    chart = ""
    if samples:
        chart = f"""
<section class="panel">
  <h2>Trend Snapshot</h2>
  <svg viewBox="0 0 860 170" role="img" aria-label="PER and RSSI trend">
    <rect x="0" y="0" width="860" height="170" fill="#fbf7f2" rx="10"></rect>
    <g stroke="#e8ddd1" stroke-width="1">
      <line x1="0" y1="42.5" x2="860" y2="42.5"></line>
      <line x1="0" y1="85" x2="860" y2="85"></line>
      <line x1="0" y1="127.5" x2="860" y2="127.5"></line>
    </g>
    <polyline fill="none" stroke="#d47b2c" stroke-width="3" points="{html.escape(per_points)}"></polyline>
    <polyline fill="none" stroke="#2f6d8f" stroke-width="3" points="{html.escape(rssi_points)}"></polyline>
  </svg>
  <div class="legend">
    <span><i class="per"></i> PER %</span>
    <span><i class="rssi"></i> RSSI dBm</span>
  </div>
</section>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --paper:#f4efe7; --ink:#1c2630; --muted:#5e6a75; --line:#d7cbbd;
  --panel:#fffdf8; --accent:#2f6d8f; --warm:#d47b2c; --ok:#2d7a4a;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:15px/1.5 "Segoe UI", Arial, sans-serif; color:var(--ink); background:linear-gradient(180deg, #f0ebe2 0%, #ebe3d5 100%); }}
.page {{ max-width:980px; margin:0 auto; padding:28px 20px 40px; }}
header {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:22px 24px; box-shadow:0 10px 28px rgba(56,46,35,.08); }}
h1 {{ margin:0 0 6px; font-size:28px; letter-spacing:.02em; }}
.sub {{ color:var(--muted); font-size:14px; }}
.stamp {{ margin-top:10px; font-size:13px; color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:14px; margin-top:18px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:16px 18px; box-shadow:0 8px 24px rgba(56,46,35,.06); }}
.k {{ color:var(--muted); font-size:11px; letter-spacing:.12em; text-transform:uppercase; }}
.v {{ margin-top:8px; font-size:28px; font-weight:700; }}
.panel {{ margin-top:18px; background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:20px 22px; box-shadow:0 8px 24px rgba(56,46,35,.06); }}
.panel h2 {{ margin:0 0 12px; font-size:16px; }}
dl {{ display:grid; grid-template-columns:220px 1fr; gap:10px 16px; margin:0; }}
dt {{ color:var(--muted); font-weight:600; }}
dd {{ margin:0; word-break:break-word; }}
pre {{ margin:0; padding:16px; border-radius:12px; background:#f6f1e8; border:1px solid #e3d7c7; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #eadfce; }}
th {{ color:var(--muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase; }}
.empty {{ color:var(--muted); text-align:center; padding:20px 8px; }}
svg {{ width:100%; height:auto; border-radius:10px; border:1px solid #eadfce; }}
.legend {{ display:flex; gap:18px; margin-top:10px; color:var(--muted); font-size:13px; }}
.legend i {{ display:inline-block; width:18px; height:3px; margin-right:7px; vertical-align:middle; }}
.legend i.per {{ background:var(--warm); }}
.legend i.rssi {{ background:var(--accent); }}
@media print {{
  body {{ background:#fff; }}
  .page {{ padding:0; }}
  header, .card, .panel {{ box-shadow:none; }}
}}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>TIS Test Report</h1>
    <div class="sub">FCS960K-N / AIC8800D80 U02 bench run summary</div>
    <div class="stamp">Generated {_stamp(run.get("generated_at"))} from {html.escape(run.get("source", "tis-tester"))}</div>
  </header>

  <section class="grid">
    <div class="card"><div class="k">Mode</div><div class="v">{html.escape(run["mode"].upper())}</div></div>
    <div class="card"><div class="k">Radio</div><div class="v">{html.escape(params.radio.upper())}</div></div>
    <div class="card"><div class="k">Duration</div><div class="v">{html.escape(_duration_s(run.get("started_at"), run.get("ended_at")))}</div></div>
    <div class="card"><div class="k">Samples</div><div class="v">{html.escape(str(summary["sample_count"]))}</div></div>
  </section>

  <section class="panel">
    <h2>Test Configuration</h2>
    <dl>
      <dt>Started</dt><dd>{html.escape(_stamp(run.get("started_at")))}</dd>
      <dt>Finished</dt><dd>{html.escape(_stamp(run.get("ended_at")))}</dd>
      <dt>Band</dt><dd>{html.escape(params.band)}</dd>
      <dt>Channel</dt><dd>{html.escape(str(params.channel))}</dd>
      <dt>Bandwidth</dt><dd>{html.escape(str(params.bandwidth_mhz))} MHz</dd>
      <dt>Modulation / Rate</dt><dd>{html.escape(params.rate)}</dd>
      <dt>TX Power</dt><dd>{html.escape(str(params.tx_power_dbm))} dBm</dd>
      <dt>Expected Packets</dt><dd>{html.escape(str(params.expected_packets or "n/a"))}</dd>
      <dt>Raw CSV Log</dt><dd>{html.escape(run.get("log_path") or "n/a")}</dd>
      <dt>Status</dt><dd>{html.escape(run.get("message") or "Completed")}</dd>
    </dl>
  </section>

  <section class="grid">
    <div class="card"><div class="k">Final RSSI</div><div class="v">{html.escape(_fmt_num(summary["final_rssi_dbm"]))}</div></div>
    <div class="card"><div class="k">Final PER</div><div class="v">{html.escape(_fmt_num(summary["final_per_pct"], 2))}</div></div>
    <div class="card"><div class="k">Packets OK / Err</div><div class="v">{html.escape(f"{summary['packets_ok'] if summary['packets_ok'] is not None else 'n/a'} / {summary['packets_err'] if summary['packets_err'] is not None else 'n/a'}")}</div></div>
    <div class="card"><div class="k">Total Packets</div><div class="v">{html.escape(str(summary["packets_total"] if summary["packets_total"] is not None else "n/a"))}</div></div>
  </section>

  <section class="grid">
    <div class="card"><div class="k">Average RSSI</div><div class="v">{html.escape(_fmt_num(summary["avg_rssi_dbm"]))}</div></div>
    <div class="card"><div class="k">RSSI Range</div><div class="v">{html.escape(f"{_fmt_num(summary['min_rssi_dbm'])} to {_fmt_num(summary['max_rssi_dbm'])}")}</div></div>
    <div class="card"><div class="k">Average PER</div><div class="v">{html.escape(_fmt_num(summary["avg_per_pct"], 2))}</div></div>
    <div class="card"><div class="k">Worst PER</div><div class="v">{html.escape(_fmt_num(summary["max_per_pct"], 2))}</div></div>
  </section>

  {chart}

  <section class="panel">
    <h2>Recent Samples</h2>
    <table>
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>RSSI dBm</th>
          <th>PER %</th>
          <th>CRC OK</th>
          <th>CRC Err</th>
          <th>Packets</th>
          <th>Event</th>
        </tr>
      </thead>
      <tbody>
        {sample_rows}
      </tbody>
    </table>
  </section>

  <section class="panel">
    <h2>Parameter Snapshot</h2>
    <pre>{html.escape(params_json)}</pre>
  </section>
</div>
</body>
</html>
"""


def write_report(results_dir: str | Path, run: dict) -> Path:
    root = Path(results_dir) / "reports"
    root.mkdir(parents=True, exist_ok=True)
    params: TestParams = run["params"]
    started = datetime.fromtimestamp(run.get("started_at") or datetime.now().timestamp())
    stamp = started.strftime("%Y%m%d_%H%M%S")
    name = (
        f"tis_report_{_slug(params.radio)}_{_slug(run['mode'])}_"
        f"ch{params.channel}_{_slug(params.rate)}_{stamp}.html"
    )
    path = root / name
    path.write_text(render_report(run), encoding="utf-8")
    return path


def write_sweep_report(results_dir: str | Path, source: str, title: str,
                       rows: list[dict], log_path: str | None = None) -> Path:
    root = Path(results_dir) / "reports"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = root / f"{_slug(title)}_{stamp}.html"
    def rank(row: dict) -> tuple[int, float]:
        per = row.get("per_pct")
        # More packets is better; for equal packet counts, lower PER is
        # better.  Treat 0.0 as a real (best) value rather than as missing.
        return int(row.get("packets_total", 0)), -(float(per) if per is not None else float("inf"))

    ordered = sorted(rows, key=rank, reverse=True)
    best = ordered[:10]
    body_rows = []
    for row in ordered:
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['channel']))}</td>"
            f"<td>{html.escape(str(row['freq_mhz']))}</td>"
            f"<td>{html.escape(str(row['rate']))}</td>"
            f"<td>{html.escape(_fmt_num(row.get('rssi_dbm')))}</td>"
            f"<td>{html.escape(_fmt_num(row.get('per_pct'), 2))}</td>"
            f"<td>{html.escape(str(row.get('packets_ok', 0)))}</td>"
            f"<td>{html.escape(_fmt_num(row.get('packets_err'), 0))}</td>"
            f"<td>{html.escape(str(row.get('packets_total', 0)))}</td>"
            "</tr>"
        )
    top_rows = []
    for row in best:
        top_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['channel']))}</td>"
            f"<td>{html.escape(str(row['freq_mhz']))}</td>"
            f"<td>{html.escape(str(row['rate']))}</td>"
            f"<td>{html.escape(str(row.get('packets_total', 0)))}</td>"
            f"<td>{html.escape(_fmt_num(row.get('per_pct'), 2))}</td>"
            "</tr>"
        )
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font: 15px/1.5 "Segoe UI", Arial, sans-serif; margin: 0; background: #f0ebe2; color: #1c2630; }}
.page {{ max-width: 1040px; margin: 0 auto; padding: 24px; }}
.panel {{ background: #fffdf8; border: 1px solid #d7cbbd; border-radius: 18px; padding: 20px 22px; margin-top: 18px; }}
h1,h2 {{ margin: 0 0 10px; }}
.sub {{ color: #5e6a75; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eadfce; }}
th {{ color: #5e6a75; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
</style>
</head>
<body>
<div class="page">
  <div class="panel">
    <h1>{html.escape(title)}</h1>
    <div class="sub">Generated {_stamp(time.time())} from {html.escape(source)}</div>
    <div class="sub">Raw CSV log: {html.escape(log_path or "n/a")}</div>
  </div>
  <div class="panel">
    <h2>Best Candidates</h2>
    <table>
      <thead><tr><th>Channel</th><th>Freq MHz</th><th>PHY</th><th>Packets</th><th>PER %</th></tr></thead>
      <tbody>{''.join(top_rows) or '<tr><td colspan="5">No sweep points recorded.</td></tr>'}</tbody>
    </table>
  </div>
  <div class="panel">
    <h2>All Sweep Points</h2>
    <table>
      <thead><tr><th>Channel</th><th>Freq MHz</th><th>PHY</th><th>RSSI</th><th>PER %</th><th>CRC OK</th><th>CRC Err</th><th>Packets</th></tr></thead>
      <tbody>{''.join(body_rows) or '<tr><td colspan="8">No sweep points recorded.</td></tr>'}</tbody>
    </table>
  </div>
</div>
</body>
</html>
"""
    path.write_text(html_doc, encoding="utf-8")
    return path
