#!/usr/bin/env python3
"""
ARC Tanzania Weather Station Dashboard - Build Script

Reads the Omnisense CSV, processes wind/solar/precipitation data through
modular processors, and generates a self-contained index.html with embedded
data and Plotly.js charts.

Usage:
    python build.py                         # Standard build
    python build.py --csv path/to/file.csv  # Specify CSV file
"""

import argparse
import base64
import json
import math
import struct
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from modules.common import (
    load_weather_csv, find_latest_csv, build_available_periods,
    spike_filter, detect_precip_resets, to_eat_ms, TIMEZONE,
)
from modules import wind, solar, precipitation, cross_variable


# ── Configuration ─────────────────────────────────────────────────────────────
OUTPUT_FILE = Path("index.html")
LOGO_TRIM_PATH = Path("logo/logotrim.png")
LOGO_FULL_PATH = Path("logo/logo.png")
CYCLES_DIR = Path("data/cycles")

# Building orientation in degrees from North (clockwise).
# Set this once the actual building bearing is confirmed.
# Used in Driving Rain Index facade calculations.
BUILDING_ORIENTATION_DEG = 0  # TODO: replace with actual bearing


def _read_logo(path):
    """Read a logo PNG and return (data_uri, aspect_ratio)."""
    if not path.exists():
        return "", 1.0
    data = path.read_bytes()
    b64 = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    aspect = 1.0
    if data[:4] == b'\x89PNG':
        try:
            w = struct.unpack('>I', data[16:20])[0]
            h = struct.unpack('>I', data[20:24])[0]
            if h > 0:
                aspect = w / h
        except Exception:
            pass
    return b64, aspect


def get_logo_b64():
    """Return (header_logo_b64, header_aspect, watermark_logo_b64, watermark_aspect)."""
    trim_b64, trim_aspect = _read_logo(LOGO_TRIM_PATH)
    full_b64, full_aspect = _read_logo(LOGO_FULL_PATH)
    return trim_b64, trim_aspect, full_b64, full_aspect


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10,'th')}"


def format_fetch_time(dt):
    """Format a UTC datetime as '7th March 2026 at 04:32 UTC'."""
    if dt is None:
        return None
    return f"{_ordinal(dt.day)} {dt.strftime('%B %Y')} at {dt.strftime('%H:%M')} UTC"


# ── Cycle phase parsing ───────────────────────────────────────────────────────

def parse_enso_oni(path):
    """Parse NOAA ONI CSV -> dict of 'YYYY-MM' -> phase index (0=La Nina, 1=Neutral, 2=El Nino).
    ONI thresholds: <= -0.5 La Nina, >= 0.5 El Nino, else Neutral."""
    phases = {}
    if not path.exists():
        print(f"  Warning: {path} not found, ENSO phases will be empty")
        return phases
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("Date"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date_str = parts[0].strip()
        val_str = parts[1].strip()
        try:
            val = float(val_str)
        except ValueError:
            continue
        if val <= -99:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        key = f"{dt.year}-{dt.month:02d}"
        if val <= -0.5:
            phases[key] = 0
        elif val >= 0.5:
            phases[key] = 2
        else:
            phases[key] = 1
    print(f"  ENSO: {len(phases)} months parsed")
    return phases


def parse_iod_dmi(path):
    """Parse BoM IOD weekly DMI -> dict of 'YYYY-MM' -> phase index (0=Negative, 1=Neutral, 2=Positive).
    Weekly values are averaged per month, then classified."""
    monthly_vals = {}
    if not path.exists():
        print(f"  Warning: {path} not found, IOD phases will be empty")
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            start_str = parts[0].strip()
            dmi = float(parts[2].strip())
            dt = datetime.strptime(start_str, "%Y%m%d")
        except (ValueError, IndexError):
            continue
        key = f"{dt.year}-{dt.month:02d}"
        monthly_vals.setdefault(key, []).append(dmi)
    phases = {}
    for key, vals in monthly_vals.items():
        avg = sum(vals) / len(vals)
        if avg <= -0.4:
            phases[key] = 0
        elif avg >= 0.4:
            phases[key] = 2
        else:
            phases[key] = 1
    print(f"  IOD: {len(phases)} months parsed")
    return phases


def _iso_week(dt):
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _romi_to_phase(rmm1, rmm2):
    angle = math.degrees(math.atan2(rmm2, rmm1)) % 360
    sector = int(angle / 45) % 8
    phase_map = [5, 6, 7, 8, 1, 2, 3, 4]
    return phase_map[sector]


def parse_mjo_romi(path):
    """Parse NOAA ROMI data -> dict of 'YYYY-Www' -> phase index (0-7, or -1 for weak/inactive).
    Daily data aggregated to ISO weeks by majority phase; amplitude < 1.0 -> weak (-1)."""
    weekly_phases = {}
    if not path.exists():
        print(f"  Warning: {path} not found, MJO phases will be empty")
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            yr, mo, dy = int(parts[0]), int(parts[1]), int(parts[2])
            rmm1 = float(parts[4])
            rmm2 = float(parts[5])
            amplitude = float(parts[6])
        except (ValueError, IndexError):
            continue
        try:
            dt = date(yr, mo, dy)
        except ValueError:
            continue
        wk = _iso_week(dt)
        if amplitude < 1.0:
            phase_idx = -1
        else:
            phase_num = _romi_to_phase(rmm1, rmm2)
            phase_idx = phase_num - 1
        weekly_phases.setdefault(wk, []).append(phase_idx)
    phases = {}
    for wk, daily in weekly_phases.items():
        counts = Counter(daily)
        n_weak = counts.get(-1, 0)
        if n_weak > len(daily) / 2:
            phases[wk] = -1
        else:
            non_weak = {k: v for k, v in counts.items() if k >= 0}
            if non_weak:
                phases[wk] = max(non_weak, key=non_weak.get)
            else:
                phases[wk] = -1
    print(f"  MJO: {len(phases)} weeks parsed (from ROMI data)")
    return phases


def generate_cycle_phases_js():
    """Parse cycle data files and return (js_string, freshness_dict).

    freshness_dict contains enso_last, iod_last, mjo_last (last data keys) for
    use in the dataFreshness stale-data indicator.
    """
    print("Parsing climate cycle data...")
    enso = parse_enso_oni(CYCLES_DIR / "enso" / "oni.csv")
    iod = parse_iod_dmi(CYCLES_DIR / "iod" / "iod_1.txt")
    mjo = parse_mjo_romi(CYCLES_DIR / "mjo" / "romi.cpcolr.1x.txt")

    freshness = {}
    enso_keys = sorted(enso.keys())
    iod_keys = sorted(iod.keys())
    mjo_keys = sorted(mjo.keys())
    if enso_keys:
        freshness["enso_last"] = enso_keys[-1]
    if iod_keys:
        freshness["iod_last"] = iod_keys[-1]
    if mjo_keys:
        freshness["mjo_last"] = mjo_keys[-1]
    oni_path = CYCLES_DIR / "enso" / "oni.csv"
    if oni_path.exists():
        mtime = datetime.fromtimestamp(oni_path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
        freshness["cyclesFetchTime"] = format_fetch_time(mtime)

    def dict_to_js(d, per_line=6):
        items = [f"'{k}':{v}" for k, v in sorted(d.items())]
        lines = []
        for i in range(0, len(items), per_line):
            lines.append("  " + ",".join(items[i:i+per_line]) + ",")
        return "{\n" + "\n".join(lines) + "\n}" if lines else "{}"

    js = []
    js.append("// Climate oscillation phase lookup tables (auto-generated from cycle data files)")
    js.append("// ENSO: ONI-based. 0=La Ni\u00f1a, 1=Neutral, 2=El Ni\u00f1o")
    js.append("const ENSO_LABELS = ['La Ni\u00f1a', 'Neutral', 'El Ni\u00f1o'];")
    js.append(f"const ENSO_PHASES = {dict_to_js(enso)};")
    js.append("// IOD: DMI-based. 0=Negative, 1=Neutral, 2=Positive")
    js.append("const IOD_LABELS = ['Negative IOD', 'Neutral', 'Positive IOD'];")
    js.append(f"const IOD_PHASES = {dict_to_js(iod)};")
    js.append("// MJO: Phase by week (YYYY-Www \u2192 phase 0-7, or -1 for weak/inactive)")
    js.append("// Derived from ROMI (Real-time OLR-based MJO Index) converted to RMM phases")
    js.append("const MJO_LABELS = ['Phase 1 (W. Hem/Africa)','Phase 2 (Indian Ocean)','Phase 3 (E. Indian Ocean)',")
    js.append("  'Phase 4 (Maritime Continent)','Phase 5 (W. Pacific)','Phase 6 (W. Pacific/Dateline)',")
    js.append("  'Phase 7 (E. Pacific)','Phase 8 (W. Hem/Africa)'];")
    js.append(f"const MJO_PHASES = {dict_to_js(mjo)};")
    return "\n".join(js), freshness


def build_dashboard(csv_path=None):
    """Main build function."""
    # Find CSV
    if csv_path:
        csv_file = csv_path
    else:
        csv_file = find_latest_csv()

    print(f"Loading data from: {csv_file}")

    # Load and parse
    df = load_weather_csv(csv_file)
    print(f"Loaded {len(df)} weather station readings")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")

    # Build available periods for date range selector
    periods = build_available_periods(df)

    # Process each module
    print("Processing wind data...")
    wind_result = wind.process(df)

    print("Processing solar data...")
    solar_result = solar.process(df)

    print("Processing precipitation data...")
    precip_result = precipitation.process(df)

    # Get rain events for cross-variable analysis
    rain_events = None
    for chart in precip_result["charts"]:
        if chart["id"] == "rain-events":
            rain_events = chart.get("events", [])
            break

    print("Processing cross-variable analyses...")
    cross_result = cross_variable.process(df, rain_events)

    # Assemble all data
    all_charts = (
        wind_result["charts"] +
        solar_result["charts"] +
        precip_result["charts"] +
        cross_result["charts"]
    )

    # Rename diurnal charts to Average Profiles
    _rename_map = {
        "diurnal-wind":    ("avg-wind-profiles",    "Average Wind Profiles",    "Maelezo ya Wastani ya Upepo"),
        "diurnal-solar":   ("avg-solar-profiles",   "Average Solar Profiles",   "Maelezo ya Wastani ya Jua"),
        "diurnal-rainfall":("avg-rainfall-profiles","Average Rainfall Profiles","Maelezo ya Wastani ya Mvua"),
    }
    for chart in all_charts:
        if chart.get("id") in _rename_map:
            new_id, new_title, new_title_sw = _rename_map[chart["id"]]
            chart["id"] = new_id
            chart["title"] = new_title
            chart["title_sw"] = new_title_sw

    all_stats = {
        "wind": wind_result["stats"],
        "solar": solar_result["stats"],
        "precipitation": precip_result["stats"],
        "cross": cross_result["stats"],
    }

    # Data freshness
    csv_name = Path(csv_file).stem
    # Extract timestamp from filename like omnisense_20260322_0449
    fetch_ts = ""
    parts = csv_name.split("_")
    if len(parts) >= 3:
        fetch_ts = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]} {parts[2][:2]}:{parts[2][2:4]} UTC"

    # Build raw timeseries for client-side recomputation when range is filtered
    df_r = df.copy()
    df_r["peak_wind_kph"] = spike_filter(df_r["peak_wind_kph"], 150)
    precip_incr = detect_precip_resets(df_r["precip_total_mm"]).diff().clip(lower=0).fillna(0)
    raw_data = {
        "ts":         [to_eat_ms(t) for t in df_r["timestamp"]],
        "avgWind":    [round(float(v), 1) if pd.notna(v) else None for v in df_r["avg_wind_kph"]],
        "peakWind":   [round(float(v), 1) if pd.notna(v) else None for v in df_r["peak_wind_kph"]],
        "windDir":    [int(v) if pd.notna(v) else None for v in df_r["wind_dir"]],
        "solar":      [round(float(v), 1) if pd.notna(v) else None for v in df_r["solar_wm2"]],
        "precipRate": [round(float(v), 3) if pd.notna(v) else None for v in df_r["precip_rate_mmh"]],
        "precipIncr": [round(float(v), 3) if pd.notna(v) else None for v in precip_incr],
    }

    data_blob = {
        "meta": periods,
        "charts": all_charts,
        "stats": all_stats,
        "raw": raw_data,
        "dataFreshness": {
            "csvFile": Path(csv_file).name,
            "fetchTime": fetch_ts,
            "rowCount": len(df),
            "dateMin": str(df["timestamp"].min()),
            "dateMax": str(df["timestamp"].max()),
        },
    }

    # Generate HTML
    header_logo_b64, header_logo_aspect, watermark_logo_b64, watermark_logo_aspect = get_logo_b64()
    cycle_phases_js, cycle_freshness = generate_cycle_phases_js()
    data_blob["dataFreshness"].update(cycle_freshness)
    json_str = json.dumps(data_blob, separators=(',', ':'), default=str)

    html = HTML_TEMPLATE
    html = html.replace('__DATA__', json_str)
    html = html.replace('__LOGO_B64__', header_logo_b64)
    html = html.replace('__LOGO_ASPECT__', str(round(header_logo_aspect, 4)))
    html = html.replace('__WATERMARK_LOGO_B64__', watermark_logo_b64)
    html = html.replace('__WATERMARK_LOGO_ASPECT__', str(round(watermark_logo_aspect, 4)))
    html = html.replace('// __CYCLE_PHASES_JS__', cycle_phases_js)

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print(f"Generated {OUTPUT_FILE} ({size_kb:.0f} KB)")


# ── HTML Template ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARC Tanzania - Weather Station</title>
<link href="https://fonts.googleapis.com/css2?family=Ubuntu:wght@300;400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Ubuntu',sans-serif;font-size:13px;background:#f8f9fa;color:#333;display:flex;flex-direction:column;height:100vh;overflow:hidden}
#header{background:white;border-bottom:1px solid #ddd;padding:6px 12px;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap;min-height:40px}
#header h1{font-size:18px;font-weight:500;color:#222;margin-right:2px;white-space:nowrap}
#logo{height:32px;width:auto;flex-shrink:0;vertical-align:middle}
#header a{display:flex;align-items:center}
#main{display:flex;flex:1;overflow:hidden;position:relative}
#sidebar{width:300px;background:white;border-right:1px solid #ddd;overflow-y:auto;padding:10px;flex-shrink:0;display:flex;flex-direction:column;gap:8px;transition:transform 0.2s ease;z-index:10}
#chart-area{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;position:relative}
#time-bar{background:white;border-bottom:1px solid #ddd;padding:6px 10px;display:flex;flex-direction:column;gap:4px;flex-shrink:0}
#time-bar-top{display:flex;align-items:center;width:100%;gap:8px}
#time-bar-left{flex:1;display:flex;align-items:center;gap:8px}
#bar-title{font-size:14px;font-weight:600;color:#222;white-space:nowrap;text-align:center;padding:0 8px;overflow:hidden;text-overflow:ellipsis}
#time-bar-right{flex:1;display:flex;align-items:center;gap:8px;justify-content:flex-end;flex-wrap:wrap}
#chart{flex:1;min-height:0}
.section{display:flex;flex-direction:column;gap:2px}
.section-title{font-weight:600;font-size:11px;color:#666;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em}
select,button,input{font-family:inherit}
select,input[type="date"],input[type="number"]{font-size:12px;padding:3px 5px;border:1px solid #ccc;border-radius:4px;background:white}
select{cursor:pointer;max-width:100%}
select:focus{outline:none;border-color:#4a90d9}
.divider{border:none;border-top:1px solid #eee;margin:2px 0}
label{font-size:12px}
.cb-label{display:flex;align-items:center;gap:5px;padding:1px 0;cursor:pointer;line-height:1.4;font-size:12px}
.cb-label:hover{color:#1f77b4}
.control-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.control-row label{font-size:12px;color:#666;white-space:nowrap}
.hidden{display:none!important}
.info-i{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;background:#999;color:white;font-size:9px;font-style:italic;font-weight:700;cursor:help;flex-shrink:0;line-height:1;font-family:Georgia,'Times New Roman',serif}
.info-i:hover{background:#666}
#info-fixed-tip,.info-tip-fixed{display:none;position:fixed;background:#333;color:white;font-size:12px;font-family:'Ubuntu',sans-serif;padding:6px 9px;border-radius:4px;line-height:1.5;width:320px;max-width:90vw;z-index:9999;pointer-events:none;white-space:normal}
#chart-info-tip{display:none;position:fixed;background:#333;color:white;font-size:12px;font-family:'Ubuntu',sans-serif;padding:6px 9px;border-radius:4px;line-height:1.5;width:320px;max-width:90vw;z-index:9999;pointer-events:none;white-space:normal}
.stats-panel{background:#f0f8f0;border:1px solid #c8e6c9;border-radius:6px;padding:8px;font-size:12px}
.stats-panel h4{font-size:12px;font-weight:600;margin-bottom:4px;color:#2e7d32}
.stats-row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #e8f5e9}
.stats-row:last-child{border-bottom:none}
.stats-label{color:#555}
.stats-value{font-weight:500;color:#333}
#chart-category{font-weight:600;font-size:13px;padding:3px 7px;border:1px solid #aaa;border-radius:4px;background:#f5f5f5}
#chart-select{font-size:12px}
#download-btn{padding:4px 10px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:#28a745;color:white;font-weight:500;white-space:nowrap}
#download-btn:hover{background:#218838}
#download-btn:disabled{opacity:0.6;cursor:default}
#dl-spinner{display:none;width:16px;height:16px;border:2px solid rgba(40,167,69,0.3);border-top-color:#28a745;border-radius:50%;animation:dlspin 0.7s linear infinite;flex-shrink:0}
@keyframes dlspin{to{transform:rotate(360deg)}}
#lang-wrap { position: relative; flex-shrink: 0; }
#lang-btn { background: none; border: 1px solid #ccc; border-radius: 4px; padding: 3px 6px; cursor: pointer; font-size: 16px; line-height: 1; color: #555; display: flex; align-items: center; }
#lang-btn:hover { background: #f0f0f0; border-color: #aaa; }
#lang-menu { display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; background: white; border: 1px solid #ccc; border-radius: 4px; box-shadow: 0 2px 8px rgba(0,0,0,0.12); z-index: 200; min-width: 110px; }
#lang-menu.open { display: block; }
#lang-menu button { display: block; width: 100%; text-align: left; padding: 6px 10px; border: none; background: none; cursor: pointer; font-size: 12px; font-family: inherit; color: #333; }
#lang-menu button:hover { background: #f0f4ff; }
#lang-menu button.active { font-weight: 600; color: #1f77b4; }
.bar-divider{border-left:1px solid #ccc;height:20px;flex-shrink:0;margin:0 2px}
#sidebar-toggle{display:none;background:none;border:1px solid #ccc;border-radius:4px;padding:4px 7px;cursor:pointer;font-size:16px;line-height:1;color:#555;flex-shrink:0}
#sidebar-toggle:hover{background:#f0f0f0}
.stale-warn{color:#d4880f;font-size:11px;cursor:help}
#rain-events-table{width:100%;border-collapse:collapse;font-size:11px}
#rain-events-table th{background:#f0f0f0;padding:4px 6px;text-align:left;cursor:pointer;border-bottom:2px solid #ddd;position:sticky;top:0;user-select:none}
#rain-events-table th:hover{background:#e0e0e0}
#rain-events-table th .sort-arrow{margin-left:4px;opacity:0.4;font-size:10px}
#rain-events-table th.sort-asc .sort-arrow,#rain-events-table th.sort-desc .sort-arrow{opacity:1}
#rain-events-table td{padding:3px 6px;border-bottom:1px solid #eee}
#rain-events-table tr:hover{background:#f5f5f5}
#events-container{max-height:100%;overflow:auto;flex:1}
input[type="range"]{width:100%}
.slider-row{display:flex;align-items:center;gap:6px}
.slider-value{min-width:40px;text-align:right;font-weight:500;font-size:12px}
optgroup{font-weight:600;font-style:normal}
#sidebar-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.3);z-index:9}
@media(max-width:900px){
  #sidebar{width:190px;padding:8px}
  #header h1{font-size:13px}
}
@media(max-width:680px){
  #sidebar-toggle{display:block}
  #sidebar{position:absolute;top:0;left:0;height:100%;width:300px;transform:translateX(-100%);box-shadow:2px 0 8px rgba(0,0,0,0.15)}
  #sidebar.open{transform:translateX(0)}
  #sidebar-backdrop.open{display:block}
  #header{padding:5px 8px;gap:6px}
  #header h1{font-size:12px}
  #time-bar{padding:5px 8px;gap:3px}
  #time-bar-top{gap:5px}
  #bar-title{font-size:12px}
  select{font-size:11px}
  .cb-label{font-size:11px}
}
@media(max-width:420px){
  #header h1{display:none}
  #download-btn{font-size:11px;padding:3px 7px}
  input[type=date]{font-size:11px;max-width:110px}
}
#wind-unit-wrap{display:none;padding:2px 0 0 0}
.wind-unit-notch{display:inline-flex;border:1px solid #d0d0d0;border-radius:3px;overflow:hidden}
.wind-unit-btn{padding:2px 7px;font-size:10px;border:none;background:transparent;cursor:pointer;color:#999;white-space:nowrap}
.wind-unit-btn.active{background:#e6e6e6;color:#333;font-weight:600}
#wind-cat-controls{display:none}
#wind-cat-custom-toggle{display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.05em;padding:2px 0;user-select:none}
#wind-cat-custom-toggle:hover{color:#222}
#wind-cat-custom-arrow{transition:transform .2s;display:inline-block;font-size:9px}
.wind-unit-btn:not(.active):hover{background:#f0f0f0;color:#555}
</style>
</head>
<body>

<div id="sidebar-backdrop"></div>
<div id="header">
  <button id="sidebar-toggle" aria-label="Toggle controls">&#9776;</button>
  <a href="https://actionresearchprojects.net"><img id="logo" alt="ARC"></a>
  <h1 data-i18n="title">ARC Tanzania - Weather Station</h1>
  <a href="https://actionresearchprojects.net/explainers/arc-tz-weather" target="_blank" class="info-i" id="about-info-icon" title="About this dashboard" style="text-decoration:none;margin-left:auto;">i</a>
  <div id="lang-wrap">
    <button id="lang-btn" onclick="document.getElementById('lang-menu').classList.toggle('open')" title="Language"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></button>
    <div id="lang-menu">
      <button onclick="setLanguage('en')">English</button>
      <button onclick="setLanguage('sw')">Kiswahili</button>
    </div>
  </div>
</div>

<div id="main">
  <div id="sidebar">

    <!-- Periodic options (shown for avg profiles charts) -->
    <div class="section" id="periodic-options" style="display:none">
      <div class="section-title" data-i18n="periodSettings">Period Settings</div>
      <label class="cb-label" style="margin-bottom:6px;">
        <span data-i18n="groupBy">Group By</span>
        <select id="period-group-by" style="margin-left:6px;font-size:12px;">
          <option value="hour" data-i18n="hour">Hour</option>
          <option value="synoptic" data-i18n="synopticHours">Synoptic Hours</option>
        </select>
      </label>
      <label class="cb-label" style="margin-bottom:6px;">
        <span data-i18n="cycle">Cycle</span>
        <select id="natural-cycles" style="margin-left:6px;font-size:12px;">
          <option value="day" data-i18n="day">Day</option>
          <option value="year" data-i18n="year">Year</option>
          <option value="mjo">Madden&ndash;Julian Oscillation (MJO)</option>
          <option value="iod">Indian Ocean Dipole (IOD)</option>
          <option value="enso">El Ni&ntilde;o&ndash;Southern Oscillation (ENSO)</option>
        </select>
        <span class="info-i" id="natural-cycles-info" style="display:none;margin-left:4px;">i</span>
      </label>
      <div id="natural-cycles-tip" style="display:none;font-size:11px;color:#666;line-height:1.4;margin-bottom:6px;padding:6px 8px;background:#f5f5f5;border:1px solid #ddd;border-radius:4px;"></div>
      <div id="periodic-warnings" style="margin-top:6px;font-size:11px;color:#a0522d;"></div>
    </div>
    <hr class="divider" id="periodic-divider" style="display:none">

    <!-- Wind unit notch (shown for wind-related charts) -->
    <div id="wind-unit-wrap">
      <div class="wind-unit-notch">
        <button id="wu-kmh" class="wind-unit-btn active" onclick="setWindUnit('kmh')">km/h</button><button id="wu-ms" class="wind-unit-btn" onclick="setWindUnit('ms')">m/s</button><button id="wu-kn" class="wind-unit-btn" onclick="setWindUnit('kn')">kn</button>
      </div>
    </div>

    <!-- Wind series checkboxes (shown for wind-timeseries and wind-category-dist) -->
    <div id="wind-series-controls" style="display:none">
      <div class="section">
        <div class="section-title">Series</div>
        <label class="cb-label"><input type="checkbox" id="cb-wind-avg" checked onchange="updateWindSeries()"> Average Wind</label>
        <label class="cb-label"><input type="checkbox" id="cb-wind-gust" onchange="updateWindSeries()"> Peak Gust</label>
        <label class="cb-label" id="cb-wind-24h-label" style="display:none"><input type="checkbox" id="cb-wind-24h" onchange="updateWindSeries()"> 24hr Mean</label>
      </div>
      <hr class="divider">
    </div>

    <!-- Wind category distribution controls (shown for wind-category-dist chart) -->
    <div id="wind-cat-controls">
      <div class="section">
        <div class="section-title">Wind Categories</div>
        <label class="cb-label" style="margin-bottom:6px;">Classification
          <select id="wind-cat-system" onchange="setWindCatSystem(this.value)" style="margin-left:6px">
            <option value="beaufort">Beaufort (WMO)</option>
            <option value="lawson" selected>Lawson 2001</option>
            <option value="davenport">Davenport 1975</option>
            <option value="custom">Custom</option>
          </select>
        </label>
        <hr class="divider">
        <label class="cb-label" style="margin-bottom:6px;">
          <span data-i18n="countBy">Count By</span>
          <select id="wind-cat-value-unit" onchange="setWindCatValueUnit(this.value)" style="margin-left:6px">
            <option value="pct" selected>Percentage</option>
            <option value="hours">Hours</option>
            <option value="days">Days</option>
            <option value="weeks">Weeks</option>
            <option value="months">Months</option>
          </select>
        </label>
        <label class="cb-label" style="margin-bottom:6px;display:none;" id="wind-cat-cycle-label">
          <span data-i18n="cycle">Cycle</span>
          <select id="wind-cat-per-unit" onchange="setWindCatPerUnit(this.value)" style="margin-left:6px">
          </select>
        </label>
        <div id="wind-cat-custom-section" style="display:none;margin-top:4px">
          <hr class="divider" style="margin-bottom:6px">
          <div id="wind-cat-custom-toggle" onclick="toggleCustomEditor()"><span id="wind-cat-custom-arrow">&#9658;</span> Custom Thresholds</div>
          <div id="wind-cat-custom-editor" style="display:none;margin-top:6px"><div id="wind-cat-bands-list"></div><div style="display:flex;gap:4px;margin-top:4px"><button onclick="addCustomBand()" style="font-size:10px;padding:1px 6px;border:1px solid #ccc;border-radius:3px;cursor:pointer">+ Add band</button><button onclick="applyCustomBands()" style="font-size:10px;padding:1px 6px;border:1px solid #ccc;border-radius:3px;cursor:pointer;background:#e8f0ff">Apply</button></div></div>
        </div>
      </div>
    </div>

    <!-- Stats Panel (populated by JS) -->
    <hr class="divider" id="stats-divider" style="margin:-4px 0 -4px 0">
    <div class="stats-panel" id="stats-panel" style="margin-top:-2px">
      <h4 id="stats-heading">Statistics</h4>
      <div id="stats-content"></div>
    </div>

    <!-- Data Freshness -->
    <div style="margin-top:auto;padding-top:8px;border-top:1px solid #eee;">
      <div id="data-freshness" style="font-size:10px;color:#888;line-height:1.6"></div>
    </div>
  </div>

  <div id="chart-area">
    <div id="time-bar">
      <div id="time-bar-top">
        <div id="time-bar-left">
          <select id="chart-category">
            <option value="wind" data-i18n="windGroup">Wind</option>
            <option value="solar" data-i18n="solarGroup">Solar</option>
            <option value="precipitation" data-i18n="precipGroup">Precipitation</option>
          </select>
          <select id="chart-select">
            <option value="wind-rose" data-i18n="windRose">Wind Rose</option>
            <option value="wind-timeseries" data-i18n="windTimeSeries">Wind Speed (Time Series)</option>
            <option value="avg-wind-profiles" data-i18n="avgWindProfiles">Average Wind Profiles</option>
            <option value="wind-distribution" data-i18n="windDistribution">Wind Speed Distribution</option>
            <option value="gust-factor" data-i18n="gustFactor">Gust Factor</option>
            <option value="calm-periods" data-i18n="calmPeriods">Calm Periods</option>
            <option value="ventilation-availability" data-i18n="ventAvailability">Ventilation Availability</option>
            <option value="wind-category-dist" data-i18n="windCatDist">Wind Speed Categories</option>
          </select>
          <span class="info-i" id="chart-info-icon">i</span>
          <div id="chart-info-tip"></div>
        </div>
        <span id="bar-title"></span>
        <div id="time-bar-right">
          <div class="control-row">
            <label>Range:</label>
            <select id="time-mode">
              <option value="all" data-i18n="allTime">All time</option>
              <option value="between" data-i18n="betweenDates">Between dates</option>
              <option value="year" data-i18n="year">Year</option>
              <option value="season" data-i18n="season">Season</option>
              <option value="month" data-i18n="month">Month</option>
              <option value="week" data-i18n="week">Week</option>
              <option value="day" data-i18n="day">Day</option>
            </select>
          </div>
          <div id="between-inputs" class="control-row hidden">
            <label>From <input type="date" id="date-start"></label>
            <label>To <input type="date" id="date-end"></label>
          </div>
          <div id="year-input"   class="hidden"><select id="year-select"></select></div>
          <div id="season-input" class="hidden"><select id="season-select"></select></div>
          <div id="month-input" class="hidden"><select id="month-select"></select></div>
          <div id="week-input" class="hidden"><select id="week-select"></select></div>
          <div id="day-input" class="hidden"><select id="day-select"></select></div>
          <button id="download-btn" data-i18n="downloadPng">Download PNG</button>
          <div id="dl-spinner"></div>
        </div>
      </div>
      <div class="info-tip-fixed" id="period-info-tip"></div>
    </div>
    <div id="chart"></div>
    <div id="events-container" class="hidden">
      <table id="rain-events-table">
        <thead>
          <tr>
            <th data-i18n="evStart" data-col="start_ms">Start</th>
            <th data-i18n="evEnd" data-col="end_ms">End</th>
            <th data-i18n="evDuration" data-col="duration_min">Duration</th>
            <th data-i18n="evTotal" data-col="total_mm">Total (mm)</th>
            <th data-i18n="evPeakRate" data-col="peak_rate">Peak (mm/h)</th>
            <th data-i18n="evMeanRate" data-col="mean_rate">Mean (mm/h)</th>
            <th data-i18n="evWindDir" data-col="wind_dir">Wind Dir</th>
          </tr>
        </thead>
        <tbody id="rain-events-body"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
// ── Data ──────────────────────────────────────────────────────────────────────
const ALL_DATA = __DATA__;
const LOGO_B64 = '__LOGO_B64__';
const LOGO_ASPECT = __LOGO_ASPECT__;
const WATERMARK_LOGO_B64 = '__WATERMARK_LOGO_B64__';
const WATERMARK_LOGO_ASPECT = __WATERMARK_LOGO_ASPECT__;

// __CYCLE_PHASES_JS__

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  chartType: 'wind-rose',
  timeMode: 'all',
  betweenStart: null,
  betweenEnd: null,
  selectedYear: null,
  selectedSeason: null,
  selectedMonth: null,
  selectedWeek: null,
  selectedDay: null,
  savedZoom: null,
  periodCycle: 'day',
  periodGroupBy: 'hour',
  windUnit: 'kmh',  // 'ms' = m/s, 'kmh' = km/h (default)
  windCatSystem: 'lawson',     // classification system for wind-category-dist
  windCatValueUnit: 'pct',     // count by: pct|hours|days|weeks|months
  windCatPerUnit: 'day',       // cycle: day|week|month|year
  windCatCustomBands: null,    // user-defined bands; null = use Lawson defaults
  windCatCustomUnit: 'ms',     // unit for custom threshold values: ms|kmh|kn
};

let currentLang = 'en';
let rainEventsSort = {col: 'start_ms', dir: 'desc'};

// ── i18n ─────────────────────────────────────────────────────────────────────
const I18N = {
  en: {
    title: 'ARC Tanzania - Weather Station',
    chartType: 'Chart Type',
    periodSettings: 'Period Settings',
    statistics: 'Statistics',
    range: 'Range:',
    allTime: 'All time',
    betweenDates: 'Between dates',
    season: 'Season',
    month: 'Month',
    week: 'Week',
    day: 'Day',
    from: 'From ',
    to: 'To ',
    downloadPng: 'Download PNG',
    windGroup: 'Wind',
    solarGroup: 'Solar',
    precipGroup: 'Precipitation',
    combinedGroup: 'Combined',
    windRose: 'Wind Rose',
    windTimeSeries: 'Wind Speed (Time Series)',
    diurnalWind: 'Average Wind Profiles',
    avgWindProfiles: 'Average Wind Profiles',
    windDistribution: 'Wind Speed Distribution',
    gustFactor: 'Gust Factor',
    calmPeriods: 'Calm Periods',
    ventAvailability: 'Ventilation Availability',
    windCatDist: 'Wind Speed Categories',
    solarTimeSeries: 'Solar Radiation (Time Series)',
    dailyInsolation: 'Daily Insolation',
    diurnalSolar: 'Average Solar Profiles',
    avgSolarProfiles: 'Average Solar Profiles',
    solarDistribution: 'Solar Distribution',
    clearnessIndex: 'Clearness Index',
    peakSolarHours: 'Peak Solar Hours',
    cumulativeRainfall: 'Cumulative Rainfall',
    dailyRainfall: 'Daily Rainfall',
    rainfallIntensity: 'Rainfall Intensity',
    diurnalRainfall: 'Average Rainfall Profiles',
    avgRainProfiles: 'Average Rainfall Profiles',
    drySpells: 'Dry Spells',
    rainEvents: 'Rain Events',
    drivingRain: 'Driving Rain Index',
    windRain: 'Wind-Rain Coincidence',
    solarWind: 'Solar-Wind Correlation',
    preStorm: 'Pre-Storm Signatures',
    ventWindows: 'Ventilation Windows',
    evStart: 'Start',
    evEnd: 'End',
    evDuration: 'Duration',
    evTotal: 'Total (mm)',
    evPeakRate: 'Peak (mm/h)',
    evMeanRate: 'Mean (mm/h)',
    evWindDir: 'Wind Dir',
    // Info tooltips
    infoWindRose: 'Shows the frequency of wind from each of 16 compass directions, with colour bands for speed ranges. The central percentage shows how often conditions are calm (0 km/h). This reveals prevailing wind directions for orienting ventilation openings.',
    infoWindTS: 'Continuous time series of 5-minute average wind speed and peak gust. The red line shows the 24-hour running mean. Identifies storm events and the relationship between average and gust speeds.',
    infoDiurnalWind: 'Mean wind speed by hour of day, with shaded standard deviation band. The bar chart shows calm percentage by hour. Identifies the daily ventilation cycle; in coastal Tanzania, sea/land breezes create predictable diurnal patterns.',
    infoWindDist: 'Distribution of 5-minute average wind speeds. The dashed red line shows a Weibull probability distribution fit, commonly used in wind analysis. The Weibull shape (k) and scale (c) parameters characterise the site wind regime.',
    infoGustFactor: 'Each 5-minute reading plotted as gust factor (peak/avg) vs. average speed. Colour represents hour of day. The dashed red line at 2.0 marks the typical threshold for turbulent conditions. High gust factors at low speeds indicate gusty, turbulent conditions.',
    infoCalmPeriods: 'Distribution of consecutive calm period durations (0 km/h readings). Extended calm periods mean the building relies on stack effect alone for ventilation. This directly informs whether mechanical backup ventilation is needed.',
    infoVentAvail: 'For each day, shows hours in three categories: above ventilation threshold (effective wind), below threshold but non-zero (marginal), and calm. The threshold is adjustable. Directly answers "what fraction of the time is natural ventilation effective?"',
    infoWindCatDist: 'Horizontal bar chart showing how often wind falls into each speed category. Switch between Beaufort, Lawson 2001, Davenport, or custom thresholds. Count by percentage or a time unit (e.g. hours per day). Hover bars for speed ranges and counts.',
    infoSolarTS: 'Continuous time series of global horizontal irradiance (W/m2). Shows solar intensity patterns, cloudy vs. clear days, and seasonal trends. Directly related to solar heat gain through windows and roofing.',
    infoDailyInsol: 'Daily solar insolation (kWh/m2/day) calculated by integrating 5-minute radiation readings. The dashed red line shows the typical clear-sky reference for this latitude (~5.5 kWh/m2/day). Days below this line indicate significant cloud cover.',
    infoDiurnalSolar: 'Mean solar radiation by hour, with standard deviation shading. The shape of the diurnal curve (and deviation from clear-sky) characterises the site solar regime. Asymmetry (morning vs. afternoon) affects orientation-dependent heat gain.',
    infoSolarDist: 'Distribution of solar radiation readings during daylight hours (excluding night-time zeros). Bimodal distributions indicate frequent cloud interruption; unimodal high peaks indicate clear-sky dominance.',
    infoClearness: 'Daily clearness index Kt = what the sensor measured / what would arrive at ground level if there were no atmosphere. Values near 1 mean little was lost to cloud or haze; values near 0 mean heavy cloud. Because it divides out the seasonal variation in the sun\'s position, a Kt of 0.5 in December means the same sky condition as 0.5 in June. The theoretical maximum (extraterrestrial radiation H0) is calculated from latitude and day of year using Duffie and Beckman (2020); no external data is used. Colour bands are calibrated for a humid tropical coastal site: at Mkuranga, marine aerosols and high atmospheric moisture from the Indian Ocean mean even a genuinely clear day rarely exceeds Kt 0.60-0.65. Standard temperate-climate thresholds (clear > 0.65) would mis-classify clear days here as partly cloudy. Thresholds used: clear (Kt > 0.55), partly cloudy (0.25-0.55), overcast (Kt < 0.25), following Saunier, Reddy and Kumar (1987) and Udo (2000), who showed Liu-Jordan thresholds are not suitable for tropical sites.',
    infoPSH: 'Calculated entirely from ARC station data. The sensor records solar irradiance (W/m2) every 5 minutes; these readings are summed across each day and converted to total daily energy (kWh/m2). Peak Solar Hours (PSH) is that same number, reframed as a time equivalent: how many hours would the sun need to shine at its theoretical maximum (1,000 W/m2) to deliver the same energy? Because 1,000 W/m2 = 1 kW/m2, the maths simplifies to the same value. A day with 3.5 kWh/m2 of solar energy = 3.5 PSH.',
    infoCumRain: 'Corrected cumulative rainfall over the entire period. The raw sensor totals are corrected for counter resets by detecting negative jumps and adding the pre-reset total. The slope indicates rain intensity.',
    infoDailyRain: 'Daily rainfall totals derived from the corrected cumulative series. Colour indicates intensity category: light (< 2.5 mm, green), moderate (2.5-7.5 mm, yellow), heavy (7.5-25 mm, orange), very heavy (> 25 mm, red).',
    infoRainIntensity: 'Distribution of instantaneous rainfall rates during rain events. Log scale because most rain is light but rare intense events matter most for building design. The 95th percentile intensity is a key design parameter.',
    infoDiurnalRain: 'For each hour, shows mean rainfall amount (bars) and the probability that it is raining (red line). In tropical coastal locations, rain often follows a diurnal pattern with afternoon convective storms.',
    infoDrySpells: 'Distribution of consecutive periods with no rainfall. Dry spells indicate periods when windows can remain open without rain risk. Extended dry spells during the wet season may indicate unusual weather patterns.',
    infoRainEvents: 'Each row is one discrete rain event. Events are detected by grouping 5-minute readings where rainfall rate > 0, bridging gaps of up to 15 minutes so a single storm is not split into fragments. Trace events below 0.5 mm total are excluded: following WMO guidance, sub-0.5 mm falls are too small to meaningfully wet surfaces or contribute to runoff. Where rain is captured in only one reading, duration is shown as "< 5 min" since the exact duration within that sampling window is unknown.',
    infoDRI: 'The driving rain index (DRI) quantifies wind-driven rain exposure on building facades. The polar chart shows which directions deliver the most driving rain. This directly informs which facades need the most weather protection.',
    infoWindRainCo: 'Joint frequency distribution of wind speed and rainfall rate during rain events. Shows how often rain coincides with strong winds. If most rain falls during calm periods, windows can have rain shelters and stay open.',
    infoSolarWind: 'Explores the relationship between solar heating and wind speed. In coastal tropical locations, solar heating drives thermal convection, which may correlate with afternoon sea breezes. Colour indicates hour of day.',
    infoPreStorm: 'Composite plot showing the average behaviour of wind speed and solar radiation around rain events. Created by aligning all detected rain events at t=0 (event start) and averaging. Shows whether there are reliable pre-storm signatures.',
    infoVentWin: 'For each hour of each day, classifies the ventilation condition as: Effective (green, adequate wind, no rain), Marginal (yellow, some wind or light rain), or Closed (red, heavy rain). This is the synthesis chart combining all three weather variables.',
    infoPeriod: 'Select a time period to filter the data. "All time" shows the complete dataset. Other options let you zoom into specific seasons, months, weeks, or individual days.',
    // Periodic controls
    periodSettings: 'Period Settings',
    cycle: 'Cycle',
    groupBy: 'Group By',
    hour: 'Hour',
    synopticHours: 'Synoptic Hours',
    month: 'Month',
    week: 'Week',
    season: 'Season',
    year: 'Year',
    phase: 'Phase',
    hourOfDay: 'Hour of Day',
    timeOfDay: 'Time of Day',
    monthOfYear: 'Month of Year',
    weekOfYear: 'Week of Year',
    dayOfYear: 'Day of Year',
    tanzanianSeason: 'Tanzanian Season',
    infoAvgWindProfiles: 'Mean wind speed averaged across the selected cycle, with \u00b11 SD shading. Calm percentage bars (right axis) show how often wind is zero for each category. Use "Day" to see how the sea/land breeze cycle drives ventilation; use oscillation cycles (MJO, IOD, ENSO) to see how large-scale climate patterns affect wind.',
    infoAvgSolarProfiles: 'Mean solar radiation averaged across the selected cycle, with \u00b11 SD shading. "Day" shows the diurnal solar curve; "Year" reveals seasonal insolation patterns. Asymmetry in the diurnal curve indicates morning vs. afternoon cloud cover differences.',
    infoAvgRainProfiles: 'Mean rainfall amount (bars) and rain probability (line, right axis) averaged across the selected cycle. "Day" shows whether convective afternoon storms or nocturnal rain dominate; "Year" reveals the wet/dry season structure. Oscillation cycles show how MJO, IOD, and ENSO modulate rainfall.',
    // Data freshness
    dataUpdated: 'Data updated',
    staleWarning: 'Data may be stale (older than 2 days)',
  },
  sw: {
    title: 'ARC Tanzania - Kituo cha Hali ya Hewa',
    chartType: 'Aina ya Chati',
    periodSettings: 'Mipangilio ya Kipindi',
    statistics: 'Takwimu',
    range: 'Kipindi:',
    allTime: 'Wakati wote',
    betweenDates: 'Kati ya tarehe',
    season: 'Msimu',
    month: 'Mwezi',
    week: 'Wiki',
    day: 'Siku',
    from: 'Kutoka ',
    to: 'Hadi ',
    downloadPng: 'Pakua PNG',
    windGroup: 'Upepo',
    solarGroup: 'Jua',
    precipGroup: 'Mvua',
    combinedGroup: 'Pamoja',
    windRose: 'Mwelekeo wa Upepo',
    windTimeSeries: 'Kasi ya Upepo (Mfuatano)',
    diurnalWind: 'Maelezo ya Wastani ya Upepo',
    avgWindProfiles: 'Maelezo ya Wastani ya Upepo',
    windDistribution: 'Usambazaji wa Kasi ya Upepo',
    gustFactor: 'Kipengele cha Upepo Mkali',
    calmPeriods: 'Vipindi vya Utulivu',
    ventAvailability: 'Upatikanaji wa Hewa',
    windCatDist: 'Makundi ya Kasi ya Upepo',
    solarTimeSeries: 'Mionzi ya Jua (Mfuatano)',
    dailyInsolation: 'Jua la Kila Siku',
    diurnalSolar: 'Maelezo ya Wastani ya Jua',
    avgSolarProfiles: 'Maelezo ya Wastani ya Jua',
    solarDistribution: 'Usambazaji wa Jua',
    clearnessIndex: 'Fahirisi ya Uwazi',
    peakSolarHours: 'Masaa ya Jua Kali',
    cumulativeRainfall: 'Mvua ya Jumla',
    dailyRainfall: 'Mvua ya Kila Siku',
    rainfallIntensity: 'Kiwango cha Mvua',
    diurnalRainfall: 'Maelezo ya Wastani ya Mvua',
    avgRainProfiles: 'Maelezo ya Wastani ya Mvua',
    drySpells: 'Vipindi vya Ukame',
    rainEvents: 'Matukio ya Mvua',
    drivingRain: 'Fahirisi ya Mvua ya Upepo',
    windRain: 'Upepo na Mvua Wakati Mmoja',
    solarWind: 'Uhusiano wa Jua na Upepo',
    preStorm: 'Dalili za Kabla ya Dhoruba',
    ventWindows: 'Madirisha ya Hewa',
    evStart: 'Kuanza',
    evEnd: 'Kuisha',
    evDuration: 'Muda',
    evTotal: 'Jumla (mm)',
    evPeakRate: 'Kilele (mm/h)',
    evMeanRate: 'Wastani (mm/h)',
    evWindDir: 'Mwelekeo wa Upepo',
    dataUpdated: 'Data imesasishwa',
    staleWarning: 'Data inaweza kuwa ya zamani (zaidi ya siku 2)',
    // Periodic controls
    periodSettings: 'Mipangilio ya Kipindi',
    cycle: 'Mzunguko',
    groupBy: 'Panga kwa',
    hour: 'Saa',
    synopticHours: 'Masaa ya Synoptic',
    month: 'Mwezi',
    week: 'Wiki',
    season: 'Msimu',
    year: 'Mwaka',
    phase: 'Awamu',
    hourOfDay: 'Saa ya Siku',
    timeOfDay: 'Wakati wa Siku',
    monthOfYear: 'Mwezi wa Mwaka',
    weekOfYear: 'Wiki ya Mwaka',
    dayOfYear: 'Siku ya Mwaka',
    tanzanianSeason: 'Msimu wa Tanzania',
    infoAvgWindProfiles: 'Wastani wa kasi ya upepo kwa mzunguko uliochaguliwa, na kivuli cha \u00b11 SD. Asilimia ya utulivu (mhimili wa kulia) inaonyesha mara ngapi upepo ni sifuri kwa kila kategoria.',
    infoAvgSolarProfiles: 'Wastani wa mionzi ya jua kwa mzunguko uliochaguliwa, na kivuli cha \u00b11 SD. "Siku" inaonyesha mkunjo wa jua wa kila siku; "Mwaka" inafunua mwelekeo wa misimu.',
    infoAvgRainProfiles: 'Wastani wa mvua (nguzo) na uwezekano wa mvua (mstari, mhimili wa kulia) kwa mzunguko uliochaguliwa. "Siku" inaonyesha kama dhoruba za mchana au mvua za usiku zinatawala.',
  },
};

function t(key) { return (I18N[currentLang] || I18N.en)[key] || I18N.en[key] || key; }

// ── Helpers ──────────────────────────────────────────────────────────────────
function toEATString(ms) {
  return new Date(ms + 3 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' ');
}

function formatDuration(minutes, shortEvent) {
  if (shortEvent) return '< 5 min';
  if (minutes < 60) return Math.round(minutes) + ' min';
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m > 0 ? h + 'h ' + m + 'm' : h + 'h';
}

function getChartById(id) {
  return ALL_DATA.charts.find(c => c.id === id);
}

// Chart info tooltip text mapping
const CHART_INFO = {
  'wind-rose': 'infoWindRose',
  'wind-timeseries': 'infoWindTS',
  'avg-wind-profiles': 'infoAvgWindProfiles',
  'wind-distribution': 'infoWindDist',
  'gust-factor': 'infoGustFactor',
  'calm-periods': 'infoCalmPeriods',
  'ventilation-availability': 'infoVentAvail',
  'wind-category-dist': 'infoWindCatDist',
  'solar-timeseries': 'infoSolarTS',
  'daily-insolation': 'infoDailyInsol',
  'avg-solar-profiles': 'infoAvgSolarProfiles',
  'solar-distribution': 'infoSolarDist',
  'clearness-index': 'infoClearness',
  'peak-solar-hours': 'infoPSH',
  'cumulative-rainfall': 'infoCumRain',
  'daily-rainfall': 'infoDailyRain',
  'rainfall-intensity': 'infoRainIntensity',
  'avg-rainfall-profiles': 'infoAvgRainProfiles',
  'dry-spells': 'infoDrySpells',
  'rain-events': 'infoRainEvents',
  'driving-rain': 'infoDRI',
  'wind-rain': 'infoWindRainCo',
  'solar-wind': 'infoSolarWind',
  'pre-storm': 'infoPreStorm',
  'ventilation-windows': 'infoVentWin',
};

// SHELVED CHARTS (temporarily hidden; restore by adding back to the relevant category array below):
// {value: 'wind-rain', i18n: 'windRain', en: 'Wind-Rain Coincidence'}          -- was: combined
// {value: 'ventilation-windows', i18n: 'ventWindows', en: 'Ventilation Windows'} -- was: combined
// {value: 'pre-storm', i18n: 'preStorm', en: 'Pre-Storm Signatures'}            -- was: precipitation (last)
// {value: 'peak-solar-hours', i18n: 'peakSolarHours', en: 'Peak Solar Hours'}   -- was: solar; parked pending PSH literature review and clearer use case

const CATEGORY_CHARTS = {
  wind: [
    {value: 'wind-rose', i18n: 'windRose', en: 'Wind Rose'},
    {value: 'wind-timeseries', i18n: 'windTimeSeries', en: 'Wind Speed (Time Series)'},
    {value: 'avg-wind-profiles', i18n: 'avgWindProfiles', en: 'Average Wind Profiles'},
    {value: 'wind-distribution', i18n: 'windDistribution', en: 'Wind Speed Distribution'},
    {value: 'gust-factor', i18n: 'gustFactor', en: 'Gust Factor'},
    {value: 'calm-periods', i18n: 'calmPeriods', en: 'Calm Periods'},
    {value: 'ventilation-availability', i18n: 'ventAvailability', en: 'Ventilation Availability'},
    {value: 'wind-category-dist', i18n: 'windCatDist', en: 'Wind Speed Categories'},
  ],
  solar: [
    {value: 'solar-timeseries', i18n: 'solarTimeSeries', en: 'Solar Radiation (Time Series)'},
    {value: 'daily-insolation', i18n: 'dailyInsolation', en: 'Daily Insolation'},
    {value: 'avg-solar-profiles', i18n: 'avgSolarProfiles', en: 'Average Solar Profiles'},
    {value: 'solar-distribution', i18n: 'solarDistribution', en: 'Solar Distribution'},
    {value: 'clearness-index', i18n: 'clearnessIndex', en: 'Clearness Index'},
    {value: 'solar-wind', i18n: 'solarWind', en: 'Solar-Wind Correlation'},
  ],
  precipitation: [
    {value: 'driving-rain', i18n: 'drivingRain', en: 'Driving Rain Index'},
    {value: 'cumulative-rainfall', i18n: 'cumulativeRainfall', en: 'Cumulative Rainfall'},
    {value: 'daily-rainfall', i18n: 'dailyRainfall', en: 'Daily Rainfall'},
    {value: 'rainfall-intensity', i18n: 'rainfallIntensity', en: 'Rainfall Intensity'},
    {value: 'avg-rainfall-profiles', i18n: 'avgRainProfiles', en: 'Average Rainfall Profiles'},
    {value: 'dry-spells', i18n: 'drySpells', en: 'Dry Spells'},
    {value: 'rain-events', i18n: 'rainEvents', en: 'Rain Events'},
  ],
};

function populateChartSelect(category) {
  const sel = document.getElementById('chart-select');
  sel.innerHTML = '';
  const charts = CATEGORY_CHARTS[category] || [];
  charts.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.value;
    opt.textContent = t(c.i18n) || c.en;
    opt.dataset.i18n = c.i18n;
    sel.appendChild(opt);
  });
}

// ── Tooltip Wiring ───────────────────────────────────────────────────────────
function wireTooltip(iconId, tipId, textKey) {
  const icon = document.getElementById(iconId);
  const tip = document.getElementById(tipId);
  if (!icon || !tip) return;
  icon.addEventListener('mouseenter', (e) => {
    tip.textContent = t(textKey);
    tip.style.display = 'block';
    const r = icon.getBoundingClientRect();
    tip.style.left = Math.min(r.left, window.innerWidth - 340) + 'px';
    tip.style.top = (r.bottom + 6) + 'px';
  });
  icon.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
}

// ── Sidebar Visibility ───────────────────────────────────────────────────────
function updateSidebarControls() {
  const ct = state.chartType;
  // Show/hide chart vs table
  const isTable = ct === 'rain-events';
  document.getElementById('chart').classList.toggle('hidden', isTable);
  document.getElementById('events-container').classList.toggle('hidden', !isTable);
  // Show/hide periodic options
  const isPeriodic = ct === 'avg-wind-profiles' || ct === 'avg-solar-profiles' || ct === 'avg-rainfall-profiles';
  document.getElementById('periodic-options').style.display = isPeriodic ? '' : 'none';
  document.getElementById('periodic-divider').style.display = isPeriodic ? '' : 'none';
  // Show/hide wind unit toggle (all wind-group charts + cross charts with wind speed axes)
  const isWindRelated = ct === 'wind-rose' || ct === 'wind-timeseries' || ct === 'avg-wind-profiles' ||
    ct === 'wind-distribution' || ct === 'gust-factor' || ct === 'calm-periods' ||
    ct === 'ventilation-availability' || ct === 'wind-rain' || ct === 'solar-wind' ||
    ct === 'pre-storm' || ct === 'driving-rain' || ct === 'ventilation-windows' ||
    ct === 'wind-category-dist';
  document.getElementById('wind-unit-wrap').style.display = isWindRelated ? 'block' : 'none';
  // Show/hide wind series checkboxes
  const showWindSeries = ct === 'wind-timeseries' || ct === 'wind-category-dist';
  document.getElementById('wind-series-controls').style.display = showWindSeries ? '' : 'none';
  document.getElementById('cb-wind-24h-label').style.display = ct === 'wind-timeseries' ? '' : 'none';
  // Show/hide wind category distribution controls
  const isWindCatDist = ct === 'wind-category-dist';
  document.getElementById('wind-cat-controls').style.display = isWindCatDist ? 'block' : 'none';
  if (isWindCatDist) _updateWindCatCycleOptions();
}

// ── Stats Panel ──────────────────────────────────────────────────────────────
function getRangeLabel() {
  const mode = state.timeMode;
  if (mode === 'all') return '';
  if (mode === 'year') return state.selectedYear ? String(state.selectedYear) : '';
  if (mode === 'season') {
    const sel = document.getElementById('season-select');
    return sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '';
  }
  if (mode === 'month') {
    const sel = document.getElementById('month-select');
    return sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '';
  }
  if (mode === 'week') {
    const sel = document.getElementById('week-select');
    return sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '';
  }
  if (mode === 'day') {
    const sel = document.getElementById('day-select');
    return sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '';
  }
  if (mode === 'between') {
    const s = document.getElementById('date-start').value;
    const e = document.getElementById('date-end').value;
    if (s && e) return s + ' to ' + e;
    return s || e || '';
  }
  return '';
}

function updateStatsHeading() {
  const h4 = document.getElementById('stats-heading');
  if (!h4) return;
  const label = getRangeLabel();
  h4.textContent = t('statistics') + (label ? ' [' + label + ']' : '');
}

function updateStatsPanel() {
  updateStatsHeading();
  const ct = state.chartType;
  const content = document.getElementById('stats-content');
  const panel = document.getElementById('stats-panel');
  let html = '';

  const chart = _computedChart || getChartById(ct);
  const {ws, ss, ps, cs} = _getStats();

  if (ct.startsWith('wind') || ct === 'diurnal-wind' || ct === 'avg-wind-profiles' || ct === 'gust-factor' || ct === 'calm-periods' || ct === 'ventilation-availability') {
    const wDisp = v => (Math.round(wToUnit(v) * 100) / 100).toFixed(2);
    html += statsRow('Mean speed', wDisp(ws.meanSpeed) + ' ' + wLabel());
    html += statsRow('Max speed', wDisp(ws.maxSpeed) + ' ' + wLabel());
    html += statsRow('Max gust', wDisp(ws.maxGust) + ' ' + wLabel());
    html += statsRow('Calm %', ws.calmPct + '%');
    html += statsRow('Prevailing dir', ws.prevailingDir);
    html += statsRow('Median', wDisp(ws.medianSpeed) + ' ' + wLabel());
    html += statsRow('95th percentile', wDisp(ws.p95Speed) + ' ' + wLabel());
    if (ct === 'gust-factor' && chart) {
      html += statsRow('Mean gust factor', chart.meanGustFactor);
      html += statsRow('Median gust factor', chart.medianGustFactor);
    }
    if (ct === 'calm-periods' && chart) {
      html += statsRow('Longest calm', formatDuration(chart.longestCalmMin));
      html += statsRow('Mean calm', formatDuration(chart.meanCalmMin));
      html += statsRow('Calms/day', chart.calmsPerDay);
    }
    if (ct === 'ventilation-availability' && chart) {
      html += statsRow('Effective %', chart.effectivePct + '%');
    }
    if (ct === 'wind-category-dist') {
      const sysNames = {beaufort:'Beaufort (WMO/1805)', lawson:'Lawson 2001', davenport:'Davenport 1975', custom:'Custom'};
      html += statsRow('Classification', sysNames[state.windCatSystem||'beaufort']||'');
      if (_computedChart && _computedChart.total) html += statsRow('Readings', _computedChart.total);
    }
  } else if (ct.startsWith('solar') || ct === 'daily-insolation' || ct === 'diurnal-solar' || ct === 'avg-solar-profiles' || ct === 'clearness-index' || ct === 'peak-solar-hours') {
    html += statsRow('Mean daytime W/m\u00b2', ss.meanDaytimeIrradiance);
    html += statsRow('Max radiation', ss.maxRadiation + ' W/m\u00b2');
    html += statsRow('High radiation %', ss.highRadiationPct + '%');
    html += statsRow('Mean insolation', ss.meanDailyInsolation + ' kWh/m\u00b2/day');
    html += statsRow('Mean Kt', ss.meanClearnessIndex);
    html += statsRow('Mean PSH', ss.meanPeakSolarHours + ' h');
    html += statsRow('Mean daylight', ss.meanDaytimeHours + ' h');
    if (ct === 'clearness-index' && chart) {
      html += statsRow('Clear days', chart.clearPct + '%');
      html += statsRow('Partly cloudy', chart.partlyCloudyPct + '%');
      html += statsRow('Overcast', chart.overcastPct + '%');
    }
    if (ct === 'solar-distribution' && chart) {
      html += statsRow('Modal bin', chart.modalBin);
    }
  } else if (ct.startsWith('cumulative') || ct.startsWith('daily-rain') || ct.startsWith('rainfall') || ct === 'diurnal-rainfall' || ct === 'avg-rainfall-profiles' || ct === 'dry-spells' || ct === 'rain-events') {
    html += statsRow('Total rainfall', ps.totalRainfall + ' mm');
    html += statsRow('Rainy days', ps.rainyDays + ' / ' + ps.totalDays);
    html += statsRow('Mean daily (rainy)', ps.meanDailyRainy + ' mm');
    html += statsRow('Max daily', ps.maxDailyRainfall + ' mm');
    html += statsRow('Median intensity', ps.medianIntensity + ' mm/h');
    html += statsRow('95th pctl intensity', ps.p95Intensity + ' mm/h');
    html += statsRow('Max intensity', ps.maxIntensity + ' mm/h');
    html += statsRow('Rain events', ps.eventCount);
    html += statsRow('Events/week', ps.eventsPerWeek);
    if (ct === 'dry-spells' && chart) {
      html += statsRow('Longest dry', Math.round(chart.longestDryH) + ' h');
      html += statsRow('Mean dry', chart.meanDryH + ' h');
    }
    if (ct === 'avg-rainfall-profiles' && chart) {
      html += statsRow('Peak hour', chart.peakHour + ':00 EAT');
    }
  } else if (isCrossChart(ct)) {
    html += statsRow('Rain+wind %', cs.rainWithWindPct + '%');
    html += statsRow('Ventilation window', cs.ventilationWindowPct + '%');
    if (ct === 'driving-rain' && chart) {
      html += statsRow('Dominant DRI dir', chart.dominantDir);
      if (chart.facadeDRI) {
        html += statsRow('N facade DRI', chart.facadeDRI.N);
        html += statsRow('E facade DRI', chart.facadeDRI.E);
        html += statsRow('S facade DRI', chart.facadeDRI.S);
        html += statsRow('W facade DRI', chart.facadeDRI.W);
      }
    }
    if (ct === 'solar-wind' && chart) {
      html += statsRow('Correlation (r)', chart.correlation);
    }
    if (ct === 'ventilation-windows' && chart) {
      html += statsRow('Effective %', chart.effectivePct + '%');
      html += statsRow('Marginal %', chart.marginalPct + '%');
      html += statsRow('Closed %', chart.closedPct + '%');
    }
    if (ct === 'pre-storm' && chart) {
      html += statsRow('Events analysed', chart.eventCount);
    }
  }

  content.innerHTML = html;
  panel.classList.toggle('hidden', !html);
}

function isCrossChart(ct) {
  return ct === 'driving-rain' || ct === 'wind-rain' || ct === 'solar-wind' || ct === 'pre-storm' || ct === 'ventilation-windows';
}

function statsRow(label, value) {
  return '<div class="stats-row"><span class="stats-label">' + label + '</span><span class="stats-value">' + value + '</span></div>';
}

// ── Wind Unit Helpers ─────────────────────────────────────────────────────────
// Exact conversion factors (WMO Beaufort definition):
//   1 kn = 463/250 km/h  (exact)   1 kn = 463/900 m/s  (exact)
// All conversions are from km/h (sensor native unit) using these ratios.
// No premature rounding — full IEEE 754 double precision is preserved.
const _KPH_TO_MS = 250 / 900;   // = 5/18 = 1/3.6  (kph × 250/463 × 463/900 = kph × 250/900)
const _KPH_TO_KN = 250 / 463;   // exact WMO ratio
function wToUnit(kph) {
  if (kph == null) return null;
  if (state.windUnit === 'ms') return kph * _KPH_TO_MS;
  if (state.windUnit === 'kn') return kph * _KPH_TO_KN;
  return kph;
}
function wLabel() {
  if (state.windUnit === 'ms') return 'm/s';
  if (state.windUnit === 'kn') return 'kn';
  return 'km/h';
}
function setWindUnit(unit) {
  state.windUnit = unit;
  ['kmh', 'ms', 'kn'].forEach(u => document.getElementById('wu-' + u).classList.toggle('active', u === unit));
  updatePlot();
}
function _customUnitToKph(val, unit) {
  if (unit === 'ms') return val * 3.6;
  if (unit === 'kn') return val * (463/250);
  return val; // kmh
}
function _customUnitLabel(unit) {
  if (unit === 'ms') return 'm/s';
  if (unit === 'kn') return 'kn';
  return 'km/h';
}
function setWindCatCustomUnit(unit) {
  state.windCatCustomUnit = unit;
  _renderCustomEditor();
}

function updateWindSeries() { updatePlot(); }

// ── Raw-Data Recomputation ────────────────────────────────────────────────────

const _C16 = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
const _WB = [0,5,10,15,20,999];
const _WL = ["0-5","5-10","10-15","15-20","20+"];
const _WL_MS = ["0-1.4","1.4-2.8","2.8-4.2","4.2-5.6","5.6+"];
const _WL_KN = ["0-2.7","2.7-5.4","5.4-8.1","8.1-10.8","10.8+"];
const _WC = ["#4575b4","#91bfdb","#fee090","#fc8d59","#d73027"];

function _cBin(deg) {
  if (deg == null) return null;
  const d = ((deg % 360) + 360) % 360;
  if (d >= 348.75 || d < 11.25) return "N";
  return _C16[Math.min(Math.floor((d - 11.25) / 22.5) + 1, 15)];
}

function filterRaw(start, end) {
  const r = ALL_DATA.raw;
  if (!r) return null;
  const out = {ts:[],avgWind:[],peakWind:[],windDir:[],solar:[],precipRate:[],precipIncr:[]};
  for (let i = 0; i < r.ts.length; i++) {
    if (r.ts[i] >= start && r.ts[i] <= end) {
      out.ts.push(r.ts[i]); out.avgWind.push(r.avgWind[i]);
      out.peakWind.push(r.peakWind[i]); out.windDir.push(r.windDir[i]);
      out.solar.push(r.solar[i]); out.precipRate.push(r.precipRate[i]);
      out.precipIncr.push(r.precipIncr[i]);
    }
  }
  return out.ts.length ? out : null;
}

function _median(a) { const s=a.slice().sort((x,y)=>x-y),m=s.length>>1; return s.length%2?s[m]:(s[m-1]+s[m])/2; }
function _pctile(a,p) { const s=a.slice().sort((x,y)=>x-y),i=(p/100)*(s.length-1),lo=Math.floor(i),hi=Math.ceil(i); return s[lo]+(s[hi]-s[lo])*(i-lo); }

let _computedChart = null;

function _computeStats(raw) {
  const spd = raw.avgWind.filter(v=>v!=null);
  const gusts = raw.peakWind.filter(v=>v!=null);
  const ws = spd.length ? {
    meanSpeed: Math.round(spd.reduce((a,b)=>a+b,0)/spd.length*10)/10,
    maxSpeed: Math.round(Math.max(...spd)*10)/10,
    maxGust: gusts.length ? Math.round(Math.max(...gusts)*10)/10 : 0,
    calmPct: Math.round(spd.filter(v=>v===0).length/spd.length*1000)/10,
    medianSpeed: Math.round(_median(spd)*10)/10,
    p95Speed: Math.round(_pctile(spd,95)*10)/10,
    prevailingDir: (()=>{
      const cnt={};
      raw.avgWind.forEach((v,i)=>{ if(v>0){ const d=_cBin(raw.windDir[i]); if(d) cnt[d]=(cnt[d]||0)+1; }});
      return Object.entries(cnt).sort((a,b)=>b[1]-a[1])[0]?.[0]||'N/A';
    })(),
  } : ALL_DATA.stats.wind;

  const sol = raw.solar.filter(v=>v!=null);
  const dt = sol.filter(v=>v>0);
  let meanIns=0, meanDtH=0;
  if (raw.ts.length) {
    const dInsol={}, dH={};
    raw.ts.forEach((t,i)=>{ if(raw.solar[i]==null) return;
      const d=eatDate(t), k=d.getUTCFullYear()*10000+(d.getUTCMonth()+1)*100+d.getUTCDate();
      if(!dInsol[k]) dInsol[k]=[];
      dInsol[k].push({t,v:raw.solar[i]});
      if(raw.solar[i]>0) dH[k]=(dH[k]||0)+1;
    });
    const insols=Object.values(dInsol).map(pts=>{ pts.sort((a,b)=>a.t-b.t); let wh=0;
      for(let i=1;i<pts.length;i++) wh+=(pts[i].v+pts[i-1].v)/2*(pts[i].t-pts[i-1].t)/3600000;
      return wh/1000; });
    meanIns=insols.length?Math.round(insols.reduce((a,b)=>a+b,0)/insols.length*100)/100:0;
    const hs=Object.values(dH);
    meanDtH=hs.length?Math.round(hs.reduce((a,b)=>a+b,0)/hs.length/12*10)/10:0;
  }
  const ss = sol.length ? {
    meanDaytimeIrradiance: dt.length?Math.round(dt.reduce((a,b)=>a+b,0)/dt.length*10)/10:0,
    maxRadiation: Math.round(Math.max(...sol)*10)/10,
    highRadiationPct: dt.length?Math.round(dt.filter(v=>v>500).length/dt.length*1000)/10:0,
    meanDailyInsolation: meanIns, meanClearnessIndex: ALL_DATA.stats.solar.meanClearnessIndex,
    meanPeakSolarHours: meanIns, meanDaytimeHours: meanDtH,
  } : ALL_DATA.stats.solar;

  const incr = raw.precipIncr.filter(v=>v!=null);
  let ps = ALL_DATA.stats.precipitation;
  if (incr.length) {
    const byDay={};
    raw.ts.forEach((t,i)=>{ if(raw.precipIncr[i]==null) return;
      const d=eatDate(t), k=d.getUTCFullYear()*10000+(d.getUTCMonth()+1)*100+d.getUTCDate();
      byDay[k]=(byDay[k]||0)+raw.precipIncr[i]; });
    const tots=Object.values(byDay), rates=raw.precipRate.filter(v=>v!=null&&v>0);
    ps = { totalRainfall:Math.round(tots.reduce((a,b)=>a+b,0)*10)/10, rainyDays:tots.filter(v=>v>0.2).length,
      totalDays:tots.length, meanDailyRainy:0,
      maxDailyRainfall:tots.length?Math.round(Math.max(...tots)*10)/10:0,
      medianIntensity:rates.length?Math.round(_median(rates)*10)/10:0,
      p95Intensity:rates.length?Math.round(_pctile(rates,95)*10)/10:0,
      maxIntensity:rates.length?Math.round(Math.max(...rates)*10)/10:0,
      eventCount:ALL_DATA.stats.precipitation.eventCount,
      eventsPerWeek:ALL_DATA.stats.precipitation.eventsPerWeek };
  }
  return {ws, ss, ps, cs: ALL_DATA.stats.cross};
}

function _getStats() {
  if (!ALL_DATA.raw || state.timeMode === 'all') return {ws:ALL_DATA.stats.wind,ss:ALL_DATA.stats.solar,ps:ALL_DATA.stats.precipitation,cs:ALL_DATA.stats.cross};
  const {start,end} = getTimeRange();
  const raw = filterRaw(start, end);
  return raw ? _computeStats(raw) : {ws:ALL_DATA.stats.wind,ss:ALL_DATA.stats.solar,ps:ALL_DATA.stats.precipitation,cs:ALL_DATA.stats.cross};
}

function _buildWindRose(raw) {
  const total=raw.avgWind.filter(v=>v!=null).length, calm=raw.avgWind.filter(v=>v===0).length;
  const calmPct=total?Math.round(calm/total*1000)/10:0;
  const binLabels = state.windUnit === 'ms' ? _WL_MS : state.windUnit === 'kn' ? _WL_KN : _WL;
  const traces=binLabels.map((lbl,li)=>{
    const lo=_WB[li],hi=_WB[li+1],cnt={};_C16.forEach(d=>cnt[d]=0);
    raw.avgWind.forEach((v,i)=>{ if(v==null||v<=0||v<lo||v>=hi) return; const d=_cBin(raw.windDir[i]); if(d) cnt[d]++; });
    return {type:'barpolar',r:_C16.map(d=>total?Math.round(cnt[d]/total*10000)/100:0),theta:_C16,name:lbl+' '+wLabel(),marker:{color:_WC[li]}};
  });
  return {data:traces, calmPct,
    layout:{polar:{angularaxis:{direction:'clockwise',rotation:90,tickmode:'array',tickvals:Array.from({length:16},(_,i)=>i*22.5),ticktext:_C16},radialaxis:{ticksuffix:'%',angle:45}},barmode:'stack',bargap:0,showlegend:true,legend:{x:1.1,y:1}}};
}

function _buildWindDist(raw) {
  const spd=raw.avgWind.filter(v=>v!=null); if(!spd.length) return null;
  const maxS=Math.min(Math.max(...spd)+1,50),step=0.5,bins=[];
  for(let b=0;b<maxS+step;b+=step) bins.push(b);
  const cnts=new Array(bins.length-1).fill(0),ctrs=bins.slice(0,-1).map((b,i)=>Math.round((b+bins[i+1])/2*10)/10);
  spd.forEach(v=>{ const j=Math.min(Math.floor(v/step),cnts.length-1); cnts[j]++; });
  const xVals = ctrs.map(c => wToUnit(c));
  return {data:[{type:'bar',name:'Frequency',x:xVals,y:cnts,marker:{color:'#1f77b4'}}], calmCount:spd.filter(v=>v===0).length,
    layout:{xaxis:{title:'Wind Speed (' + wLabel() + ')'},yaxis:{title:'Count'},showlegend:true,bargap:0.05}};
}

function _buildCalmPeriods(raw) {
  const BE=[0,5,30,60,180,360,720,1440,99999],BL=['<5min','5-30min','30min-1h','1-3h','3-6h','6-12h','12-24h','24h+'];
  const cnts=new Array(BL.length).fill(0),durs=[];
  let inC=false,si=0;
  raw.avgWind.forEach((v,i)=>{ if(v===0&&!inC){inC=true;si=i;} else if(v!==0&&inC){inC=false;durs.push((raw.ts[i-1]-raw.ts[si])/60000);} });
  if(inC&&raw.ts.length) durs.push((raw.ts[raw.ts.length-1]-raw.ts[si])/60000);
  durs.forEach(d=>{ for(let j=0;j<BE.length-1;j++) if(d>=BE[j]&&d<BE[j+1]){cnts[j]++;break;} });
  const longest=durs.length?Math.round(Math.max(...durs)*10)/10:0;
  const meanD=durs.length?Math.round(durs.reduce((a,b)=>a+b,0)/durs.length*10)/10:0;
  const totDays=raw.ts.length>1?(raw.ts[raw.ts.length-1]-raw.ts[0])/86400000:1;
  return {data:[{type:'bar',orientation:'h',name:'Calm Periods',y:BL,x:cnts,marker:{color:'#999'}}],
    longestCalmMin:longest, meanCalmMin:meanD, calmsPerDay:Math.round(durs.length/totDays*10)/10,
    layout:{xaxis:{title:'Number of Periods'},yaxis:{title:'Duration',autorange:'reversed'}}};
}

function _buildSolarDist(raw) {
  const s=raw.solar.filter(v=>v!=null&&v>0); if(!s.length) return null;
  const SZ=50,n=Math.ceil(Math.max(...s)/SZ),ctrs=Array.from({length:n},(_,i)=>(i+0.5)*SZ);
  const cnts=new Array(n).fill(0);
  s.forEach(v=>{ cnts[Math.min(Math.floor(v/SZ),n-1)]++; });
  const cols=ctrs.map(c=>c<200?'#4575b4':c<500?'#fee090':c<800?'#fc8d59':'#d73027');
  return {data:[{type:'bar',name:'Frequency',x:ctrs,y:cnts,marker:{color:cols}}],
    modalBin:ctrs[cnts.indexOf(Math.max(...cnts))],
    layout:{xaxis:{title:'Solar Irradiance (W/m\u00b2)'},yaxis:{title:'Count'},bargap:0.05}};
}

function _buildDrivingRain(raw) {
  const dri={};_C16.forEach(d=>dri[d]=0); let any=false;
  raw.ts.forEach((_,i)=>{
    const w=raw.avgWind[i],r=raw.precipRate[i],d=raw.windDir[i];
    if(!w||!r||w<=0||r<=0||d==null) return;
    const v=_cBin(d); if(!v) return;
    dri[v]+=(w/3.6)*Math.pow(r,8/9); any=true;
  });
  if(!any) return null;
  const vals=_C16.map(d=>Math.round(dri[d]*100)/100);
  const facadeDRI={N:0,E:0,S:0,W:0};
  raw.ts.forEach((_,i)=>{
    const w=raw.avgWind[i],r=raw.precipRate[i],d=raw.windDir[i];
    if(!w||!r||w<=0||r<=0||d==null) return;
    const dv=(w/3.6)*Math.pow(r,8/9);
    [{f:'N',deg:0},{f:'E',deg:90},{f:'S',deg:180},{f:'W',deg:270}].forEach(({f,deg})=>{
      const c=Math.cos((d-deg)*Math.PI/180); if(c>0) facadeDRI[f]+=dv*c;
    });
  });
  Object.keys(facadeDRI).forEach(k=>facadeDRI[k]=Math.round(facadeDRI[k]*10)/10);
  return {data:[{type:'barpolar',r:vals,theta:_C16,name:'DRI',marker:{color:'#1f77b4'}}],
    dominantDir:_C16[vals.indexOf(Math.max(...vals))], facadeDRI,
    layout:{polar:{angularaxis:{direction:'clockwise',rotation:90},radialaxis:{visible:true}}}};
}

function _buildWindRainCoincidence(raw) {
  const WE = state.windUnit === 'ms' ? [0,0.1,0.3,0.6,0.8,1.0,1.4,1.9,2.8,3.9,5.6,999]
           : state.windUnit === 'kn'  ? [0,0.5,1,2,3,4,5,6,8,11,16,999]
           :                            [0,0.5,1,1.5,2,3,4,5,7,10,14,18,25,999];
  const RE=[0,0.2,0.5,1,1.5,2,3,5,8,12,20,999];
  const mkL=edges=>edges.slice(0,-1).map((lo,i)=>edges[i+1]===999?`${lo}+`:lo===Math.floor(lo)&&edges[i+1]===Math.floor(edges[i+1])?`${Math.floor(lo)}-${Math.floor(edges[i+1])}`:`${lo}-${edges[i+1]}`);
  const wl=mkL(WE),rl=mkL(RE);
  const z=Array.from({length:rl.length},()=>new Array(wl.length).fill(0));
  raw.ts.forEach((_,i)=>{
    const wKph=raw.avgWind[i],r=raw.precipRate[i]; if(wKph==null||r==null||r<=0) return;
    const w = wToUnit(wKph);
    const wi=WE.findIndex((e,j)=>j<WE.length-1&&w>=e&&w<WE[j+1]);
    const ri=RE.findIndex((e,j)=>j<RE.length-1&&r>=e&&r<RE[j+1]);
    if(wi>=0&&ri>=0) z[ri][wi]++;
  });
  let nW=wl.length; while(nW>1&&z.every(row=>row[nW-1]===0)) nW--;
  let nR=rl.length; while(nR>1&&z[nR-1].slice(0,nW).every(v=>v===0)) nR--;
  const zt=z.slice(0,nR).map(r=>r.slice(0,nW)),wlT=wl.slice(0,nW),rlT=rl.slice(0,nR);
  wlT[nW-1]=WE[nW-1]+'+'; rlT[nR-1]=RE[nR-1]+'+';
  return {data:[{type:'heatmap',x:wlT,y:rlT,z:zt,colorscale:'YlOrRd',colorbar:{title:'Count'}}],
    layout:{xaxis:{title:'Wind Speed (' + wLabel() + ')'},yaxis:{title:'Rain Rate (mm/h)'}}};
}

function _buildSolarWind(raw) {
  const pts=[];
  raw.ts.forEach((_,i)=>{ if(raw.solar[i]==null||raw.solar[i]<=0||raw.avgWind[i]==null) return;
    pts.push({x:raw.solar[i],y:raw.avgWind[i],h:eatDate(raw.ts[i]).getUTCHours()}); });
  if(!pts.length) return null;
  const s=pts.length>5000?pts.filter((_,i)=>i%Math.ceil(pts.length/5000)===0):pts;
  const n=s.length,xs=s.map(p=>p.x),ys=s.map(p=>p.y);
  const mx=xs.reduce((a,b)=>a+b,0)/n,my=ys.reduce((a,b)=>a+b,0)/n;
  const num=xs.reduce((v,x,i)=>v+(x-mx)*(ys[i]-my),0);
  const dx=Math.sqrt(xs.reduce((v,x)=>v+(x-mx)**2,0)),dy=Math.sqrt(ys.reduce((v,y)=>v+(y-my)**2,0));
  return {data:[{type:'scatter',mode:'markers',name:'Readings',x:s.map(p=>p.x),y:s.map(p=>wToUnit(p.y)),
    marker:{color:s.map(p=>p.h),colorscale:'Viridis',colorbar:{title:'Hour'},size:3,opacity:0.4}}],
    correlation:dx&&dy?Math.round(num/(dx*dy)*1000)/1000:0,
    layout:{xaxis:{title:'Solar Radiation (W/m\u00b2)'},yaxis:{title:'Wind Speed (' + wLabel() + ')'}}};
}

function _buildVentWindows(raw) {
  const byD={};
  raw.ts.forEach((t,i)=>{
    const d=eatDate(t),k=d.getUTCFullYear()*10000+(d.getUTCMonth()+1)*100+d.getUTCDate();
    const lbl=d.getUTCFullYear()+'-'+String(d.getUTCMonth()+1).padStart(2,'0')+'-'+String(d.getUTCDate()).padStart(2,'0');
    if(!byD[k]) byD[k]={lbl,hrs:{}};
    const h=d.getUTCHours(); if(!byD[k].hrs[h]) byD[k].hrs[h]={w:[],r:[]};
    if(raw.avgWind[i]!=null) byD[k].hrs[h].w.push(raw.avgWind[i]);
    if(raw.precipRate[i]!=null) byD[k].hrs[h].r.push(raw.precipRate[i]);
  });
  const keys=Object.keys(byD).sort(),yLbl=keys.map(k=>byD[k].lbl),hrs=Array.from({length:24},(_,i)=>i);
  const z=keys.map(k=>hrs.map(h=>{
    const hr=byD[k].hrs[h]; if(!hr||!hr.w.length) return 0;
    const mw=hr.w.reduce((a,b)=>a+b,0)/hr.w.length,mr=hr.r.length?Math.max(...hr.r):0;
    return mr>=2.5?3:mw>=3.5&&mr===0?1:2;
  }));
  const flat=z.flat().filter(v=>v>0),tot=flat.length;
  const cs=[[0,'#e0e0e0'],[0.33,'#2ca02c'],[0.67,'#ffbf00'],[1.0,'#d62728']];
  return {data:[{type:'heatmap',x:hrs,y:yLbl,z:z,colorscale:cs,zmin:0,zmax:3,showscale:false}],
    effectivePct:tot?Math.round(flat.filter(v=>v===1).length/tot*1000)/10:0,
    marginalPct:tot?Math.round(flat.filter(v=>v===2).length/tot*1000)/10:0,
    closedPct:tot?Math.round(flat.filter(v=>v===3).length/tot*1000)/10:0,
    layout:{xaxis:{title:'Hour of Day (EAT)',dtick:1},yaxis:{title:'Date',autorange:'reversed'}}};
}

// ── Wind Category Distribution ───────────────────────────────────────────────
const _CAT_PALETTE = ['#313695','#4575b4','#74add1','#abd9e9','#e0f3f8','#fee090','#fdae61','#f46d43','#d73027','#a50026'];
function _catColors(n) {
  if (n <= 1) return [_CAT_PALETTE[4]];
  if (n >= _CAT_PALETTE.length) return _CAT_PALETTE.slice(0, n);
  const step = (_CAT_PALETTE.length - 1) / (n - 1);
  return Array.from({length: n}, (_, i) => _CAT_PALETTE[Math.round(i * step)]);
}
const _DEFAULT_CUSTOM_BANDS = [
  {label:'Sitting', hi_val:4}, {label:'Standing', hi_val:6},
  {label:'Strolling', hi_val:8}, {label:'Business Walking', hi_val:10},
  {label:'Uncomfortable', hi_val:null}
];
const _DENOM_F = {
  'hours-day':24, 'hours-year':24*365.25,
  'days-week':7, 'days-month':30.44, 'days-year':365.25,
  'weeks-year':52.18, 'months-year':12,
};
// Valid cycle options per value unit
const _VALID_CYCLES = {
  hours:['day','year'], days:['week','month','year'], weeks:['year'], months:['year'],
};
function _getDenomKey() { return (state.windCatValueUnit||'pct')+'-'+(state.windCatPerUnit||'day'); }
function _getDenomFactor() { return _DENOM_F[_getDenomKey()] || 24; }
function _getDenomLabel() {
  const v=state.windCatValueUnit||'pct', p=state.windCatPerUnit||'day';
  if (v==='pct') return '%';
  return v[0].toUpperCase()+v.slice(1)+' per '+p;
}
function _isPercentMode() { return (state.windCatValueUnit||'pct') === 'pct'; }

function _buildWindCategoryDist(raw) {
  const KN_TO_KPH = 463/250, MS_TO_KPH = 3.6;
  const sys = state.windCatSystem || 'beaufort';
  let bands = [];

  if (sys === 'beaufort') {
    const BF = [
      {label:'Calm',           lo:0,  hi:1},   {label:'Light Air',      lo:1,  hi:4},
      {label:'Light Breeze',   lo:4,  hi:7},   {label:'Gentle Breeze',  lo:7,  hi:11},
      {label:'Moderate Breeze',lo:11, hi:17},  {label:'Fresh Breeze',   lo:17, hi:22},
      {label:'Strong Breeze',  lo:22, hi:28},  {label:'Near Gale',      lo:28, hi:34},
      {label:'Gale',           lo:34, hi:41},  {label:'Severe+',        lo:41, hi:Infinity},
    ];
    bands = BF.map(b => ({label:b.label, lo_kph:b.lo*KN_TO_KPH, hi_kph:b.hi*KN_TO_KPH}));
  } else if (sys === 'lawson') {
    const LW = [
      {label:'Sitting',lo:0,hi:4},{label:'Standing',lo:4,hi:6},{label:'Strolling',lo:6,hi:8},
      {label:'Business Walking',lo:8,hi:10},{label:'Uncomfortable',lo:10,hi:Infinity},
    ];
    bands = LW.map(b => ({label:b.label, lo_kph:b.lo*MS_TO_KPH, hi_kph:b.hi*MS_TO_KPH}));
  } else if (sys === 'davenport') {
    const DAV = [
      {label:'Long Sitting',lo:0,hi:3.6},{label:'Short Sitting',lo:3.6,hi:5.3},
      {label:'Walking Quietly',lo:5.3,hi:7.6},{label:'Walking Fast',lo:7.6,hi:9.8},
      {label:'Uncomfortable',lo:9.8,hi:Infinity},
    ];
    bands = DAV.map(b => ({label:b.label, lo_kph:b.lo*MS_TO_KPH, hi_kph:b.hi*MS_TO_KPH}));
  } else {
    const cb = state.windCatCustomBands || _DEFAULT_CUSTOM_BANDS;
    const cUnit = state.windCatCustomUnit || 'ms';
    bands = cb.map((b,i) => ({
      label: b.label || ('Band '+(i+1)),
      lo_kph: i===0 ? 0 : _customUnitToKph(cb[i-1].hi_val||0, cUnit),
      hi_kph: b.hi_val!=null ? _customUnitToKph(b.hi_val, cUnit) : Infinity,
    }));
  }

  if (!bands.length) return null;

  const isPct = _isPercentMode(), xTitle = _getDenomLabel();
  function fmtKph(kph) { return Math.round(wToUnit(kph)*10)/10; }
  function fmtRange(b) {
    if (b.hi_kph===Infinity) return fmtKph(b.lo_kph)+'+\u202f'+wLabel();
    return fmtKph(b.lo_kph)+'\u2013'+fmtKph(b.hi_kph)+'\u202f'+wLabel();
  }
  function fmtDuration(val) {
    const u = state.windCatValueUnit || 'pct';
    if (u === 'pct') return val + '%';
    let mins;
    if (u === 'hours') mins = val * 60;
    else if (u === 'days') mins = val * 1440;
    else if (u === 'weeks') mins = val * 10080;
    else if (u === 'months') mins = val * 43830;
    else return val;
    const mo = Math.floor(mins / 43830); mins -= mo * 43830;
    const wk = Math.floor(mins / 10080); mins -= wk * 10080;
    const d = Math.floor(mins / 1440); mins -= d * 1440;
    const h = Math.floor(mins / 60);
    const m = Math.round(mins - h * 60);
    const parts = [];
    if (mo) parts.push(mo + (mo===1?' month':' months'));
    if (wk) parts.push(wk + (wk===1?' week':' weeks'));
    if (d) parts.push(d + (d===1?' day':' days'));
    if (h) parts.push(h + (h===1?' hr':' hrs'));
    if (m) parts.push(m + ' min');
    return parts.length ? parts.join(', ') : '0 min';
  }

  const showAvg = document.getElementById('cb-wind-avg').checked;
  const showGust = document.getElementById('cb-wind-gust').checked;
  const series = [];
  if (showAvg) series.push({arr: raw.avgWind, name: 'Average', color: '#1f77b4'});
  if (showGust) series.push({arr: raw.peakWind, name: 'Peak Gust', color: '#ff7f0e'});
  if (!series.length) series.push({arr: raw.avgWind, name: 'Average', color: '#1f77b4'});

  const rangeText = bands.map(b => fmtRange(b));
  const traceData = [];
  let grandTotal = 0;

  series.forEach(s => {
    const counts = new Array(bands.length).fill(0);
    let total = 0;
    s.arr.forEach(v => {
      if (v==null) return; total++;
      for (let i=0; i<bands.length; i++) {
        if (v >= bands[i].lo_kph && v < bands[i].hi_kph) { counts[i]++; break; }
      }
    });
    grandTotal += total;
    const xVals = isPct
      ? counts.map(c => Math.round(c/Math.max(total,1)*10000)/100)
      : counts.map(c => Math.round(c/Math.max(total,1)*_getDenomFactor()*100)/100);
    const hover = bands.map((b,i) => {
      const durStr = isPct ? xVals[i]+'%' : fmtDuration(xVals[i]);
      return '<b>'+b.label+'</b> ('+s.name+')<br>Range: '+fmtRange(b)+'<br>Count: '+counts[i]+'<br>'+xTitle+': '+durStr+'<extra></extra>';
    });
    traceData.push({
      type:'bar', orientation:'h', name: s.name,
      y: bands.map(b=>b.label), x: xVals,
      marker: {color: series.length === 1 ? _catColors(bands.length) : s.color},
      hovertemplate: hover, customdata: counts,
      text: series.length === 1 ? rangeText : null,
      textposition: 'outside',
      textfont: {size: 10, color: '#555'},
      cliponaxis: false,
    });
  });

  return {
    data: traceData,
    layout: {
      xaxis: {title: xTitle},
      yaxis: {autorange:'reversed', title:'', automargin: true},
      showlegend: series.length > 1,
      barmode: 'group',
      margin: {l: 10, r: 150, t: 30, b: 50},
    },
    total: grandTotal,
  };
}

function setWindCatSystem(sys) {
  state.windCatSystem = sys;
  const sel = document.getElementById('wind-cat-system');
  if (sel) sel.value = sys;
  document.getElementById('wind-cat-custom-section').style.display = sys==='custom' ? '' : 'none';
  if (sys==='custom' && !state.windCatCustomBands) {
    state.windCatCustomBands = _DEFAULT_CUSTOM_BANDS.map(b => Object.assign({},b));
  }
  updatePlot();
}

function setWindCatValueUnit(val) {
  state.windCatValueUnit = val;
  _updateWindCatCycleOptions();
  updatePlot();
}
function setWindCatPerUnit(per) {
  state.windCatPerUnit = per;
  updatePlot();
}
function _updateWindCatCycleOptions() {
  const v = state.windCatValueUnit || 'pct';
  const cycleLabel = document.getElementById('wind-cat-cycle-label');
  const cycleSel = document.getElementById('wind-cat-per-unit');
  if (v === 'pct') {
    cycleLabel.style.display = 'none';
    return;
  }
  cycleLabel.style.display = '';
  const valid = _VALID_CYCLES[v] || [];
  cycleSel.innerHTML = valid.map(c => '<option value="'+c+'">'+c[0].toUpperCase()+c.slice(1)+'</option>').join('');
  if (!valid.includes(state.windCatPerUnit)) state.windCatPerUnit = valid[0];
  cycleSel.value = state.windCatPerUnit;
}

function toggleCustomEditor() {
  const ed = document.getElementById('wind-cat-custom-editor');
  const arr = document.getElementById('wind-cat-custom-arrow');
  const showing = ed.style.display !== 'none';
  ed.style.display = showing ? 'none' : '';
  if (arr) arr.style.transform = showing ? '' : 'rotate(90deg)';
  if (!showing) _renderCustomEditor();
}

function _renderCustomEditor() {
  const el = document.getElementById('wind-cat-bands-list');
  if (!el) return;
  const bands = state.windCatCustomBands || _DEFAULT_CUSTOM_BANDS;
  const cUnit = state.windCatCustomUnit || 'ms';
  const unitSel = '<div style="margin-bottom:4px"><label style="font-size:10px;color:#666">Unit: <select onchange="setWindCatCustomUnit(this.value)" style="font-size:10px;padding:1px 3px;border:1px solid #ccc;border-radius:2px">' +
    '<option value="ms"' + (cUnit==='ms'?' selected':'') + '>m/s</option>' +
    '<option value="kmh"' + (cUnit==='kmh'?' selected':'') + '>km/h</option>' +
    '<option value="kn"' + (cUnit==='kn'?' selected':'') + '>kn</option>' +
    '</select></label></div>';
  const uLabel = _customUnitLabel(cUnit);
  el.innerHTML = unitSel + bands.map((b,i) => {
    const isLast = i===bands.length-1;
    const delBtn = bands.length>2 ? '<button onclick="removeCustomBand('+i+')" style="font-size:9px;padding:0 4px;border:1px solid #ccc;border-radius:2px;cursor:pointer;color:#888;line-height:1.4">x</button>' : '';
    const hiField = isLast ? '<em style="font-size:10px;color:#aaa">no limit</em>' :
      '<input type="number" min="0" step="0.5" value="'+(b.hi_val||'')+'" style="width:48px;font-size:10px;border:1px solid #ccc;border-radius:2px;padding:1px 3px" oninput="updateCustomBand('+i+',\'hi\',this.value)">';
    return '<div style="display:flex;align-items:center;gap:3px;margin-bottom:2px">'+
      '<input type="text" value="'+b.label+'" style="width:95px;font-size:10px;border:1px solid #ccc;border-radius:2px;padding:1px 3px" oninput="updateCustomBand('+i+',\'label\',this.value)">'+
      '<span style="font-size:10px;color:#888">\u2264</span>'+hiField+
      '<span style="font-size:10px;color:#888">'+uLabel+'</span>'+delBtn+'</div>';
  }).join('');
}

function updateCustomBand(i, field, val) {
  if (!state.windCatCustomBands) return;
  if (field==='hi') { const v=parseFloat(val); state.windCatCustomBands[i].hi_val=isNaN(v)?null:v; }
  else state.windCatCustomBands[i].label = val;
}

function addCustomBand() {
  if (!state.windCatCustomBands) state.windCatCustomBands = _DEFAULT_CUSTOM_BANDS.map(b=>Object.assign({},b));
  const bands = state.windCatCustomBands;
  if (bands.length >= 6) return;
  const last = bands.pop();
  const prevHi = bands.length ? (bands[bands.length-1].hi_val||0) : 0;
  bands.push({label:'New Band', hi_val: prevHi+5});
  bands.push(last);
  state.windCatCustomBands = bands;
  _renderCustomEditor();
}

function removeCustomBand(i) {
  if (!state.windCatCustomBands || state.windCatCustomBands.length <= 2) return;
  state.windCatCustomBands.splice(i, 1);
  _renderCustomEditor();
  updatePlot();
}

function applyCustomBands() {
  if (!state.windCatCustomBands) return;
  const finite = state.windCatCustomBands.filter(b=>b.hi_val!=null).sort((a,b)=>(a.hi_val||0)-(b.hi_val||0));
  const infBand = state.windCatCustomBands.find(b=>b.hi_val==null) || {label:'Top', hi_val:null};
  state.windCatCustomBands = [...finite, infBand];
  _renderCustomEditor();
  updatePlot();
}

const _RAW_BUILDERS = {
  'wind-rose':_buildWindRose, 'wind-distribution':_buildWindDist,
  'calm-periods':_buildCalmPeriods, 'solar-distribution':_buildSolarDist,
  'driving-rain':_buildDrivingRain, 'wind-rain':_buildWindRainCoincidence,
  'solar-wind':_buildSolarWind, 'ventilation-windows':_buildVentWindows,
  'wind-category-dist':_buildWindCategoryDist,
};

// ── Chart Rendering ──────────────────────────────────────────────────────────
// ── Time Range ────────────────────────────────────────────────────────────────
function getTimeRange() {
  const m = ALL_DATA.meta;
  const min = m && m.dateRange ? m.dateRange.min : -Infinity;
  const max = m && m.dateRange ? m.dateRange.max : Infinity;
  switch (state.timeMode) {
    case 'all':     return {start: min, end: max};
    case 'between': return {start: state.betweenStart || min, end: state.betweenEnd || max};
    case 'year': {
      const y = state.selectedYear; if (!y) return {start: min, end: max};
      return {start: Date.UTC(y, 0, 1), end: Date.UTC(y, 11, 31, 23, 59, 59, 999)};
    }
    case 'season': {
      if (!state.selectedSeason) return {start: min, end: max};
      const {year: y, season: si} = state.selectedSeason;
      const sm = [[0,1],[2,4],[5,9],[10,11]][si];
      return {start: Date.UTC(y, sm[0], 1), end: Date.UTC(y, sm[1]+1, 0, 23, 59, 59, 999)};
    }
    case 'month': {
      if (!state.selectedMonth) return {start: min, end: max};
      const {year: y, month: mo} = state.selectedMonth;
      return {start: Date.UTC(y, mo-1, 1), end: Date.UTC(y, mo, 0, 23, 59, 59, 999)};
    }
    case 'week': {
      if (!state.selectedWeek) return {start: min, end: max};
      const {year: y, week: w} = state.selectedWeek;
      const jan4 = new Date(Date.UTC(y, 0, 4));
      const dow = jan4.getUTCDay() || 7;
      const weekStart = jan4.getTime() - (dow-1)*86400000 + (w-1)*7*86400000;
      return {start: weekStart, end: weekStart + 7*86400000 - 1};
    }
    case 'day': {
      const ts = state.selectedDay; if (!ts) return {start: min, end: max};
      return {start: ts, end: ts + 86400000 - 1};
    }
    default: return {start: min, end: max};
  }
}

function updatePlot() {
  const ct = state.chartType;
  const chart = getChartById(ct);
  if (!chart) return;

  const chartEl = document.getElementById('chart');
  const sel = document.getElementById('chart-select');
  const titleEl = document.getElementById('bar-title');
  titleEl.textContent = currentLang === 'sw' ? (chart.title_sw || chart.title) : chart.title;

  const config = {responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['zoom2d','pan2d','select2d','lasso2d','zoomIn2d','zoomOut2d','resetScale2d','sendDataToCloud','hoverClosestCartesian','hoverCompareCartesian','toggleSpikelines','toImage']};

  // Pre-compute filtered chart for aggregated chart types (needed by stats panel)
  _computedChart = null;
  if (_RAW_BUILDERS[ct] && ALL_DATA.raw) {
    // wind-category-dist always rebuilds from raw (system/denom switching needs it in all time modes)
    const alwaysCompute = ct === 'wind-category-dist';
    if (alwaysCompute || state.timeMode !== 'all') {
      const {start, end} = getTimeRange();
      const raw = (alwaysCompute && state.timeMode === 'all') ? ALL_DATA.raw : filterRaw(start, end);
      if (raw) _computedChart = _RAW_BUILDERS[ct](raw) || null;
    }
  }

  updateSidebarControls();
  updateStatsPanel();

  document.getElementById('chart').style.display = '';

  // Handle rain events table
  if (ct === 'rain-events') {
    renderRainEventsTable(chart);
    return;
  }

  // Periodic averages rendered from raw timeseries data
  if (ct === 'avg-wind-profiles' || ct === 'avg-solar-profiles' || ct === 'avg-rainfall-profiles') {
    const result = renderPeriodicAverages();
    Plotly.react(chartEl, result.traces, result.layout, config);
    return;
  }

  // Render pre-computed aggregated chart from filtered raw data
  if (_computedChart) {
    const _cLayout = Object.assign({}, _computedChart.layout || {});
    if (_cLayout.xaxis) _cLayout.xaxis = Object.assign({}, _cLayout.xaxis);
    if (_cLayout.yaxis) _cLayout.yaxis = Object.assign({}, _cLayout.yaxis);
    const layout = Object.assign({}, chart.layout || {}, _cLayout);
    layout.margin = layout.margin || {l: 60, r: 40, t: 30, b: 50};
    layout.autosize = true;
    layout.font = {family: 'Ubuntu, sans-serif', size: 12};
    if (ct === 'wind-rose' && _computedChart.calmPct !== undefined) {
      layout.annotations = layout.annotations || [];
      layout.annotations.push({x:0.5,y:0.5,xref:'paper',yref:'paper',text:'Calm: '+_computedChart.calmPct+'%',showarrow:false,font:{size:14,color:'#666',family:'Ubuntu'}});
    }
    Plotly.react(chartEl, _computedChart.data, layout, config);
    state.savedZoom = null;
    return;
  }

  // Build Plotly traces
  const traces = [];
  const chartData = chart.data || [];
  const {start: rngStart, end: rngEnd} = getTimeRange();

  for (const trace of chartData) {
    const t = Object.assign({}, trace);

    // Convert x_ms timestamps to EAT strings, applying time range filter
    if (t.x_ms) {
      const xms = t.x_ms;
      const keys = Object.keys(t).filter(k => k !== 'x_ms' && Array.isArray(t[k]) && t[k].length === xms.length);
      const mask = xms.map(ms => ms >= rngStart && ms <= rngEnd);
      t.x = xms.filter((_, i) => mask[i]).map(ms => toEATString(ms));
      keys.forEach(k => { t[k] = t[k].filter((_, i) => mask[i]); });
      delete t.x_ms;
    }

    // For gust factor, use x_speed for x axis
    if (ct === 'gust-factor' && t.x_speed) {
      t.x = t.x_speed;
      delete t.x_speed;
      delete t.x_ms;
    }

    traces.push(t);
  }

  // Build layout
  const layout = Object.assign({}, chart.layout || {});
  layout.margin = layout.margin || {l: 60, r: 40, t: 30, b: 50};
  layout.autosize = true;
  layout.font = {family: 'Ubuntu, sans-serif', size: 12};

  // Add season boundaries for time series charts
  if (chart.seasonBoundaries && chart.seasonBoundaries.length > 0) {
    layout.shapes = layout.shapes || [];
    layout.annotations = layout.annotations || [];
    for (const sb of chart.seasonBoundaries) {
      const xval = toEATString(sb.ts);
      layout.shapes.push({
        type: 'line', xref: 'x', yref: 'paper',
        x0: xval, x1: xval, y0: 0, y1: 1,
        line: {color: '#ccc', width: 1, dash: 'dot'}
      });
      layout.annotations.push({
        x: xval, y: 1.02, yref: 'paper',
        text: sb.label, showarrow: false,
        font: {size: 9, color: '#999'}
      });
    }
  }

  // Wind rose calm annotation
  if (ct === 'wind-rose' && chart.calmPct !== undefined) {
    layout.annotations = layout.annotations || [];
    layout.annotations.push({
      x: 0.5, y: 0.5, xref: 'paper', yref: 'paper',
      text: 'Calm: ' + chart.calmPct + '%',
      showarrow: false,
      font: {size: 14, color: '#666', family: 'Ubuntu'},
    });
  }

  // ── Wind unit conversion for pre-computed traces ──────────────────────────
  // (Only reached when _computedChart is null, i.e. timeMode==='all' or no raw data)
  // Clone nested axis objects before modifying to avoid mutating embedded chart data.
  if (layout.xaxis) layout.xaxis = Object.assign({}, layout.xaxis);
  if (layout.yaxis) layout.yaxis = Object.assign({}, layout.yaxis);
  const _needsConv = state.windUnit !== 'kmh';
  const _cvt = v => v != null ? wToUnit(v) : null;
  if (ct === 'wind-rose') {
    const binLabels = state.windUnit === 'ms' ? _WL_MS : state.windUnit === 'kn' ? _WL_KN : _WL;
    traces.forEach((tr, i) => { if (i < binLabels.length) tr.name = binLabels[i] + ' ' + wLabel(); });
  } else if (ct === 'wind-timeseries') {
    if (_needsConv) traces.forEach(tr => { if (tr.y) tr.y = tr.y.map(_cvt); });
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
    // Apply series visibility from checkboxes
    const showAvg = document.getElementById('cb-wind-avg').checked;
    const showGust = document.getElementById('cb-wind-gust').checked;
    const show24h = document.getElementById('cb-wind-24h').checked;
    if (traces[0]) traces[0].visible = showAvg;
    if (traces[1]) traces[1].visible = showGust;
    if (traces[2]) traces[2].visible = show24h;
  } else if (ct === 'diurnal-wind') {
    // Traces 0-2: wind speed (mean, +1SD, -1SD); trace 3: Calm % on y2
    if (_needsConv) traces.slice(0, 3).forEach(tr => { if (tr.y) tr.y = tr.y.map(_cvt); });
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
  } else if (ct === 'wind-distribution') {
    if (_needsConv) traces.forEach(tr => { if (tr.x) tr.x = tr.x.map(v => wToUnit(v)); });
    if (layout.xaxis) layout.xaxis.title = 'Wind Speed (' + wLabel() + ')';
  } else if (ct === 'gust-factor') {
    if (_needsConv) {
      traces.forEach(tr => { if (tr.x) tr.x = tr.x.map(v => wToUnit(v)); });
      if (layout.shapes) layout.shapes = layout.shapes.map(s => {
        if (s.x0 != null && s.x1 != null) return Object.assign({}, s, {x0: wToUnit(s.x0), x1: wToUnit(s.x1)});
        return s;
      });
    }
    if (layout.xaxis) layout.xaxis.title = 'Average Wind Speed (' + wLabel() + ')';
  } else if (ct === 'pre-storm') {
    if (_needsConv && traces[0] && traces[0].y) traces[0].y = traces[0].y.map(_cvt);
    if (traces[0]) traces[0].name = 'Wind Speed (' + wLabel() + ')';
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
  }

  Plotly.react(chartEl, traces, layout, config);
  state.savedZoom = null;
}

// ── Periodic averages ────────────────────────────────────────────────────────
function eatDate(ms) { return new Date(ms + 3 * 3600 * 1000); }

function getISOWeekStr(ms) {
  const d = eatDate(ms);
  const yr = d.getUTCFullYear();
  const jan1 = new Date(Date.UTC(yr, 0, 1));
  const dayOfYear = Math.floor((d - jan1) / 86400000);
  const weekNum = Math.floor((dayOfYear + jan1.getUTCDay()) / 7) + 1;
  return yr + '-W' + String(weekNum).padStart(2, '0');
}

const groupByOptions = {
  day:  [{value:'hour', label:'Hour'}, {value:'synoptic', label:'Synoptic Hours'}],
  year: [{value:'month', label:'Month'}, {value:'week', label:'Week'}, {value:'season', label:'Season'}],
  mjo:  [{value:'phase', label:'Phase (1\u20138)'}],
  iod:  [{value:'phase', label:'Phase (+/\u2212/Neutral)'}],
  enso: [{value:'phase', label:'Phase (Ni\u00f1o/Ni\u00f1a/Neutral)'}],
};

const oscInfoTexts = {
  mjo: 'Madden\u2013Julian Oscillation: a tropical weather pattern that circles the globe every 30\u201360 days, modulating rainfall and wind. 8 phases track its position \u2014 Phases 2\u20133 (Indian Ocean) and 4\u20135 (Maritime Continent) are most relevant to East Africa. Weekly RMM phase data; weeks with amplitude < 1.0 are excluded.',
  iod: 'Indian Ocean Dipole: a sea-surface temperature gradient between the western and eastern Indian Ocean. Positive IOD brings wetter conditions to East Africa; Negative IOD brings drier conditions. Monthly DMI-based phases: Positive, Negative, or Neutral.',
  enso: 'El Ni\u00f1o\u2013Southern Oscillation: Pacific Ocean temperature cycles affecting global weather. El Ni\u00f1o tends to bring wetter short rains (Vuli) to East Africa; La Ni\u00f1a tends to bring drier conditions. Monthly ONI-based phases: El Ni\u00f1o, La Ni\u00f1a, or Neutral.',
};

function updatePeriodCycleInfo() {
  const infoIcon = document.getElementById('natural-cycles-info');
  const infoTip = document.getElementById('natural-cycles-tip');
  const isOsc = state.periodCycle === 'mjo' || state.periodCycle === 'iod' || state.periodCycle === 'enso';
  infoIcon.style.display = isOsc ? '' : 'none';
  infoTip.style.display = 'none';
  infoTip.textContent = oscInfoTexts[state.periodCycle] || '';
  if (isOsc) {
    infoIcon.onmouseenter = () => { infoTip.style.display = ''; };
    infoIcon.onmouseleave = () => { infoTip.style.display = 'none'; };
  }
}

function updateGroupByDropdown() {
  const gsel = document.getElementById('period-group-by');
  gsel.innerHTML = '';
  const opts = groupByOptions[state.periodCycle] || [];
  opts.forEach(o => gsel.appendChild(new Option(o.label, o.value)));
  const defaults = {year:'month', day:'hour', mjo:'phase', iod:'phase', enso:'phase'};
  state.periodGroupBy = defaults[state.periodCycle] || (opts.length ? opts[0].value : 'hour');
  gsel.value = state.periodGroupBy;
  gsel.parentElement.style.display = opts.length <= 1 ? 'none' : '';
  updatePeriodCycleInfo();
}

function fitCycleWidth() {
  const sel = document.getElementById('natural-cycles');
  if (!sel) return;
  const isOsc = sel.value === 'mjo' || sel.value === 'iod' || sel.value === 'enso';
  const fs = isOsc ? '10px' : '12px';
  sel.style.fontSize = fs;
  const tmp = document.createElement('select');
  tmp.style.cssText = 'position:absolute;visibility:hidden;font-size:' + fs + ';';
  tmp.appendChild(new Option(sel.options[sel.selectedIndex].text));
  document.body.appendChild(tmp);
  sel.style.width = (tmp.offsetWidth + 8) + 'px';
  document.body.removeChild(tmp);
}

function renderPeriodicAverages() {
  const ct = state.chartType;
  const pr = state.periodCycle, pg = state.periodGroupBy;
  const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const TZ_SEASON_IDX = [0,0,1,1,1,2,2,2,2,2,3,3];
  const TZ_SEASON_LABELS = ['Kiangazi (Jan\u2013Feb)','Masika (Mar\u2013May)','Kiangazi (Jun\u2013Oct)','Vuli (Nov\u2013Dec)'];

  let nCats, categoryLabels, getCategoryIdx, xPositions = null;
  const isClimateOsc = (pr === 'mjo' || pr === 'iod' || pr === 'enso');

  if (pr === 'day' && pg === 'hour') {
    nCats = 24;
    categoryLabels = Array.from({length:24}, (_, i) => String(i).padStart(2,'0') + ':00');
    getCategoryIdx = ms => eatDate(ms).getUTCHours();
  } else if (pr === 'day' && pg === 'synoptic') {
    nCats = 4;
    categoryLabels = ['Late Night (00\u201306)','Morning (06\u201312)','Afternoon (12\u201318)','Evening (18\u201300)'];
    getCategoryIdx = ms => { const h = eatDate(ms).getUTCHours(); if (h < 6) return 0; if (h < 12) return 1; if (h < 18) return 2; return 3; };
  } else if (pr === 'year' && pg === 'month') {
    nCats = 12;
    categoryLabels = MN;
    getCategoryIdx = ms => eatDate(ms).getUTCMonth();
  } else if (pr === 'year' && pg === 'week') {
    nCats = 53;
    categoryLabels = Array.from({length:53}, (_, i) => 'W' + (i+1));
    getCategoryIdx = ms => { const d = eatDate(ms); const jan1 = new Date(Date.UTC(d.getUTCFullYear(),0,1)); return Math.min(52, Math.floor((d - jan1) / (7*86400000))); };
  } else if (pr === 'year' && pg === 'season') {
    nCats = 4;
    categoryLabels = TZ_SEASON_LABELS;
    xPositions = [0.5, 3, 7, 10.5];
    getCategoryIdx = ms => TZ_SEASON_IDX[eatDate(ms).getUTCMonth()];
  } else if (pr === 'mjo') {
    nCats = 8;
    categoryLabels = MJO_LABELS;
    getCategoryIdx = ms => { const wk = getISOWeekStr(ms); const ph = MJO_PHASES[wk]; return (ph != null) ? ph : -1; };
  } else if (pr === 'iod') {
    nCats = 3;
    categoryLabels = IOD_LABELS;
    getCategoryIdx = ms => { const d = eatDate(ms); const key = d.getUTCFullYear() + '-' + String(d.getUTCMonth()+1).padStart(2,'0'); const ph = IOD_PHASES[key]; return (ph != null) ? ph : -1; };
  } else if (pr === 'enso') {
    nCats = 3;
    categoryLabels = ENSO_LABELS;
    getCategoryIdx = ms => { const d = eatDate(ms); const key = d.getUTCFullYear() + '-' + String(d.getUTCMonth()+1).padStart(2,'0'); const ph = ENSO_PHASES[key]; return (ph != null) ? ph : -1; };
  } else {
    nCats = 24;
    categoryLabels = Array.from({length:24}, (_, i) => String(i).padStart(2,'0') + ':00');
    getCategoryIdx = ms => eatDate(ms).getUTCHours();
  }

  const xVal = ci => xPositions ? xPositions[ci] : categoryLabels[ci];
  const emptyResult = () => ({traces:[], layout:{autosize:true, font:{family:'Ubuntu, sans-serif'}}});

  // Pick raw data source
  let srcId;
  if (ct === 'avg-wind-profiles') srcId = 'wind-timeseries';
  else if (ct === 'avg-solar-profiles') srcId = 'solar-timeseries';
  else srcId = 'cumulative-rainfall';

  const srcChart = getChartById(srcId);
  if (!srcChart || !srcChart.data || srcChart.data.length === 0) return emptyResult();
  const srcTrace = srcChart.data[0];
  const x_ms = srcTrace.x_ms;
  const rawY = srcTrace.y;
  if (!x_ms || x_ms.length === 0) return emptyResult();

  // Derive values: incremental rainfall from cumulative series
  let vals;
  if (ct === 'avg-rainfall-profiles') {
    vals = new Array(x_ms.length).fill(null);
    for (let i = 1; i < rawY.length; i++) {
      if (rawY[i] != null && rawY[i-1] != null) {
        const diff = rawY[i] - rawY[i-1];
        if (diff >= 0) vals[i] = diff;
      }
    }
  } else {
    vals = rawY;
  }

  // Accumulate per category
  const sums = new Float64Array(nCats);
  const sumsq = new Float64Array(nCats);
  const counts = new Int32Array(nCats);
  const calmCounts = new Int32Array(nCats);
  const rainCounts = new Int32Array(nCats);
  const totCounts = new Int32Array(nCats);

  const {start: paRngStart, end: paRngEnd} = getTimeRange();
  for (let i = 0; i < x_ms.length; i++) {
    if (x_ms[i] < paRngStart || x_ms[i] > paRngEnd) continue;
    const ci = getCategoryIdx(x_ms[i]);
    if (ci < 0 || ci >= nCats) continue;
    const v = vals[i];
    if (v == null || !isFinite(v)) continue;
    sums[ci] += v;
    sumsq[ci] += v * v;
    counts[ci]++;
    if (ct === 'avg-wind-profiles') {
      if (rawY[i] != null && isFinite(rawY[i]) && rawY[i] === 0) calmCounts[ci]++;
    }
    if (ct === 'avg-rainfall-profiles') {
      totCounts[ci]++;
      if (v > 0) rainCounts[ci]++;
    }
  }

  const xArr = [], meanArr = [], upperArr = [], lowerArr = [];
  const calmPcts = [], rainProbs = [];
  for (let ci = 0; ci < nCats; ci++) {
    xArr.push(xVal(ci));
    if (counts[ci] > 0) {
      const mean = sums[ci] / counts[ci];
      const sd = Math.sqrt(Math.max(0, sumsq[ci] / counts[ci] - mean * mean));
      meanArr.push(+mean.toFixed(3));
      upperArr.push(+(mean + sd).toFixed(3));
      lowerArr.push(+Math.max(0, mean - sd).toFixed(3));
    } else {
      meanArr.push(null); upperArr.push(null); lowerArr.push(null);
    }
    calmPcts.push(counts[ci] > 0 ? +(calmCounts[ci] / counts[ci] * 100).toFixed(1) : null);
    rainProbs.push(totCounts[ci] > 0 ? +(rainCounts[ci] / totCounts[ci] * 100).toFixed(1) : null);
  }

  const traces = [];
  const sm = window.innerWidth < 680;

  if (ct === 'avg-wind-profiles') {
    if (!isClimateOsc) {
      traces.push({type:'scatter', mode:'lines', x:xArr, y:upperArr, line:{width:0}, showlegend:false, hoverinfo:'skip', connectgaps:false});
      traces.push({type:'scatter', mode:'lines', x:xArr, y:lowerArr, fill:'tonexty', fillcolor:'rgba(31,119,180,0.18)', line:{width:0}, name:'\u00b11 SD', showlegend:true, hoverinfo:'skip', connectgaps:false});
    }
    const wCvt = arr => arr.map(v => v != null ? wToUnit(v) : null);
    const dispMean = wCvt(meanArr), dispUpper = wCvt(upperArr), dispLower = wCvt(lowerArr);
    if (!isClimateOsc) {
      traces[0].y = dispUpper;
      traces[1].y = dispLower;
    }
    const meanTrace = {type:'scatter', x:xArr, y:dispMean, name:'Mean Wind Speed', hovertemplate:'%{x}<br>Mean: %{y:.2f} ' + wLabel() + '<extra></extra>'};
    if (isClimateOsc) { meanTrace.mode = 'markers'; meanTrace.marker = {color:'#1f77b4', size:10, line:{color:'white',width:1}}; }
    else { meanTrace.mode = 'lines+markers'; meanTrace.line = {color:'#1f77b4', width:2}; meanTrace.marker = {size:5}; meanTrace.connectgaps = false; }
    traces.push(meanTrace);
    traces.push({type:'bar', name:'Calm %', x:xArr, y:calmPcts, yaxis:'y2', marker:{color:'rgba(180,180,180,0.5)'}, textposition:'none', hovertemplate:'%{x}<br>Calm: %{y:.1f}%<extra></extra>'});
  } else if (ct === 'avg-solar-profiles') {
    if (!isClimateOsc) {
      traces.push({type:'scatter', mode:'lines', x:xArr, y:upperArr, line:{width:0}, showlegend:false, hoverinfo:'skip', connectgaps:false});
      traces.push({type:'scatter', mode:'lines', x:xArr, y:lowerArr, fill:'tonexty', fillcolor:'rgba(255,140,0,0.18)', line:{width:0}, name:'\u00b11 SD', showlegend:true, hoverinfo:'skip', connectgaps:false});
    }
    const meanTrace = {type:'scatter', x:xArr, y:meanArr, name:'Mean Irradiance', hovertemplate:'%{x}<br>Mean: %{y:.1f} W/m\u00b2<extra></extra>'};
    if (isClimateOsc) { meanTrace.mode = 'markers'; meanTrace.marker = {color:'#ff8c00', size:10, line:{color:'white',width:1}}; }
    else { meanTrace.mode = 'lines+markers'; meanTrace.line = {color:'#ff8c00', width:2}; meanTrace.marker = {size:5}; meanTrace.connectgaps = false; }
    traces.push(meanTrace);
  } else {
    // avg-rainfall-profiles
    traces.push({type:'bar', name:'Mean Rainfall', x:xArr, y:meanArr, marker:{color:'rgba(31,119,180,0.7)'}, textposition:'none', hovertemplate:'%{x}<br>Mean: %{y:.3f} mm<extra></extra>'});
    const probTrace = {type:'scatter', x:xArr, y:rainProbs, name:'Rain Probability %', yaxis:'y2', hovertemplate:'%{x}<br>Rain prob: %{y:.1f}%<extra></extra>'};
    if (isClimateOsc) { probTrace.mode = 'markers'; probTrace.marker = {color:'#d62728', size:10, line:{color:'white',width:1}}; }
    else { probTrace.mode = 'lines+markers'; probTrace.line = {color:'#d62728', width:2}; probTrace.marker = {size:5}; probTrace.connectgaps = false; }
    traces.push(probTrace);
  }

  // X-axis config
  let xTitle;
  if (pr === 'day' && pg === 'hour') xTitle = t('hourOfDay') + ' <i><span style="color:#aaa">(EAT, UTC+03:00)</span></i>';
  else if (pr === 'day' && pg === 'synoptic') xTitle = t('timeOfDay') + ' <i><span style="color:#aaa">(EAT)</span></i>';
  else if (pr === 'year' && pg === 'month') xTitle = t('monthOfYear');
  else if (pr === 'year' && pg === 'week') xTitle = t('weekOfYear');
  else if (pr === 'year' && pg === 'season') xTitle = t('tanzanianSeason');
  else if (pr === 'mjo') xTitle = 'Madden\u2013Julian Oscillation (MJO) Phase';
  else if (pr === 'iod') xTitle = 'Indian Ocean Dipole (IOD) Phase';
  else if (pr === 'enso') xTitle = 'El Ni\u00f1o\u2013Southern Oscillation (ENSO) Phase';
  else xTitle = pr;

  let xaxisCfg;
  if (xPositions) {
    xaxisCfg = {title:xTitle, type:'linear', showgrid:true, gridcolor:'#eee', range:[-0.5,11.5], zeroline:false, tickvals:[0,1,2,3,4,5,6,7,8,9,10,11], ticktext:MN, automargin:true};
  } else {
    xaxisCfg = {title:xTitle, type:'category', showgrid:true, gridcolor:'#eee', tickangle:(isClimateOsc || nCats > 15) ? -30 : 0, automargin:true};
  }

  let yTitle, y2cfg;
  if (ct === 'avg-wind-profiles') {
    yTitle = 'Wind Speed (' + wLabel() + ')';
    y2cfg = {title:'Calm %', overlaying:'y', side:'right', range:[0,100], showgrid:false};
  } else if (ct === 'avg-solar-profiles') {
    yTitle = 'Solar Radiation (W/m\u00b2)';
  } else {
    yTitle = 'Rainfall (mm per reading)';
    y2cfg = {title:'Rain Probability %', overlaying:'y', side:'right', range:[0,100], showgrid:false};
  }

  const layout = {
    autosize:true, font:{family:'Ubuntu, sans-serif'},
    margin:{l:sm?45:65, r:sm?45:65, t:sm?20:36, b:sm?60:80},
    xaxis:xaxisCfg,
    yaxis:{title:yTitle, showgrid:true, gridcolor:'#eee', rangemode:'tozero'},
    legend:{orientation:'h', x:0, y:1.08},
    plot_bgcolor:'white', paper_bgcolor:'white',
    hovermode:'closest', hoverlabel:{font:{family:'Ubuntu, sans-serif'}},
    barmode:'overlay',
  };
  if (y2cfg) layout.yaxis2 = y2cfg;

  return {traces, layout};
}


function renderRainEventsTable(chart) {
  const tbody = document.getElementById('rain-events-body');
  tbody.innerHTML = '';
  const events = (chart.events || []).slice();
  const {col, dir} = rainEventsSort;
  events.sort((a, b) => {
    const av = a[col], bv = b[col];
    const cmp = (typeof av === 'string') ? av.localeCompare(bv) : (av - bv);
    return dir === 'asc' ? cmp : -cmp;
  });
  for (const ev of events) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + toEATString(ev.start_ms) + '</td>' +
      '<td>' + toEATString(ev.end_ms) + '</td>' +
      '<td>' + formatDuration(ev.duration_min, ev.duration_min === 0) + '</td>' +
      '<td>' + ev.total_mm + '</td>' +
      '<td>' + ev.peak_rate + '</td>' +
      '<td>' + ev.mean_rate + '</td>' +
      '<td>' + ev.wind_dir + '</td>';
    tbody.appendChild(tr);
  }
  // Update header sort indicators
  document.querySelectorAll('#rain-events-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    let arrow = th.querySelector('.sort-arrow');
    if (!arrow) { arrow = document.createElement('span'); arrow.className = 'sort-arrow'; th.appendChild(arrow); }
    if (th.dataset.col === col) {
      th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      arrow.textContent = dir === 'asc' ? '\u25b2' : '\u25bc';
    } else {
      arrow.textContent = '\u25bc';
    }
  });
}

function initRainEventsSort() {
  document.querySelectorAll('#rain-events-table th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (rainEventsSort.col === col) {
        rainEventsSort.dir = rainEventsSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        rainEventsSort = {col, dir: 'desc'};
      }
      const chart = (ALL_DATA.charts || []).find(c => c.id === 'rain-events');
      if (chart) renderRainEventsTable(chart);
    });
  });
}

// ── Language ─────────────────────────────────────────────────────────────────
function setLanguage(lang) {
  currentLang = lang;
  localStorage.setItem('arcWeatherLang', lang);
  const menu = document.getElementById('lang-menu');
  if (menu) {
    menu.classList.remove('open');
    menu.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.textContent === (lang === 'sw' ? 'Kiswahili' : 'English'))
    );
  }
  document.documentElement.lang = lang === 'sw' ? 'sw' : 'en';
  applyLanguage();
  updatePlot();
}

function applyLanguage() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  // Translate optgroup labels
  document.querySelectorAll('[data-i18n-label]').forEach(el => {
    el.label = t(el.dataset.i18nLabel);
  });
  // Translate select options
  document.querySelectorAll('select option[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  // Range: label
  const tmSel = document.getElementById('time-mode');
  if (tmSel) {
    const row = tmSel.closest('.control-row');
    if (row) {
      const lbl = row.querySelector('label');
      if (lbl && !lbl.querySelector('input')) lbl.textContent = t('range');
    }
  }
  // From / To labels
  const betweenDiv = document.getElementById('between-inputs');
  if (betweenDiv) {
    betweenDiv.querySelectorAll('label').forEach((lbl, i) => {
      const input = lbl.querySelector('input');
      if (input) lbl.replaceChildren(document.createTextNode(t(i === 0 ? 'from' : 'to')), input);
    });
  }
  updateStatsHeading();
}

// ── Period Selectors ─────────────────────────────────────────────────────────
function populatePeriodSelectors() {
  const m = ALL_DATA.meta;
  if (!m) return;

  const ysel = document.getElementById('year-select');
  const ssel = document.getElementById('season-select');
  const msel = document.getElementById('month-select');
  const wsel = document.getElementById('week-select');
  const dsel = document.getElementById('day-select');

  if (m.availableYears) m.availableYears.forEach(y => ysel.add(new Option(y, y)));
  if (m.availableSeasons) m.availableSeasons.forEach(s => ssel.add(new Option(s.label, `${s.year}-${s.season}`)));
  if (m.availableMonths) m.availableMonths.forEach(s => msel.add(new Option(s.label, `${s.year}-${s.month}`)));
  if (m.availableWeeks) m.availableWeeks.forEach(s => wsel.add(new Option(s.label, `${s.year}-${s.week}`)));
  if (m.availableDays) m.availableDays.forEach(s => dsel.add(new Option(s.label, s.ts)));

  // Default date range
  if (m.dateRange) {
    const fmt = ms => new Date(ms).toISOString().slice(0, 10);
    document.getElementById('date-start').value = fmt(m.dateRange.min);
    document.getElementById('date-end').value = fmt(m.dateRange.max);
    state.betweenStart = m.dateRange.min;
    state.betweenEnd = m.dateRange.max;
  }

  // Set defaults to last available
  if (m.availableYears && m.availableYears.length) {
    state.selectedYear = m.availableYears[m.availableYears.length - 1];
    ysel.value = state.selectedYear;
  }
  if (m.availableSeasons && m.availableSeasons.length) {
    const last = m.availableSeasons[m.availableSeasons.length - 1];
    state.selectedSeason = {year: last.year, season: last.season};
    ssel.value = `${last.year}-${last.season}`;
  }
  if (m.availableMonths && m.availableMonths.length) {
    const last = m.availableMonths[m.availableMonths.length - 1];
    state.selectedMonth = {year: last.year, month: last.month};
    msel.value = `${last.year}-${last.month}`;
  }
  if (m.availableWeeks && m.availableWeeks.length) {
    const last = m.availableWeeks[m.availableWeeks.length - 1];
    state.selectedWeek = {year: last.year, week: last.week};
    wsel.value = `${last.year}-${last.week}`;
  }
  if (m.availableDays && m.availableDays.length) {
    state.selectedDay = m.availableDays[m.availableDays.length - 1].ts;
    dsel.value = state.selectedDay;
  }
}

function updateTimeModeVisibility() {
  const mode = state.timeMode;
  document.getElementById('between-inputs').classList.toggle('hidden', mode !== 'between');
  document.getElementById('year-input').classList.toggle('hidden', mode !== 'year');
  document.getElementById('season-input').classList.toggle('hidden', mode !== 'season');
  document.getElementById('month-input').classList.toggle('hidden', mode !== 'month');
  document.getElementById('week-input').classList.toggle('hidden', mode !== 'week');
  document.getElementById('day-input').classList.toggle('hidden', mode !== 'day');
}

// ── Data Freshness ───────────────────────────────────────────────────────────
function updateDataFreshness() {
  const df = ALL_DATA.dataFreshness;
  if (!df) return;
  const el = document.getElementById('data-freshness');
  const DAY_MS = 86400000;
  const lines = [];
  let warnHtml = '';
  if (df.fetchTime) {
    const parts = df.fetchTime.split(' ');
    if (parts.length >= 2) {
      const fetchDate = new Date(parts[0] + 'T' + parts[1] + ':00Z');
      const now = new Date();
      const diffDays = (now - fetchDate) / DAY_MS;
      if (diffDays > 2) {
        warnHtml = ' <span class="stale-warn" title="' + t('staleWarning') + '">\u26a0</span>';
      }
    }
  }
  lines.push('Omnisense last updated: ' + (df.fetchTime || 'Unknown') + warnHtml);
  // Cycle data freshness
  if (df.cyclesFetchTime) {
    const now = Date.now();
    const issues = [];
    if (df.mjo_last) {
      const [y, w] = df.mjo_last.replace('W', '').split('-').map(Number);
      const mjoMs = new Date(y, 0, 1 + (w - 1) * 7).getTime();
      if (now - mjoMs > 21 * DAY_MS) issues.push('MJO data ends at ' + df.mjo_last);
    }
    if (df.enso_last) {
      const [y, m] = df.enso_last.split('-').map(Number);
      const ensoMs = new Date(y, m - 1, 15).getTime();
      if (now - ensoMs > 90 * DAY_MS) issues.push('ENSO data ends at ' + df.enso_last);
    }
    if (df.iod_last) {
      const [y, m] = df.iod_last.split('-').map(Number);
      const iodMs = new Date(y, m - 1, 15).getTime();
      if (now - iodMs > 90 * DAY_MS) issues.push('IOD data ends at ' + df.iod_last);
    }
    const cycleWarnHtml = issues.length
      ? ' <span class="stale-warn" title="' + issues.join('; ') + '">\u26a0</span>'
      : '';
    lines.push('Cycles (ENSO/IOD/MJO) last updated: ' + df.cyclesFetchTime + cycleWarnHtml);
  }
  lines.push(df.rowCount + ' readings, ' + df.dateMin.slice(0, 10) + ' to ' + df.dateMax.slice(0, 10));
  el.innerHTML = lines.join('<br>');
}

// Close language menu on click outside
document.addEventListener('click', e => {
  const wrap = document.getElementById('lang-wrap');
  const menu = document.getElementById('lang-menu');
  if (menu && wrap && !wrap.contains(e.target)) menu.classList.remove('open');
});

// ── SVG watermark helpers ────────────────────────────────────────────────────
function parseSVGDataUrl(svgDataUrl) {
  const b64tag = 'data:image/svg+xml;base64,';
  if (svgDataUrl.startsWith(b64tag)) return atob(svgDataUrl.slice(b64tag.length));
  return decodeURIComponent(svgDataUrl.slice(svgDataUrl.indexOf(',') + 1));
}

function injectSVGWatermark(doc, svgW, svgH, opacity) {
  if (!WATERMARK_LOGO_B64) return;
  const ns = 'http://www.w3.org/2000/svg';
  const root = doc.querySelector('.infolayer') || doc.documentElement;
  const logoH = 40, logoW = Math.round(logoH * WATERMARK_LOGO_ASPECT);
  const textSize = 9, lineH = 14;
  const leftMargin = 12, rightMargin = 12, bottomEdge = 10, topEdge = 12;
  const line1 = 'Graph generated by ARC (Architecture for Resilient Communities).';
  const line2 = 'Find out more about what we do at actionresearchprojects.net.';
  const logoX = leftMargin, logoY = topEdge;
  const txt2Y = svgH - bottomEdge, txt1Y = txt2Y - lineH;

  const imgEl = doc.createElementNS(ns, 'image');
  imgEl.setAttribute('href', WATERMARK_LOGO_B64);
  imgEl.setAttribute('x', String(logoX));
  imgEl.setAttribute('y', String(logoY));
  imgEl.setAttribute('width', String(logoW));
  imgEl.setAttribute('height', String(logoH));
  imgEl.setAttribute('opacity', String(opacity));
  root.appendChild(imgEl);

  function mkTxt(y, content) {
    const el = doc.createElementNS(ns, 'text');
    el.setAttribute('x', String(svgW - rightMargin));
    el.setAttribute('y', String(y));
    el.setAttribute('text-anchor', 'end');
    el.setAttribute('dominant-baseline', 'auto');
    el.setAttribute('font-family', 'Ubuntu, sans-serif');
    el.setAttribute('font-size', String(textSize));
    el.setAttribute('fill', '#555');
    el.setAttribute('opacity', String(opacity));
    el.textContent = content;
    return el;
  }
  root.appendChild(mkTxt(txt1Y, line1));
  root.appendChild(mkTxt(txt2Y, line2));
}

function svgToCanvas(svgStr, W, H, scale) {
  return new Promise((resolve, reject) => {
    const canvas = document.createElement('canvas');
    canvas.width = W * scale; canvas.height = H * scale;
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);
    const img = new Image();
    img.onload = () => { ctx.drawImage(img, 0, 0, W, H); resolve(canvas); };
    img.onerror = reject;
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr);
  });
}

// ── Download PNG ─────────────────────────────────────────────────────────────
document.getElementById('download-btn').addEventListener('click', () => {
  const btn = document.getElementById('download-btn');
  const spinner = document.getElementById('dl-spinner');
  function dlStart() { btn.disabled = true; spinner.style.display = 'inline-block'; }
  function dlDone()  { btn.disabled = false; spinner.style.display = 'none'; }

  const chartEl = document.getElementById('chart');
  const ct = state.chartType;
  const chart = getChartById(ct);
  const title = chart ? (currentLang === 'sw' ? (chart.title_sw || chart.title) : chart.title) : ct;

  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const ts = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + '_' + pad(now.getHours()) + pad(now.getMinutes());
  const filename = 'ARC_Weather_' + ct + '_' + ts;

  const sm = window.innerWidth < 680;
  const W = chartEl.offsetWidth;
  const H = chartEl.offsetHeight;
  const scale = 3;

  function canvasToPNG(canvas) {
    canvas.toBlob(blob => {
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl; a.download = filename + '.png';
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(blobUrl);
      dlDone();
    }, 'image/png');
  }

  dlStart();
  const origMarginT = (chartEl.layout && chartEl.layout.margin && chartEl.layout.margin.t) || 50;
  const pngTopMargin = sm ? 55 : 85;

  Plotly.relayout('chart', {
    'title.text': '<b>' + title + '</b>',
    'title.font.size': sm ? 12 : 14,
    'margin.t': pngTopMargin,
  }).then(() => {
    return Plotly.toImage('chart', {format: 'svg', width: W, height: H});
  }).then(svgDataUrl => {
    Plotly.relayout('chart', {'title.text': '', 'margin.t': origMarginT});
    const doc = new DOMParser().parseFromString(parseSVGDataUrl(svgDataUrl), 'image/svg+xml');
    injectSVGWatermark(doc, W, H, 1.0);
    return svgToCanvas(new XMLSerializer().serializeToString(doc), W, H, scale);
  }).then(canvasToPNG).catch(dlDone);
});

// ── Event Handlers ───────────────────────────────────────────────────────────
document.getElementById('chart-category').addEventListener('change', function() {
  populateChartSelect(this.value);
  state.chartType = document.getElementById('chart-select').value;
  state.savedZoom = null;
  updatePlot();
});

document.getElementById('chart-select').addEventListener('change', function() {
  state.chartType = this.value;
  state.savedZoom = null;
  updatePlot();
});

document.getElementById('time-mode').addEventListener('change', function() {
  state.timeMode = this.value;
  updateTimeModeVisibility();
  updatePlot();
});

document.getElementById('year-select').addEventListener('change', function() {
  state.selectedYear = parseInt(this.value); updatePlot();
});
document.getElementById('season-select').addEventListener('change', function() {
  const [y, s] = this.value.split('-').map(Number);
  state.selectedSeason = {year: y, season: s}; updatePlot();
});
document.getElementById('month-select').addEventListener('change', function() {
  const [y, mo] = this.value.split('-').map(Number);
  state.selectedMonth = {year: y, month: mo}; updatePlot();
});
document.getElementById('week-select').addEventListener('change', function() {
  const [y, w] = this.value.split('-').map(Number);
  state.selectedWeek = {year: y, week: w}; updatePlot();
});
document.getElementById('day-select').addEventListener('change', function() {
  state.selectedDay = parseInt(this.value); updatePlot();
});
document.getElementById('date-start').addEventListener('change', function() {
  state.betweenStart = new Date(this.value + 'T00:00:00').getTime(); updatePlot();
});
document.getElementById('date-end').addEventListener('change', function() {
  state.betweenEnd = new Date(this.value + 'T23:59:59').getTime(); updatePlot();
});

// ── Initialization ───────────────────────────────────────────────────────────
function init() {
  // Logo
  if (LOGO_B64) {
    const logo = document.getElementById('logo');
    logo.src = LOGO_B64;
  }

  // Populate period selectors
  populatePeriodSelectors();

  // Wire tooltips
  wireTooltip('chart-info-icon', 'chart-info-tip', CHART_INFO[state.chartType] || 'infoWindRose');
  // Dynamic chart info tooltip
  const chartIcon = document.getElementById('chart-info-icon');
  const chartTip = document.getElementById('chart-info-tip');
  chartIcon.addEventListener('mouseenter', (e) => {
    const key = CHART_INFO[state.chartType] || 'infoWindRose';
    chartTip.textContent = t(key);
    chartTip.style.display = 'block';
    const r = chartIcon.getBoundingClientRect();
    chartTip.style.left = Math.min(r.left, window.innerWidth - 340) + 'px';
    chartTip.style.top = (r.bottom + 6) + 'px';
  });
  chartIcon.addEventListener('mouseleave', () => { chartTip.style.display = 'none'; });

  // Periodic controls
  fitCycleWidth();
  updateGroupByDropdown();
  document.getElementById('natural-cycles').addEventListener('change', e => {
    state.periodCycle = e.target.value;
    fitCycleWidth();
    updateGroupByDropdown();
    updatePlot();
  });
  document.getElementById('period-group-by').addEventListener('change', e => {
    state.periodGroupBy = e.target.value;
    updatePlot();
  });

  // Data freshness
  updateDataFreshness();

  // Apply saved language preference and mark active button
  const savedLang = localStorage.getItem('arcWeatherLang') || 'en';
  if (savedLang !== 'en') setLanguage(savedLang);
  else {
    const menu = document.getElementById('lang-menu');
    if (menu) menu.querySelector('button').classList.add('active');
  }

  // Sidebar toggle + backdrop
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  function closeSidebar() { sidebar.classList.remove('open'); backdrop.classList.remove('open'); }
  toggle.addEventListener('click', () => {
    const isOpen = sidebar.classList.toggle('open');
    backdrop.classList.toggle('open', isOpen);
  });
  backdrop.addEventListener('click', closeSidebar);
  window.addEventListener('resize', () => {
    if (window.innerWidth > 680) closeSidebar();
    Plotly.relayout('chart', {autosize: true});
  });

  // Wire rain events table sort
  initRainEventsSort();

  // Initial render
  updatePlot();
}

init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build ARC Weather Station dashboard")
    parser.add_argument("--csv", help="Path to specific CSV file")
    args = parser.parse_args()

    build_dashboard(csv_path=args.csv)
