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
    load_weather_csv, load_outdoor_th, find_latest_csv, build_available_periods,
    wind_qc, detect_precip_resets, to_eat_ms, TIMEZONE,
    MAGNETIC_DECLINATION_EXPIRED, _IGRF14_EXPIRY,
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
    df_r = wind_qc(df_r)
    precip_incr = detect_precip_resets(df_r["precip_total_mm"]).diff().clip(lower=0).fillna(0)
    _arc_result   = wind.fit_arc_wind_bands(df_r["avg_wind_kph"].dropna().values)
    arc_wind_bands = _arc_result["bands"] if _arc_result else None
    arc_gmm_meta   = _arc_result["meta"]   if _arc_result else None
    _arc_gust_result  = wind.fit_arc_wind_bands(df_r["peak_wind_kph"].dropna().values)
    arc_bands_gust = _arc_gust_result["bands"] if _arc_gust_result else None
    arc_meta_gust  = _arc_gust_result["meta"]  if _arc_gust_result else None
    # Outdoor temperature & humidity (weather-station T&RH sensor) for conditional
    # filtering. Merged onto the wind/solar/rain timestamps by nearest match within
    # 5 minutes; df_r is already sorted ascending so the merged order aligns row-for-row.
    _th = load_outdoor_th(csv_file)
    if not _th.empty:
        _merged = pd.merge_asof(
            df_r[["timestamp"]].reset_index(drop=True),
            _th.sort_values("timestamp"),
            on="timestamp", direction="nearest", tolerance=pd.Timedelta("5min"),
        )
        _temp_arr = list(_merged["temp"])
        _hum_arr = list(_merged["humidity"])
        print(f"Merged outdoor T&RH ({len(_th)} readings, "
              f"{_th['timestamp'].min()} to {_th['timestamp'].max()})")
    else:
        _temp_arr = [None] * len(df_r)
        _hum_arr = [None] * len(df_r)
        print("WARNING: outdoor T&RH sensor not found; temperature/humidity filters disabled")

    raw_data = {
        "ts":           [to_eat_ms(t) for t in df_r["timestamp"]],
        "avgWind":      [round(float(v), 1) if pd.notna(v) else None for v in df_r["avg_wind_kph"]],
        "peakWind":     [round(float(v), 1) if pd.notna(v) else None for v in df_r["peak_wind_kph"]],
        "windDir":      [int(v) if pd.notna(v) else None for v in df_r["wind_dir"]],
        "solar":        [round(float(v), 1) if pd.notna(v) else None for v in df_r["solar_wm2"]],
        "precipRate":   [round(float(v), 3) if pd.notna(v) else None for v in df_r["precip_rate_mmh"]],
        "precipIncr":   [round(float(v), 3) if pd.notna(v) else None for v in precip_incr],
        "temp":         [round(float(v), 1) if pd.notna(v) else None for v in _temp_arr],
        "humidity":     [round(float(v), 1) if pd.notna(v) else None for v in _hum_arr],
        "arcBands":     arc_wind_bands,
        "arcMeta":      arc_gmm_meta,
        "arcBandsGust": arc_bands_gust,
        "arcMetaGust":  arc_meta_gust,
    }

    print("Computing hourly DRI (ISO 15927-3)...")
    dri_hourly = cross_variable.build_driving_rain_hourly(df_r, precip_incr)

    data_blob = {
        "meta": periods,
        "charts": all_charts,
        "stats": all_stats,
        "raw": raw_data,
        "driHourly": dri_hourly,
        "declModelExpired": MAGNETIC_DECLINATION_EXPIRED,
        "declModelExpiry": _IGRF14_EXPIRY.strftime("%B %Y"),
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
.polarlayer text{stroke:white;stroke-width:3px;paint-order:stroke fill;stroke-linejoin:round}
body{font-family:'Ubuntu',sans-serif;font-size:13px;background:#f8f9fa;color:#333;display:flex;flex-direction:column;height:100vh;overflow:hidden}
#header{background:white;border-bottom:1px solid #ddd;padding:6px 12px;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap;min-height:40px}
#decl-expired-banner{background:#fff3cd;border-bottom:3px solid #e6a817;flex-shrink:0}
#decl-expired-inner{display:flex;align-items:flex-start;gap:12px;padding:10px 16px;max-width:900px}
#decl-expired-icon{font-size:22px;color:#b45309;flex-shrink:0;line-height:1.3}
#decl-expired-inner strong{display:block;margin-bottom:3px;color:#78350f}
#decl-expired-inner div{font-size:12px;color:#44362a;line-height:1.5}
#decl-expired-inner code{background:#fde68a;padding:1px 4px;border-radius:3px;font-size:11px}
#decl-expired-dismiss{margin-left:auto;flex-shrink:0;background:transparent;border:1px solid #92400e;color:#92400e;padding:4px 12px;cursor:pointer;border-radius:4px;font-size:12px;white-space:nowrap;align-self:center}
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
#chart-note{font-size:10px;color:#999;padding:2px 8px;min-height:0;font-style:italic}
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
#download-menu{padding:4px 8px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:#28a745;color:white;font-weight:500;white-space:nowrap}
#download-menu:hover{background:#218838}
#download-menu:disabled{opacity:0.6;cursor:default}
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
#wr-slider-wrap{display:none;padding:4px 0 0 0}
#wr-slider-wrap label{font-size:11px;cursor:pointer}
#dri-resample-wrap{display:none;padding:4px 0 0 0}
#dri-resample-wrap .ds-label{font-size:10px;color:#666;margin-bottom:2px}
#solar-dist-controls{display:none;padding:4px 0 0 0}
#solar-dist-controls .ds-label{font-size:10px;color:#666;margin-bottom:2px}
#wr-slider-bar{display:none;background:white;border-top:1px solid #ddd;padding:6px 12px 8px;flex-shrink:0}
#wr-slider-bar .wr-sl-row{display:flex;align-items:center;gap:8px}
#wr-slider-bar input[type=range]{flex:1;margin:0}
#wr-slider-bar .wr-sl-date{font-size:13px;font-weight:600;min-width:180px;text-align:center;font-family:'Ubuntu',monospace}
#wr-slider-bar select{font-size:11px;padding:1px 4px}
#wr-slider-bar .wr-sl-btns button{border:1px solid #ccc;background:white;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:12px}
#wr-slider-bar .wr-sl-btns button:hover{background:#f0f0f0}
#wr-slider-bar .wr-sl-btns button.active{background:#e6e6e6;font-weight:600}
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

<div id="decl-expired-banner" style="display:none">
  <div id="decl-expired-inner">
    <span id="decl-expired-icon">&#9888;</span>
    <div>
      <strong>Magnetic declination model expired</strong>
      The IGRF-14 model used to correct wind directions to true north expired in <span id="decl-expiry-label"></span>.
      Wind direction readings may be increasingly inaccurate.
      To fix: update <code>_IGRF14_DECL_REF</code>, <code>_IGRF14_DECL_RATE</code>, and <code>_IGRF14_EXPIRY</code>
      in <code>modules/common.py</code> using IGRF-15 / WMM-2030 values for this site
      (-7.065&#176;S, 39.18&#176;E).
    </div>
    <button id="decl-expired-dismiss" onclick="document.getElementById('decl-expired-banner').style.display='none'">Dismiss</button>
  </div>
</div>

<div id="main">
  <div id="sidebar">

    <!-- Global Data Filters (value-based, applies to point-in-time charts) -->
    <div class="section" id="data-filter-section">
      <div class="section-title">Data Filters <span class="info-i" id="data-filter-info" onmouseenter="document.getElementById('data-filter-info-tip').style.display=''" onmouseleave="document.getElementById('data-filter-info-tip').style.display='none'">i</span></div>
      <div id="data-filter-info-tip" style="display:none;font-size:11px;color:#666;line-height:1.4;margin-bottom:6px;padding:6px 8px;background:#f5f5f5;border:1px solid #ddd;border-radius:4px;">Keep only the readings that match your criteria, for example wind direction when the temperature is at or above 32 C. Filters apply to every point-in-time chart. Span and duration charts (calm periods, dry spells, ventilation availability, rain events, cumulative rainfall) show a notice instead, because removing readings breaks the meaning of a consecutive run.</div>
      <div id="data-filter-combine" style="display:none;align-items:center;gap:8px;font-size:11px;margin-bottom:6px">
        <label style="cursor:pointer;display:flex;align-items:center;gap:3px"><input type="radio" name="df-combine" value="all" checked onchange="setDataFilterCombine('all')"> Match ALL</label>
        <label style="cursor:pointer;display:flex;align-items:center;gap:3px"><input type="radio" name="df-combine" value="any" onchange="setDataFilterCombine('any')"> Match ANY</label>
      </div>
      <div id="data-filter-list"></div>
      <button id="data-filter-add" onclick="addDataFilter()" style="font-size:11px;padding:3px 8px;border:1px solid #ccc;border-radius:3px;background:#f5f5f5;cursor:pointer;color:#555">+ Add Filter</button>
      <div id="df-count-badge" style="display:none;margin-top:5px;font-size:10px;color:#555;background:#f0f4ff;border:1px solid #c8d4f0;border-radius:3px;padding:2px 6px;font-family:monospace"></div>
    </div>
    <hr class="divider">

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

    <!-- Wind rose slider toggle -->
    <div id="wr-slider-wrap">
      <label class="cb-label"><input type="checkbox" id="wr-slider-cb" onchange="toggleWindRoseSlider(this.checked)"> Slider mode</label>
      <div id="wr-slider-gran" style="display:none;margin-top:3px">
        <label style="font-size:10px;color:#666">Window:
          <select id="wr-slider-granularity" onchange="wrSliderGranChanged()" style="font-size:10px">
            <option value="3600000">1 hour</option>
            <option value="21600000">6 hours</option>
            <option value="86400000" selected>1 day</option>
            <option value="604800000">1 week</option>
            <option value="2592000000">1 month</option>
          </select>
        </label>
      </div>
      <div style="margin-top:6px">
        <div style="font-size:10px;color:#666;margin-bottom:2px">Threshold:</div>
        <input id="wr-thresh-slider" type="range" min="0" max="40" step="1" value="0" style="width:100%;margin:0 0 4px 0;display:block" oninput="onWrThreshSlider(this.value)">
        <div style="display:flex;align-items:center;gap:4px">
          <input id="wr-thresh-input" type="number" min="0" step="0.1" placeholder="e.g. 15" style="width:65px;font-size:11px;padding:2px 4px;border:1px solid #ccc;border-radius:3px" oninput="setWrThreshold(this.value)">
          <span id="wr-thresh-unit" style="font-size:10px;color:#666">km/h</span>
        </div>
      </div>
    </div>

    <!-- Solar distribution granularity (solar-distribution only) -->
    <div id="solar-dist-controls">
      <div class="ds-label">Bin Size (W/m²)</div>
      <div class="wind-unit-notch">
        <button id="sd-25" class="wind-unit-btn" onclick="setSolarDistBin(25)">25</button><button id="sd-50" class="wind-unit-btn active" onclick="setSolarDistBin(50)">50</button><button id="sd-100" class="wind-unit-btn" onclick="setSolarDistBin(100)">100</button>
      </div>
      <div style="display:flex;align-items:center;gap:4px;margin-top:5px">
        <span style="font-size:10px;color:#666">Custom:</span>
        <input id="sd-custom" type="number" min="5" max="500" step="5" placeholder="e.g. 75" style="width:60px;font-size:10px;padding:1px 4px;border:1px solid #ccc;border-radius:3px" onchange="setSolarDistBinCustom(this.value)">
      </div>
    </div>

    <!-- DRI resampling toggle (driving rain only) -->
    <div id="dri-resample-wrap">
      <div class="ds-label">Resampling</div>
      <div class="wind-unit-notch">
        <button id="dr-5min" class="wind-unit-btn active" onclick="setDriResample('5min')">5-min</button><button id="dr-1h" class="wind-unit-btn" onclick="setDriResample('1h')">1-hour</button>
      </div>
    </div>

    <!-- Wind unit notch (shown for wind-related charts) -->
    <div id="wind-unit-wrap">
      <div class="wind-unit-notch">
        <button id="wu-kmh" class="wind-unit-btn active" onclick="setWindUnit('kmh')">km/h</button><button id="wu-ms" class="wind-unit-btn" onclick="setWindUnit('ms')">m/s</button><button id="wu-kn" class="wind-unit-btn" onclick="setWindUnit('kn')">kn</button>
      </div>
    </div>

    <!-- Ventilation threshold selector (shown for ventilation-availability) -->
    <div id="vent-thresh-wrap" style="display:none">
      <div class="wind-unit-notch" style="margin-bottom:6px">
        <button id="vtu-kmh" class="wind-unit-btn active" onclick="setWindUnit('kmh')">km/h</button><button id="vtu-ms" class="wind-unit-btn" onclick="setWindUnit('ms')">m/s</button><button id="vtu-kn" class="wind-unit-btn" onclick="setWindUnit('kn')">kn</button>
      </div>
      <div style="font-size:10px;color:#666;margin-bottom:3px">Threshold: <span id="vent-thresh-label" style="color:#333;font-weight:600">3.5 km/h</span></div>
      <input type="range" id="vent-thresh-slider" min="0" max="5" step="any" value="2" oninput="onVentThreshDrag(this.value)" onchange="onVentThreshSlide(this.value)" style="width:100%;margin:0;cursor:pointer">
    </div>

    <!-- Wind series checkboxes (shown for wind-timeseries and wind-category-dist) -->
    <div id="wind-series-controls" style="display:none">
      <div class="section">
        <div class="section-title">Series</div>
        <label class="cb-label"><input type="checkbox" id="cb-wind-avg" checked onchange="updateWindSeries()"> <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#1f77b4;vertical-align:middle"></span> Average Wind</label>
        <label class="cb-label"><input type="checkbox" id="cb-wind-gust" onchange="updateWindSeries()"> <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#ff7f0e;vertical-align:middle"></span> Peak Gust</label>
        <label class="cb-label" id="cb-wind-24h-label" style="display:none"><input type="checkbox" id="cb-wind-24h" checked onchange="updateWindSeries()"> <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#d62728;vertical-align:middle"></span> 24hr Mean</label>
      </div>
      <hr class="divider">
    </div>

    <!-- Wind category distribution controls (shown for wind-category-dist chart) -->
    <div id="wind-cat-controls">
      <div class="section">
        <div class="section-title">Wind Categories</div>
        <label class="cb-label" style="margin-bottom:6px;">Classification
          <select id="wind-cat-system" onchange="setWindCatSystem(this.value)" style="margin-left:6px">
            <option value="arc">ARC-calibrated</option>
            <option value="beaufort" selected>Beaufort (WMO)</option>
            <option value="lawson">Lawson 2001</option>
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
        <div id="arc-diag-section" style="display:none;margin-top:4px">
          <hr class="divider" style="margin-bottom:6px">
          <label class="cb-label"><input type="checkbox" id="arc-diag-cb" onchange="setArcDiagMode(this.checked)" style="margin-right:4px"> Show calibration</label>
        </div>
      </div>
    </div>

    <!-- Indoor Ventilation Calculator (shown for wind-category-dist) -->
    <div id="indoor-vent-controls" style="display:none">
      <hr class="divider">
      <div class="section">
        <div class="section-title">Indoor Air Speed</div>
        <div style="font-size:10px;color:#666;margin-bottom:6px">Enter your window and room dimensions to estimate how much indoor air movement the outdoor wind produces. Results appear as an overlay on the chart.</div>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="The window that faces the wind, where air enters. Measure the open gap (width x height), not the frame. A typical bedroom window is roughly 0.5-1.5 m wide and 1-1.5 m tall.">
          <span style="font-size:10px;color:#555">Inlet window area (m&sup2;) <span style="color:#aaa;font-size:9px">(?)</span></span>
          <input type="number" id="iv-inlet-area" min="0.01" max="20" step="0.01" value="1.0" onchange="autoUpdateIndoorVent()" style="width:80px;font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px">
        </label>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="The window on the opposite side of the room where air exits. If this is smaller than the inlet, it limits how much air can flow through.">
          <span style="font-size:10px;color:#555">Outlet window area (m&sup2;) <span style="color:#aaa;font-size:9px">(?)</span></span>
          <input type="number" id="iv-outlet-area" min="0.01" max="20" step="0.01" value="1.0" onchange="autoUpdateIndoorVent()" style="width:80px;font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px">
        </label>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="Length x width of the room. A bigger room feels less breezy for the same outdoor wind, because the same amount of air is spread over more space. Example: 4 m x 5 m = 20 m2.">
          <span style="font-size:10px;color:#555">Room floor area (m&sup2;) <span style="color:#aaa;font-size:9px">(?)</span></span>
          <input type="number" id="iv-floor-area" min="1" max="500" step="0.5" value="20" onchange="autoUpdateIndoorVent()" style="width:80px;font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px">
        </label>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="Floor to ceiling height. Taller rooms feel less breezy because the airflow spreads over more space from top to bottom.">
          <span style="font-size:10px;color:#555">Room height (m) <span style="color:#aaa;font-size:9px">(?)</span></span>
          <input type="number" id="iv-room-height" min="1.5" max="10" step="0.1" value="3.0" onchange="autoUpdateIndoorVent()" style="width:80px;font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px">
        </label>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="How exposed the building is to wind. Exposed = open land with no nearby buildings or trees. Suburban = some nearby buildings or trees. Urban = closely surrounded by buildings of similar height.">
          <span style="font-size:10px;color:#555">Shielding condition <span style="color:#aaa;font-size:9px">(?)</span></span>
          <select id="iv-shielding" onchange="autoUpdateIndoorVent()" style="font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px;width:130px">
            <option value="exposed">Exposed (open site)</option>
            <option value="suburban" selected>Suburban</option>
            <option value="urban">Urban (sheltered)</option>
          </select>
        </label>
        <label class="cb-label" style="flex-direction:column;align-items:flex-start;gap:2px;margin-bottom:5px"
          title="Roughly which direction the wind hits the inlet window. Direct means straight through; side-on means wind blows across the face of the building. If you are unsure, Diagonal is a reasonable guess.">
          <span style="font-size:10px;color:#555">Wind direction to window <span style="color:#aaa;font-size:9px">(?)</span></span>
          <select id="iv-wind-dir" onchange="autoUpdateIndoorVent()" style="font-size:11px;border:1px solid #ccc;border-radius:3px;padding:2px 4px;width:130px">
            <option value="direct">Direct (0&#176;)</option>
            <option value="angle45" selected>Diagonal (45&#176;)</option>
            <option value="sideon">Side-on (90&#176;)</option>
          </select>
        </label>
        <div style="margin-bottom:6px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:10px;color:#555;font-weight:600">Reduction Layers</span>
            <div style="display:flex;gap:4px">
              <select id="iv-layer-type" style="font-size:10px;padding:1px 3px;border:1px solid #ccc;border-radius:3px">
                <option value="mosquito-mesh">Mosquito Mesh</option>
                <option value="perforated-screen">Perforated Screen</option>
                <option value="venetian-blind">Venetian Blind</option>
              </select>
              <button onclick="addReductionLayer()" style="font-size:10px;padding:1px 6px;border:1px solid #4a90d9;border-radius:3px;cursor:pointer;background:#e8f0ff;color:#1a4a8a">Add</button>
            </div>
          </div>
          <div id="iv-layers-container" style="display:flex;flex-direction:column;gap:2px;margin-bottom:4px">
            <!-- Layers will be dynamically added here -->
          </div>
          <div style="font-size:9px;color:#888;background:#fef7e0;border:1px solid #f4d03f;border-radius:3px;padding:3px 5px;margin-bottom:2px">
            <strong>Note:</strong> Mosquito mesh values are empirically supported (von Seidlein et al. 2012). Other layer values are indicative only.
          </div>
        </div>
        <details style="margin-bottom:6px">
          <summary style="font-size:10px;color:#4a90d9;cursor:pointer;user-select:none;list-style:none">&#9656; How does this work?</summary>
          <div style="font-size:10px;color:#444;margin-top:5px;line-height:1.6;background:#f7f9fc;border-radius:4px;padding:6px 8px;border:1px solid #dde4f0">
            Fill in your room and window measurements, add reduction layers as needed, then press Apply. The chart will show green lines for each wind speed band indicating how fast the air would typically be moving inside your room when it is that windy outside.<br><br>
            Bigger windows, stronger wind, and less shelter all mean more indoor breeze. Each reduction layer (mesh, screens, blinds) cuts the airflow by multiplying the previous reduction. The overlay shows upper and lower bounds representing the cumulative effect of all active layers.<br><br>
            <a href="INDOOR_VENTILATION_CALC.pdf" target="_blank" style="color:#4a90d9">Full technical reference (PDF) &#8599;</a>
          </div>
        </details>
        <div style="display:flex;gap:4px">
          <button onclick="toggleIndoorVent()" id="iv-toggle-btn" style="flex:1;font-size:11px;padding:3px 6px;border:1px solid #4a90d9;border-radius:4px;cursor:pointer;background:#e8f0ff;color:#1a4a8a">Apply</button>
        </div>
        <div id="iv-status" style="font-size:10px;color:#888;margin-top:4px"></div>
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
          <select id="download-menu" title="Export">
            <option value="" data-i18n="downloadMenu">Download…</option>
            <option value="png" data-i18n="downloadPng">Download PNG</option>
            <option value="csv" data-i18n="downloadCsv">Export CSV</option>
            <option value="xlsx" data-i18n="downloadXlsx">Export XLSX</option>
          </select>
          <button id="download-btn" data-i18n="downloadPng" style="display:none">Download PNG</button>
          <div id="dl-spinner"></div>
        </div>
      </div>
      <div class="info-tip-fixed" id="period-info-tip"></div>
    </div>
    <div id="chart"></div>
    <div id="wr-slider-bar">
      <div class="wr-sl-row">
        <span class="wr-sl-date" id="wr-sl-date"></span>
        <input type="range" id="wr-sl-range" min="0" max="100" value="0">
        <div class="wr-sl-btns">
          <button id="wr-sl-play" onclick="wrSliderPlayPause()">&#9654;</button>
        </div>
      </div>
    </div>
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
    <div id="chart-note"></div>
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
  driResample: '5min',      // '5min' | '1h' (driving rain resampling)
  solarDistBinSize: 50,     // bin width in W/m² for solar distribution histogram
  windUnit: 'kmh',  // 'ms' = m/s, 'kmh' = km/h (default)
  ventThreshKph: 3.5, // ventilation availability threshold in km/h
  wrThreshKph: null,  // wind rose threshold in km/h (null = disabled)
  windCatSystem: 'beaufort',   // classification system for wind-category-dist
  arcDiagMode: false,          // show calibration view instead of bar chart
  windCatValueUnit: 'pct',     // count by: pct|hours|days|weeks|months
  windCatPerUnit: 'day',       // cycle: day|week|month|year
  windCatCustomBands: null,    // user-defined bands; null = use Lawson defaults
  windCatCustomUnit: 'ms',     // unit for custom threshold values: ms|kmh|kn
  indoorVent: null,            // active indoor ventilation overlay params, or null
  dataFilters: [],             // global value filters: [{id, var, op, v1, v2}]
  dataFilterCombine: 'all',    // 'all' (AND) or 'any' (OR)
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
    downloadMenu: 'Download\u2026',
    downloadCsv: 'Export CSV',
    downloadXlsx: 'Export XLSX',
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
    infoWindRose: 'Shows the frequency of wind from each of 16 compass directions, with colour bands for speed ranges. The central percentage shows how often conditions are calm (below 0.1 m/s). This reveals prevailing wind directions for orienting ventilation openings.',
    infoWindTS: 'Continuous time series of 5-minute average wind speed and peak gust. The red line shows the 12-hour running mean. Identifies storm events and the relationship between average and gust speeds.',
    infoDiurnalWind: 'Mean wind speed by hour of day, with shaded standard deviation band. The bar chart shows calm percentage by hour. Identifies the daily ventilation cycle; in coastal Tanzania, sea/land breezes create predictable diurnal patterns.',
    infoWindDist: 'Distribution of 5-minute average wind speeds. The dashed red line shows a Weibull probability distribution fit, commonly used in wind analysis. The Weibull shape (k) and scale (c) parameters characterise the site wind regime.',
    infoGustFactor: 'Each 5-minute reading plotted as gust factor (peak/avg) vs. average speed. Colour represents hour of day. The dashed red line at 2.0 marks the typical threshold for turbulent conditions. High gust factors at low speeds indicate gusty, turbulent conditions.',
    infoCalmPeriods: 'Distribution of consecutive calm period durations (wind \u22640.1 m/s). Extended calm periods mean the building relies on stack effect alone for ventilation. This directly informs whether mechanical backup ventilation is needed.',
    infoVentAvail: 'For each day, shows hours in three categories: above ventilation threshold (effective), below threshold but non-zero (marginal), and calm (below 0.1 m/s). Use the threshold slider to change the effective wind cutoff. Directly answers "what fraction of the time is natural ventilation effective?"',
        infoWindCatDist: 'Horizontal bar chart showing how often wind falls into each speed category. Switch between ARC-calibrated, Beaufort (WMO), Lawson 2001, Davenport 1975, or custom thresholds. Count by percentage or a time unit (e.g. hours per day). Hover bars for speed ranges and counts. ARC-calibrated uses a data-driven algorithm: it finds the P90 of non-calm readings as a tail threshold X, puts all speeds above X into a single open-ended tail band, then grid-searches N (2-7) equal-width bands between calm and X, picking the N that maximises the minimum band count (most even coverage). Enable "Show calibration" to see the speed distribution histogram with band boundaries and tail threshold overlaid.',
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
    infoAvgRainProfiles: 'Mean rainfall amount (bars) and rain frequency (line, right axis) averaged across the selected cycle. "Day" shows whether convective afternoon storms or nocturnal rain dominate; "Year" reveals the wet/dry season structure. Oscillation cycles show how MJO, IOD, and ENSO modulate rainfall.',
    // Data freshness
    dataUpdated: 'Data updated',
    staleWarning: 'Data may be stale (older than 2 days)',
    sensorStaleWarning: 'Weather station has not reported a new reading recently; it may be offline',
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
    downloadMenu: 'Pakua\u2026',
    downloadCsv: 'Hamisha CSV',
    downloadXlsx: 'Hamisha XLSX',
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
    sensorStaleWarning: 'Kituo cha hali ya hewa hakijatoa usomaji mpya hivi karibuni; huenda kimezimika',
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
    ct === 'wind-rain' || ct === 'solar-wind' ||
    ct === 'pre-storm' || ct === 'driving-rain' || ct === 'ventilation-windows' ||
    ct === 'wind-category-dist';
  document.getElementById('wind-unit-wrap').style.display = isWindRelated ? 'block' : 'none';
  const isVentAvail = ct === 'ventilation-availability';
  document.getElementById('vent-thresh-wrap').style.display = isVentAvail ? 'block' : 'none';
  if (isVentAvail) {
    ['kmh','ms','kn'].forEach(u => { const b = document.getElementById('vtu-'+u); if (b) b.classList.toggle('active', u === state.windUnit); });
    _syncVentSlider();
  }
  // Show/hide wind rose slider toggle
  const isWindRose = ct === 'wind-rose';
  document.getElementById('wr-slider-wrap').style.display = isWindRose ? 'block' : 'none';
  if (!isWindRose && _wrSlider.on) {
    _wrSliderStop();
    document.getElementById('wr-slider-bar').style.display = 'none';
    document.getElementById('wr-slider-cb').checked = false;
    _wrSlider.on = false;
  }
  // Show/hide wind series checkboxes
  const showWindSeries = ct === 'wind-timeseries' || ct === 'wind-category-dist';
  document.getElementById('wind-series-controls').style.display = showWindSeries ? '' : 'none';
  document.getElementById('cb-wind-24h-label').style.display = ct === 'wind-timeseries' ? '' : 'none';
  // Show/hide wind category distribution controls
  const isWindCatDist = ct === 'wind-category-dist';
  document.getElementById('wind-cat-controls').style.display = isWindCatDist ? 'block' : 'none';
  if (isWindCatDist) _updateWindCatCycleOptions();
  document.getElementById('indoor-vent-controls').style.display = isWindCatDist ? '' : 'none';
  // Show/hide DRI resampling toggle
  document.getElementById('dri-resample-wrap').style.display = ct === 'driving-rain' ? 'block' : 'none';
  // Show/hide solar distribution granularity control
  document.getElementById('solar-dist-controls').style.display = ct === 'solar-distribution' ? 'block' : 'none';
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
    html += statsRow('Calm (' + calmLabel() + ')', ws.calmPct + '%');
    html += statsRow('Prevailing dir', ws.prevailingDir);
    html += statsRow('Median', wDisp(ws.medianSpeed) + ' ' + wLabel());
    html += statsRow('95th percentile', wDisp(ws.p95Speed) + ' ' + wLabel());
    if (ct === 'wind-rose' && _computedChart && _computedChart.threshStats) {
      const ts = _computedChart.threshStats;
      html += '<div class="stats-row" style="border-top:1px solid #c8e6c9;margin-top:2px;padding-top:4px"><span class="stats-label">Threshold</span><span class="stats-value">' + (Math.round(wToUnit(ts.threshKph)*10)/10) + ' ' + wLabel() + '</span></div>';
      html += statsRow('Above threshold', ts.pct + '%');
      html += statsRow('Dominant dir (above)', ts.domDir);
    }
    if (ct === 'gust-factor' && chart) {
      html += statsRow('Mean gust factor', chart.meanGustFactor);
      html += statsRow('Median gust factor', chart.medianGustFactor);
    }
    if (ct === 'calm-periods' && chart) {
      html += statsRow('Longest calm', formatDuration(chart.longestCalmMin));
      html += statsRow('Mean calm', formatDuration(chart.meanCalmMin));
      html += statsRow('Calms/day', chart.calmsPerDay);
    }
    if (ct === 'ventilation-availability' && chart && chart.ventHist) {
      const _vtKph = state.ventThreshKph || 3.5;
      const {start: _vtS, end: _vtE} = getTimeRange();
      const _vtDaily = _computeVentDaily(chart.ventHist, _vtKph).filter(d => d.date_ms >= _vtS && d.date_ms <= _vtE);
      const _vtTot = _vtDaily.reduce((s, d) => s + d.effective_h + d.marginal_h + d.calm_h, 0);
      const _vtEff = _vtDaily.reduce((s, d) => s + d.effective_h, 0);
      const _vtEffPct = _vtTot > 0 ? Math.round(_vtEff / _vtTot * 1000) / 10 : 0;
      html += statsRow('Threshold', (Math.round(wToUnit(_vtKph) * 100) / 100) + ' ' + wLabel());
      html += statsRow('Effective %', _vtEffPct + '%');
    }
    if (ct === 'wind-category-dist') {
      const sysNames = {arc:'ARC-calibrated', beaufort:'Beaufort (WMO/1805)', lawson:'Lawson 2001', davenport:'Davenport 1975', custom:'Custom'};
      html += statsRow('Classification', sysNames[state.windCatSystem||'beaufort']||'');
      if (_computedChart && _computedChart.total) html += statsRow('Readings', _computedChart.total);
      if (state.windCatSystem === 'arc') {
        const showAvgS = document.getElementById('cb-wind-avg').checked;
        const showGustS = document.getElementById('cb-wind-gust').checked;
        const useGustMeta = showGustS && !showAvgS;
        const meta = useGustMeta ? ((ALL_DATA.raw||{}).arcMetaGust||(ALL_DATA.raw||{}).arcMeta) : (ALL_DATA.raw||{}).arcMeta;
        if (meta) {
          html += statsRow('Equal bands', meta.n_bands);
          html += statsRow('Band width', Math.round(wToUnit(meta.band_width_kph)*100)/100 + ' ' + wLabel());
          html += statsRow('Tail threshold', 'P' + meta.tail_pct + ' = ' + Math.round(wToUnit(meta.tail_x_kph)*100)/100 + ' ' + wLabel());
          html += statsRow('Min band count', meta.best_min_count);
        }
      }
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
      html += statsRow('Modal bin', (_computedChart || chart).modalBin);
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
function calmLabel() {
  if (state.windUnit === 'ms') return '\u22640.1 m/s';
  if (state.windUnit === 'kn') return '\u22640.2 kn';
  return '\u22640.36 km/h';
}
function setDriResample(r) {
  state.driResample = r;
  ['5min', '1h'].forEach(v => {
    const btn = document.getElementById('dr-' + v);
    if (btn) btn.classList.toggle('active', v === r);
  });
  updatePlot();
}


function setWindUnit(unit) {
  state.windUnit = unit;
  ['kmh', 'ms', 'kn'].forEach(u => {
    const b = document.getElementById('wu-' + u); if (b) b.classList.toggle('active', u === unit);
    const bv = document.getElementById('vtu-' + u); if (bv) bv.classList.toggle('active', u === unit);
  });
  _syncVentSlider();
  const wrUnit = document.getElementById('wr-thresh-unit');
  const wrInput = document.getElementById('wr-thresh-input');
  const wrSl = document.getElementById('wr-thresh-slider');
  if (wrUnit) wrUnit.textContent = wLabel();
  const _wrMax = Math.ceil(wToUnit(ALL_DATA.stats.wind.maxSpeed));
  if (wrSl) { wrSl.min=0; wrSl.max=_wrMax; wrSl.step=1; }
  if (state.wrThreshKph) {
    const cv = Math.round(wToUnit(state.wrThreshKph) * 10) / 10;
    if (wrInput) wrInput.value = cv;
    if (wrSl) wrSl.value = Math.min(cv, parseFloat(wrSl.max));
  }
  updatePlot();
}

function onWrThreshSlider(val) {
  const v = parseFloat(val);
  const actual = v <= 0 ? 0.01 : v;
  const inp = document.getElementById('wr-thresh-input');
  if (inp) inp.value = actual;
  setWrThreshold(actual);
}

function setWrThreshold(val) {
  const v = parseFloat(val);
  if (!val || isNaN(v) || v <= 0) {
    state.wrThreshKph = null;
    const sl = document.getElementById('wr-thresh-slider');
    if (sl) sl.value = 0;
  } else {
    if (state.windUnit === 'ms')      state.wrThreshKph = v / _KPH_TO_MS;
    else if (state.windUnit === 'kn') state.wrThreshKph = v / _KPH_TO_KN;
    else                               state.wrThreshKph = v;
    const sl = document.getElementById('wr-thresh-slider');
    if (sl) sl.value = Math.min(v, parseFloat(sl.max));
  }
  updatePlot();
}

// Ventilation threshold helpers -- histogram-based, supports any granularity
const _VENT_HIST_BIN_W = 0.1; // km/h, must match Python HIST_BIN_W

function _computeVentDaily(hist, threshKph) {
  const IH = 5 / 60;
  const threshBin = Math.ceil(threshKph / _VENT_HIST_BIN_W);
  return hist.map(day => {
    const bins = day.b;
    let lower = 0, upper = 0;
    for (let i = 0; i < bins.length; i++) {
      if (i < threshBin) lower += bins[i]; else upper += bins[i];
    }
    return {
      date_ms:     day.d,
      effective_h: Math.round(upper * IH * 10) / 10,
      marginal_h:  Math.round(Math.max(0, lower - day.c) * IH * 10) / 10,
      calm_h:      Math.round(day.c * IH * 10) / 10,
    };
  });
}

function _syncVentSlider() {
  const sl = document.getElementById('vent-thresh-slider');
  if (!sl) return;
  const kph = state.ventThreshKph || 3.5;
  let min, max, step;
  if (state.windUnit === 'ms')      { min = 0.1;  max = 5;  step = 0.05; }
  else if (state.windUnit === 'kn') { min = 0.2;  max = 10; step = 0.05; }
  else                               { min = 0.4;  max = 15; step = 0.05; }
  const val = Math.max(min, Math.min(max, Math.round(wToUnit(kph) / step) * step));
  sl.min = min; sl.max = max; sl.step = step; sl.value = val;
  _updateVentThreshLabel();
}

function _updateVentThreshLabel() {
  const kph = state.ventThreshKph || 3.5;
  const el = document.getElementById('vent-thresh-label');
  if (el) el.textContent = (Math.round(wToUnit(kph) * 100) / 100) + ' ' + wLabel();
}

function onVentThreshDrag(rawVal) {
  const v = parseFloat(rawVal);
  if (state.windUnit === 'ms')      state.ventThreshKph = v / _KPH_TO_MS;
  else if (state.windUnit === 'kn') state.ventThreshKph = v / _KPH_TO_KN;
  else                               state.ventThreshKph = v;
  _updateVentThreshLabel();
  updatePlot();
}

function onVentThreshSlide(rawVal) {
  onVentThreshDrag(rawVal);
}

function _buildVentTraces(daily) {
  const xms = daily.map(d => d.date_ms);
  return [
    {type:'scatter',mode:'lines',name:'Effective',x_ms:xms,y:daily.map(d=>d.effective_h),
     fill:'tozeroy',fillcolor:'rgba(44,160,44,0.5)',line:{color:'#2ca02c'},stackgroup:'vent'},
    {type:'scatter',mode:'lines',name:'Marginal',x_ms:xms,y:daily.map(d=>d.marginal_h),
     fill:'tonexty',fillcolor:'rgba(255,191,0,0.5)',line:{color:'#ffbf00'},stackgroup:'vent'},
    {type:'scatter',mode:'lines',name:'Calm (≤0.1 m/s)',x_ms:xms,y:daily.map(d=>d.calm_h),
     fill:'tonexty',fillcolor:'rgba(214,39,40,0.3)',line:{color:'#d62728'},stackgroup:'vent'},
  ];
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

// ── Wind Rose Slider ─────────────────────────────────────────────────────────
let _wrSlider = {on: false, playing: false, timer: null, steps: [], stepEnds: [], idx: 0, maxR: null, curR: null, animId: null};

function toggleWindRoseSlider(on) {
  _wrSlider.on = on;
  document.getElementById('wr-slider-gran').style.display = on ? 'block' : 'none';
  document.getElementById('wr-slider-bar').style.display = on ? 'block' : 'none';
  if (on) {
    _wrSlider.curR = null;
    _wrSliderBuildSteps();
    _wrSliderCalcMaxR();
    _wrSliderRender(false);
  } else {
    _wrSliderStop();
    _wrSlider.maxR = null;
    _wrSlider.curR = null;
    if (_wrSlider.animId) { cancelAnimationFrame(_wrSlider.animId); _wrSlider.animId = null; }
    updatePlot();
  }
}

function wrSliderGranChanged() {
  _wrSlider.curR = null;
  _wrSliderBuildSteps();
  _wrSliderCalcMaxR();
  _wrSliderRender(false);
}

function _wrSliderCalcMaxR() {
  // Use 90th percentile of per-window peak stacked % as the base scale.
  // Outlier windows dynamically expand the axis in _wrSliderRender.
  const gran = parseInt(document.getElementById('wr-slider-granularity').value);
  const steps = _wrSlider.steps;
  const stride = Math.max(1, Math.floor(steps.length / 200));
  const peaks = [];
  for (let si = 0; si < steps.length; si += stride) {
    const raw = filterRaw(steps[si], _wrSlider.stepEnds[si]);
    if (!raw || !raw.ts.length) continue;
    const wr = _buildWindRose(raw);
    const sums = new Array(16).fill(0);
    wr.data.filter(tr=>tr.type==='barpolar').forEach(tr => { tr.r.forEach((v, j) => { sums[j] += v; }); });
    peaks.push(Math.max(...sums));
  }
  if (!peaks.length) { _wrSlider.maxR = 10; return; }
  peaks.sort((a, b) => a - b);
  const p90 = peaks[Math.floor(peaks.length * 0.9)];
  _wrSlider.maxR = Math.ceil(p90 * 1.1) || 10;
  _wrSlider.dispR = _wrSlider.maxR; // current displayed range (animated)
}

function _wrSliderBuildSteps() {
  const r = ALL_DATA.raw;
  if (!r || !r.ts.length) return;
  const {start, end} = getTimeRange();
  const gran = parseInt(document.getElementById('wr-slider-granularity').value);
  const steps = [];
  const stepEnds = [];
  if (gran >= 2592000000) {
    // Calendar-month-aligned steps so "Jan 2026" always means Jan 1 – Feb 1 UTC
    const d0 = new Date(start);
    let y = d0.getUTCFullYear(), mo = d0.getUTCMonth();
    while (Date.UTC(y, mo, 1) < end) {
      steps.push(Date.UTC(y, mo, 1));
      stepEnds.push(Date.UTC(y, mo + 1, 1));
      mo++;
      if (mo >= 12) { mo = 0; y++; }
    }
  } else {
    let t = start;
    while (t < end) {
      steps.push(t);
      stepEnds.push(t + gran);
      t += gran;
    }
  }
  if (!steps.length) { steps.push(start); stepEnds.push(start + gran); }
  _wrSlider.steps = steps;
  _wrSlider.stepEnds = stepEnds;
  _wrSlider.idx = 0;
  const sl = document.getElementById('wr-sl-range');
  sl.min = 0;
  sl.max = Math.max(steps.length - 1, 0);
  sl.value = 0;
  sl.oninput = function() { _wrSlider.idx = +this.value; _wrSliderRender(true); };
}

function _wrSliderDateLabel(ms, gran) {
  const d = new Date(ms + 3 * 3600000);
  const mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getUTCMonth()];
  const pad = n => String(n).padStart(2, '0');
  if (gran >= 2592000000) return mo + ' ' + d.getUTCFullYear();
  if (gran >= 604800000) return d.getUTCDate() + ' ' + mo + ' \u2013 ' + _wrSliderDateLabel(ms + gran, 86400000);
  if (gran >= 86400000) return d.getUTCDate() + ' ' + mo + ' ' + d.getUTCFullYear();
  return d.getUTCDate() + ' ' + mo + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
}

function _wrSliderRender(animate) {
  if (!_wrSlider.steps.length || !ALL_DATA.raw) return;
  const gran = parseInt(document.getElementById('wr-slider-granularity').value);
  const winStart = _wrSlider.steps[_wrSlider.idx];
  const winEnd = _wrSlider.stepEnds[_wrSlider.idx];

  // Display date/time label
  if (gran < 86400000) {
    document.getElementById('wr-sl-date').textContent = _wrSliderDateLabel(winStart, gran) + ' \u2013 ' + _wrSliderDateLabel(winEnd, gran);
  } else {
    document.getElementById('wr-sl-date').textContent = _wrSliderDateLabel(winStart, gran);
  }

  // Filter raw data to window
  const raw = filterRaw(winStart, winEnd);
  if (!raw || !raw.ts.length) return;

  // Build target wind rose
  const wr = _buildWindRose(raw);
  const targetR = wr.data.map(tr => tr.r.slice());

  // Determine axis range: use base p90, but expand for outliers
  const sums = new Array(16).fill(0);
  wr.data.filter(tr=>tr.type==='barpolar').forEach(tr => { tr.r.forEach((v, j) => { sums[j] += v; }); });
  const framePeak = Math.max(...sums);
  const baseMax = _wrSlider.maxR || 10;
  const needR = framePeak > baseMax ? Math.ceil(framePeak * 1.05) : baseMax;

  const chartEl = document.getElementById('chart');
  const cfg = {responsive: true, displayModeBar: false};

  function makeLayout(rangeMax) {
    const lo = Object.assign({}, wr.layout);
    lo.polar = Object.assign({}, lo.polar);
    lo.polar.radialaxis = Object.assign({}, lo.polar.radialaxis, {range: [0, rangeMax]});
    lo.margin = {l: 60, r: 40, t: 30, b: 50};
    lo.autosize = true;
    lo.font = {family: 'Ubuntu, sans-serif', size: 12};
    return lo;
  }

  if (!animate || !_wrSlider.curR) {
    _wrSlider.curR = targetR;
    _wrSlider.dispR = needR;
    Plotly.react(chartEl, wr.data, makeLayout(needR), cfg);
    _addWrArrows(chartEl);
    return;
  }

  // Animate: interpolate bars and axis range over ~350ms
  if (_wrSlider.animId) { cancelAnimationFrame(_wrSlider.animId); _wrSlider.animId = null; }
  const fromR = _wrSlider.curR.map(tr => tr.slice());
  const fromRange = _wrSlider.dispR || baseMax;
  const dur = 350;
  const t0 = performance.now();

  function step(now) {
    let p = (now - t0) / dur;
    if (p >= 1) p = 1;
    p = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
    const interpData = wr.data.map((tr, ti) => {
      const r = fromR[ti].map((v, di) => v + (targetR[ti][di] - v) * p);
      return Object.assign({}, tr, {r: r});
    });
    const curRange = fromRange + (needR - fromRange) * p;
    Plotly.react(chartEl, interpData, makeLayout(curRange), cfg);
    if (p < 1) {
      _wrSlider.animId = requestAnimationFrame(step);
    } else {
      _wrSlider.curR = targetR;
      _wrSlider.dispR = needR;
      _wrSlider.animId = null;
    }
  }
  _wrSlider.animId = requestAnimationFrame(step);
  _wrSlider.curR = targetR;
}

function wrSliderPlayPause() {
  if (_wrSlider.playing) {
    _wrSliderStop();
  } else {
    _wrSlider.playing = true;
    document.getElementById('wr-sl-play').innerHTML = '&#9646;&#9646;';
    const gran = parseInt(document.getElementById('wr-slider-granularity').value);
    const interval = gran <= 3600000 ? 250 : gran <= 86400000 ? 400 : 600;
    _wrSlider.timer = setInterval(() => {
      if (_wrSlider.idx >= _wrSlider.steps.length - 1) {
        _wrSliderStop();
        return;
      }
      _wrSlider.idx++;
      document.getElementById('wr-sl-range').value = _wrSlider.idx;
      _wrSliderRender(true);
    }, interval);
  }
}

function _wrSliderStop() {
  _wrSlider.playing = false;
  if (_wrSlider.timer) { clearInterval(_wrSlider.timer); _wrSlider.timer = null; }
  document.getElementById('wr-sl-play').innerHTML = '&#9654;';
}

// ── Raw-Data Recomputation ────────────────────────────────────────────────────

const _CALM_KPH = 0.36; // 0.1 m/s calm threshold
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

function _resampleRawHourly(raw) {
  if (!raw) return null;
  const byH = {};
  raw.ts.forEach((t, i) => {
    const h = Math.floor(t / 3600000) * 3600000;
    if (!byH[h]) byH[h] = {t:h, w:[], s:[], c:[], p:0};
    if (raw.avgWind[i] != null) byH[h].w.push(raw.avgWind[i]);
    if (raw.windDir[i] != null) {
      const rad = raw.windDir[i] * Math.PI / 180;
      byH[h].s.push(Math.sin(rad)); byH[h].c.push(Math.cos(rad));
    }
    // precipIncr (mm per reading) sums to total mm per hour = mm/h rate
    const incr = (raw.precipIncr && raw.precipIncr[i] != null) ? raw.precipIncr[i]
               : (raw.precipRate && raw.precipRate[i] != null) ? raw.precipRate[i] / 12 : 0;
    byH[h].p += incr;
  });
  const out = {ts:[], avgWind:[], windDir:[], precipRate:[]};
  Object.keys(byH).sort((a,b) => +a - +b).forEach(k => {
    const h = byH[k];
    out.ts.push(h.t);
    out.avgWind.push(h.w.length ? Math.round(h.w.reduce((a,b)=>a+b)/h.w.length*10)/10 : null);
    if (h.s.length) {
      const ms = h.s.reduce((a,b)=>a+b)/h.s.length, mc = h.c.reduce((a,b)=>a+b)/h.c.length;
      out.windDir.push(((Math.atan2(ms,mc)*180/Math.PI)+360)%360);
    } else { out.windDir.push(null); }
    out.precipRate.push(Math.max(0, Math.round(h.p*1000)/1000));
  });
  return out.ts.length ? out : null;
}

function filterRaw(start, end) {
  const r = ALL_DATA.raw;
  if (!r) return null;
  const out = {ts:[],avgWind:[],peakWind:[],windDir:[],solar:[],precipRate:[],precipIncr:[],temp:[],humidity:[]};
  for (let i = 0; i < r.ts.length; i++) {
    if (r.ts[i] >= start && r.ts[i] <= end) {
      out.ts.push(r.ts[i]); out.avgWind.push(r.avgWind[i]);
      out.peakWind.push(r.peakWind[i]); out.windDir.push(r.windDir[i]);
      out.solar.push(r.solar[i]); out.precipRate.push(r.precipRate[i]);
      out.precipIncr.push(r.precipIncr[i]);
      out.temp.push(r.temp ? r.temp[i] : null);
      out.humidity.push(r.humidity ? r.humidity[i] : null);
    }
  }
  return out.ts.length ? out : null;
}

// ── Global Data Filters (value-based post-filter) ─────────────────────────────
// Late-stage filter applied to the raw readings before a chart is recomputed.
// Each row tests one measured variable; rows combine with ALL (AND) or ANY (OR).
// Mirrors the temp/humid substratification pattern but tests values, not time
// strata. Span/duration charts are exempt (a value filter punches holes in the
// timeline, breaking the meaning of a consecutive run) and show a notice instead.
const DATA_FILTER_VARS = {
  avgWind:   {label:'Wind speed',      label_sw:'Kasi ya upepo',     unit:'km/h',  min:0, max:60,   step:0.5},
  peakWind:  {label:'Gust speed',      label_sw:'Kasi ya kimbunga',  unit:'km/h',  min:0, max:100,  step:0.5},
  temp:      {label:'Temperature',     label_sw:'Joto',              unit:'°C', min:0, max:45,  step:0.5},
  humidity:  {label:'Humidity',        label_sw:'Unyevu',            unit:'%',     min:0, max:100,  step:1},
  solar:     {label:'Solar radiation', label_sw:'Mnururisho wa jua', unit:'W/m²', min:0, max:1100, step:10},
  precipRate:{label:'Rain rate',       label_sw:'Kasi ya mvua',      unit:'mm/h',  min:0, max:100,  step:0.5},
};
const DATA_FILTER_VAR_ORDER = ['temp','humidity','avgWind','peakWind','solar','precipRate'];

// Point-in-time charts (each reading independent) that honour the global filter.
const FILTER_SUPPORTED_CHARTS = new Set([
  'wind-rose','wind-distribution','solar-distribution','driving-rain','wind-rain',
  'solar-wind','ventilation-windows','wind-category-dist',
  'wind-timeseries','solar-timeseries','gust-factor',
  'avg-wind-profiles','avg-solar-profiles','avg-rainfall-profiles',
]);
function chartSupportsFilter(ct){ return FILTER_SUPPORTED_CHARTS.has(ct); }

// x_ms-path charts that need a passing-timestamp set built from raw data for filtering.
// (Different from _RAW_BUILDERS which rebuild the chart fully from scratch in JS.)
const FILTER_XMSCHARTS = new Set(['wind-timeseries','solar-timeseries','gust-factor']);

// Sensible per-variable default values when a new filter row is added.
const DATA_FILTER_DEFAULTS = {
  temp:      32,    // °C — hot threshold
  humidity:  70,    // % — noticeably humid
  avgWind:   10,    // km/h — light breeze (useful for cross-ventilation)
  peakWind:  15,    // km/h
  solar:     500,   // W/m² — strong midday sun
  precipRate: 2.5,  // mm/h — light rain threshold
};

let _dataFilterIdCounter = 0;

function _dfActive(f){
  return f && f.var && f.op &&
    (f.op === 'between' ? (f.v1 != null && f.v2 != null) : (f.v1 != null));
}
function getActiveDataFilters(){ return state.dataFilters.filter(_dfActive); }

function _dfTest(val, f){
  if (val == null) return false;
  if (f.op === 'ge') return val >= f.v1;
  if (f.op === 'le') return val <= f.v1;
  if (f.op === 'between'){ const lo=Math.min(f.v1,f.v2), hi=Math.max(f.v1,f.v2); return val>=lo && val<=hi; }
  return true;
}
function passesDataFilters(raw, i){
  const active = getActiveDataFilters();
  if (!active.length) return true;
  if (state.dataFilterCombine === 'any'){
    for (const f of active){ if (_dfTest(raw[f.var] ? raw[f.var][i] : null, f)) return true; }
    return false;
  }
  for (const f of active){ if (!_dfTest(raw[f.var] ? raw[f.var][i] : null, f)) return false; }
  return true;
}
// Return a new raw object with only the readings that pass; null if none.
function applyDataFilter(raw){
  if (!raw) return null;
  const active = getActiveDataFilters();
  if (!active.length) return raw;
  const keys = Object.keys(raw).filter(k => Array.isArray(raw[k]) && raw[k].length === raw.ts.length);
  const out = {}; keys.forEach(k => out[k] = []);
  for (let i = 0; i < raw.ts.length; i++){
    if (passesDataFilters(raw, i)) keys.forEach(k => out[k].push(raw[k][i]));
  }
  Object.keys(raw).forEach(k => { if (!(k in out)) out[k] = raw[k]; });  // carry non-array fields
  return out.ts.length ? out : null;
}

function _dfSummary(active, combine){
  const join = combine === 'any' ? ' OR ' : ' AND ';
  return active.map(f => {
    const m = DATA_FILTER_VARS[f.var]; const u = m ? m.unit : ''; const name = m ? m.label : f.var;
    if (f.op === 'between') return name + ' ' + f.v1 + '–' + f.v2 + u;
    return name + (f.op === 'ge' ? ' ≥ ' : ' ≤ ') + f.v1 + u;
  }).join(join);
}

function _applyFilterStatus(ct){
  const el = document.getElementById('chart-note'); if (!el) return;
  const active = getActiveDataFilters();
  if (!active.length) {
    document.getElementById('df-count-badge').style.display = 'none';
    return;
  }
  // Show live count of passing readings in the sidebar badge
  const badge = document.getElementById('df-count-badge');
  if (badge && ALL_DATA.raw) {
    const {start, end} = getTimeRange();
    const raw = applyDataFilter(filterRaw(start, end));
    const total = filterRaw(start, end);
    const passing = raw ? raw.ts.length : 0;
    const tot = total ? total.ts.length : ALL_DATA.raw.ts.length;
    badge.textContent = passing + ' / ' + tot + ' readings (' + Math.round(passing/tot*100) + '%)';
    badge.style.display = '';
  }
  const base = el.textContent ? el.textContent + '   ' : '';
  el.textContent = base + (chartSupportsFilter(ct)
    ? '● Filtered: ' + _dfSummary(active, state.dataFilterCombine)
    : '⚠ Global filter not applied to this chart type');
}

function _showNoData(){
  Plotly.react(document.getElementById('chart'), [], {
    annotations:[{text:'No data matches the filter', showarrow:false,
      font:{size:14,color:'#999'}, xref:'paper', yref:'paper', x:0.5, y:0.5}],
    margin:{l:40,r:40,t:30,b:40}, autosize:true,
  }, {responsive:true, displayModeBar:false});
}

// ── Data filter UI ────────────────────────────────────────────────────────────
function addDataFilter(){
  const varKey = 'temp';
  state.dataFilters.push({id: ++_dataFilterIdCounter, var:varKey, op:'ge', v1:DATA_FILTER_DEFAULTS[varKey], v2:null});
  renderDataFilters(); updatePlot();
}
function removeDataFilter(id){
  state.dataFilters = state.dataFilters.filter(f => f.id !== id);
  renderDataFilters(); updatePlot();
}
function setDataFilterCombine(v){ state.dataFilterCombine = v; updatePlot(); }

function renderDataFilters(){
  const wrap = document.getElementById('data-filter-list'); if (!wrap) return;
  wrap.innerHTML = '';
  if (!state.dataFilters.length){
    const empty = document.createElement('div');
    empty.style.cssText = 'font-size:10px;color:#999;font-style:italic;margin:2px 0 4px';
    empty.textContent = 'No filters. Showing all readings.';
    wrap.appendChild(empty);
  }
  state.dataFilters.forEach(f => wrap.appendChild(_renderDataFilterRow(f)));
  const combineWrap = document.getElementById('data-filter-combine');
  if (combineWrap) combineWrap.style.display = state.dataFilters.length > 1 ? 'flex' : 'none';
}

function _renderDataFilterRow(f){
  const m = DATA_FILTER_VARS[f.var] || {min:0,max:100,step:1,unit:''};
  const row = document.createElement('div');
  row.id = 'df-' + f.id;
  row.style.cssText = 'display:flex;align-items:center;gap:4px;margin-bottom:4px;flex-wrap:wrap';

  const varSel = document.createElement('select');
  varSel.style.cssText = 'font-size:10px;max-width:118px';
  DATA_FILTER_VAR_ORDER.forEach(k => {
    const mm = DATA_FILTER_VARS[k];
    varSel.appendChild(new Option(currentLang==='sw'?mm.label_sw:mm.label, k));
  });
  varSel.value = f.var;
  varSel.addEventListener('change', () => {
    f.var = varSel.value;
    f.v1 = DATA_FILTER_DEFAULTS[f.var] ?? DATA_FILTER_VARS[f.var].min;
    f.v2 = null;
    renderDataFilters(); updatePlot();
  });

  const opSel = document.createElement('select');
  opSel.style.cssText = 'font-size:10px';
  [['ge','≥'],['le','≤'],['between','↔']].forEach(([v,lbl]) => opSel.appendChild(new Option(lbl, v)));
  opSel.value = f.op;
  opSel.addEventListener('change', () => { f.op = opSel.value; if (f.op==='between' && f.v2==null) f.v2 = f.v1; renderDataFilters(); updatePlot(); });

  const mkVal = (getter, setter) => {
    const inp = document.createElement('input');
    inp.type='number'; inp.step=m.step; inp.min=m.min; inp.max=m.max;
    inp.value = getter() != null ? getter() : '';
    inp.style.cssText = 'width:50px;font-size:10px;padding:1px 3px;border:1px solid #ccc;border-radius:3px';
    inp.addEventListener('input', () => { setter(inp.value === '' ? null : parseFloat(inp.value)); updatePlot(); });
    return inp;
  };

  row.appendChild(varSel); row.appendChild(opSel);
  row.appendChild(mkVal(() => f.v1, v => f.v1 = v));
  if (f.op === 'between'){
    const dash = document.createElement('span'); dash.textContent='–'; dash.style.cssText='font-size:11px;color:#666';
    row.appendChild(dash);
    row.appendChild(mkVal(() => f.v2, v => f.v2 = v));
  }
  const unit = document.createElement('span'); unit.textContent = m.unit;
  unit.style.cssText='font-size:10px;color:#666'; row.appendChild(unit);

  const rm = document.createElement('button');
  rm.innerHTML='&times;'; rm.title='Remove filter';
  rm.style.cssText='margin-left:auto;background:none;border:none;color:#999;cursor:pointer;font-size:15px;line-height:1;padding:0 2px';
  rm.addEventListener('click', () => removeDataFilter(f.id));
  row.appendChild(rm);
  return row;
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
    calmPct: Math.round(spd.filter(v=>v<=_CALM_KPH).length/spd.length*1000)/10,
    medianSpeed: Math.round(_median(spd)*10)/10,
    p95Speed: Math.round(_pctile(spd,95)*10)/10,
    prevailingDir: (()=>{
      const cnt={};
      raw.avgWind.forEach((v,i)=>{ if(v>_CALM_KPH){ const d=_cBin(raw.windDir[i]); if(d) cnt[d]=(cnt[d]||0)+1; }});
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
  const _dfOn = getActiveDataFilters().length > 0;
  const _full = {ws:ALL_DATA.stats.wind,ss:ALL_DATA.stats.solar,ps:ALL_DATA.stats.precipitation,cs:ALL_DATA.stats.cross};
  if (!ALL_DATA.raw || (state.timeMode === 'all' && !_dfOn)) return _full;
  const {start,end} = getTimeRange();
  let raw = filterRaw(start, end);
  if (_dfOn) raw = applyDataFilter(raw);
  return raw ? _computeStats(raw) : _full;
}

let _wrSums = null;

function _addWrArrows(gd) {
  requestAnimationFrame(() => {
    try {
      gd.querySelectorAll('.wr-arrows').forEach(el => el.remove());
      if (!_wrSums || !_wrSums.some(s => s > 0)) return;
      const pg = gd.querySelector('g.polar');
      if (!pg) return;
      const mt = (pg.getAttribute('transform') || '').match(/translate\(([0-9.-]+)[,\s]+([0-9.-]+)\)/);
      if (!mt) return;
      const cx = parseFloat(mt[1]), cy = parseFloat(mt[2]);
      const fl = gd._fullLayout, pol = fl && fl.polar;
      if (!pol) return;
      const rMax = pol.radialaxis && pol.radialaxis.range ? pol.radialaxis.range[1] : Math.max(..._wrSums);
      let rPx = pol._subplot && pol._subplot.r;
      if (!rPx) {
        const dom = pol.domain || {x:[0,1],y:[0,1]};
        const mg = fl.margin || {};
        const pw = (dom.x[1]-dom.x[0]) * Math.max(1, fl.width - (mg.l||0) - (mg.r||0));
        const ph = (dom.y[1]-dom.y[0]) * Math.max(1, fl.height - (mg.t||0) - (mg.b||0));
        rPx = Math.min(pw, ph) / 2 * 0.75;
      }
      if (!rPx || !rMax) return;
      const svg = gd.querySelector('svg.main-svg');
      if (!svg) return;
      const g = document.createElementNS('http://www.w3.org/2000/svg','g');
      g.setAttribute('class','wr-arrows');
      _wrSums.forEach((s, i) => {
        if (s < 0.5) return;
        const r = (s / rMax) * rPx;
        const ang = (i * 22.5 - 90) * Math.PI / 180;
        const tx = cx + r * Math.cos(ang), ty = cy + r * Math.sin(ang);
        const aL = 8, aW = 5;
        const tpx = tx - Math.cos(ang)*aL, tpy = ty - Math.sin(ang)*aL;
        const lwx = tx - Math.sin(ang)*aW, lwy = ty + Math.cos(ang)*aW;
        const rwx = tx + Math.sin(ang)*aW, rwy = ty - Math.cos(ang)*aW;
        const p = document.createElementNS('http://www.w3.org/2000/svg','path');
        p.setAttribute('d',`M${lwx.toFixed(1)} ${lwy.toFixed(1)} L${tpx.toFixed(1)} ${tpy.toFixed(1)} L${rwx.toFixed(1)} ${rwy.toFixed(1)}`);
        p.setAttribute('stroke','#c0392b'); p.setAttribute('stroke-width','2.5');
        p.setAttribute('fill','none'); p.setAttribute('stroke-linecap','round');
        p.setAttribute('stroke-linejoin','round');
        g.appendChild(p);
      });
      svg.appendChild(g);
    } catch(e) { console.warn('wr-arrow err:',e); }
  });
}

function _buildWindRose(raw) {
  const total=raw.avgWind.filter(v=>v!=null).length, calm=raw.avgWind.filter(v=>v!=null&&v<=_CALM_KPH).length;
  const calmPct=total?Math.round(calm/total*1000)/10:0;
  const binLabels = state.windUnit === 'ms' ? _WL_MS : state.windUnit === 'kn' ? _WL_KN : _WL;
  const traces=binLabels.map((lbl,li)=>{
    const lo=_WB[li],hi=_WB[li+1],cnt={};_C16.forEach(d=>cnt[d]=0);
    raw.avgWind.forEach((v,i)=>{ if(v==null||v<=_CALM_KPH||v<lo||v>=hi) return; const d=_cBin(raw.windDir[i]); if(d) cnt[d]++; });
    return {type:'barpolar',r:_C16.map(d=>total?Math.round(cnt[d]/total*10000)/100:0),theta:_C16,name:lbl+' '+wLabel(),marker:{color:_WC[li]}};
  });
  const sums=new Array(16).fill(0);
  traces.forEach(tr=>tr.r.forEach((v,j)=>{sums[j]+=v;}));
  _wrSums = sums.slice();

  let threshStats = null;
  if (state.wrThreshKph && total) {
    const tKph = state.wrThreshKph;
    const cnt = {}; _C16.forEach(d => cnt[d] = 0);
    raw.avgWind.forEach((v, i) => {
      if (v == null || v <= tKph) return;
      const d = _cBin(raw.windDir[i]); if (d) cnt[d]++;
    });
    const rVals = _C16.map(d => total ? Math.round(cnt[d] / total * 10000) / 100 : 0);
    const abovePct = Math.round(rVals.reduce((a, b) => a + b, 0) * 10) / 10;
    const maxIdx = rVals.indexOf(Math.max(...rVals));
    const domDir = rVals[maxIdx] > 0 ? _C16[maxIdx] : 'n/a';
    threshStats = {pct: abovePct, domDir, threshKph: tKph};
    traces.push({type:'scatterpolar', mode:'lines',
      r:[...rVals, rVals[0]], theta:[..._C16, _C16[0]],
      name:'>' + Math.round(wToUnit(tKph)*10)/10 + ' ' + wLabel(),
      line:{color:'#222', width:2, dash:'dot'}, showlegend:true});
  }

  return {data:traces, calmPct, threshStats,
    layout:{polar:{angularaxis:{direction:'clockwise',rotation:90,tickmode:'array',tickvals:Array.from({length:16},(_,i)=>i*22.5),ticktext:_C16},radialaxis:{ticksuffix:'%',angle:45}},barmode:'stack',bargap:0,showlegend:true,legend:{x:1.1,y:1}}};
}

function _buildWindDist(raw) {
  const spd=raw.avgWind.filter(v=>v!=null); if(!spd.length) return null;
  const maxS=Math.min(Math.max(...spd)+1,50),step=0.5,bins=[];
  for(let b=0;b<maxS+step;b+=step) bins.push(b);
  const cnts=new Array(bins.length-1).fill(0),ctrs=bins.slice(0,-1).map((b,i)=>Math.round((b+bins[i+1])/2*10)/10);
  spd.forEach(v=>{ const j=Math.min(Math.floor(v/step),cnts.length-1); cnts[j]++; });
  const xVals = ctrs.map(c => wToUnit(c));
  return {data:[{type:'bar',name:'Frequency',x:xVals,y:cnts,marker:{color:'#1f77b4'}}], calmCount:spd.filter(v=>v<=_CALM_KPH).length,
    layout:{xaxis:{title:'Wind Speed (' + wLabel() + ')'},yaxis:{title:'Count'},showlegend:true,bargap:0.05}};
}

function _buildCalmPeriods(raw) {
  const BE=[0,5,30,60,180,360,720,1440,99999],BL=['<5min','5-30min','30min-1h','1-3h','3-6h','6-12h','12-24h','24h+'];
  const cnts=new Array(BL.length).fill(0),durs=[];
  let inC=false,si=0;
  raw.avgWind.forEach((v,i)=>{ if(v<=_CALM_KPH&&!inC){inC=true;si=i;} else if(v>_CALM_KPH&&inC){inC=false;durs.push((raw.ts[i-1]-raw.ts[si])/60000);} });
  if(inC&&raw.ts.length) durs.push((raw.ts[raw.ts.length-1]-raw.ts[si])/60000);
  durs.forEach(d=>{ for(let j=0;j<BE.length-1;j++) if(d>=BE[j]&&d<BE[j+1]){cnts[j]++;break;} });
  const longest=durs.length?Math.round(Math.max(...durs)*10)/10:0;
  const meanD=durs.length?Math.round(durs.reduce((a,b)=>a+b,0)/durs.length*10)/10:0;
  const totDays=raw.ts.length>1?(raw.ts[raw.ts.length-1]-raw.ts[0])/86400000:1;
  return {data:[{type:'bar',orientation:'h',name:'Calm Periods',y:BL,x:cnts,marker:{color:'#999'}}],
    longestCalmMin:longest, meanCalmMin:meanD, calmsPerDay:Math.round(durs.length/totDays*10)/10,
    layout:{xaxis:{title:'Number of Periods'},yaxis:{title:'Duration',autorange:'reversed'}}};
}

function setSolarDistBin(sz) {
  state.solarDistBinSize = sz;
  ['25','50','100'].forEach(v => document.getElementById('sd-'+v).classList.toggle('active', parseInt(v)===sz));
  document.getElementById('sd-custom').value = '';
  updatePlot();
}

function setSolarDistBinCustom(val) {
  const v = parseInt(val);
  if (!v || v < 5 || v > 500) return;
  state.solarDistBinSize = v;
  ['25','50','100'].forEach(b => document.getElementById('sd-'+b).classList.remove('active'));
  updatePlot();
}

function _buildSolarDist(raw) {
  const s=raw.solar.filter(v=>v!=null&&v>0); if(!s.length) return null;
  const SZ=state.solarDistBinSize||50,n=Math.ceil(Math.max(...s)/SZ),ctrs=Array.from({length:n},(_,i)=>(i+0.5)*SZ);
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
    if(!w||!r||w<=_CALM_KPH||r<=0||d==null) return;
    const v=_cBin(d); if(!v) return;
    dri[v]+=(w/3.6)*Math.pow(r,8/9); any=true;
  });
  if(!any) return null;
  const vals=_C16.map(d=>Math.round(dri[d]*100)/100);
  const facadeDRI={N:0,E:0,S:0,W:0};
  raw.ts.forEach((_,i)=>{
    const w=raw.avgWind[i],r=raw.precipRate[i],d=raw.windDir[i];
    if(!w||!r||w<=_CALM_KPH||r<=0||d==null) return;
    const dv=(w/3.6)*Math.pow(r,8/9);
    [{f:'N',deg:0},{f:'E',deg:90},{f:'S',deg:180},{f:'W',deg:270}].forEach(({f,deg})=>{
      const c=Math.cos((d-deg)*Math.PI/180); if(c>0) facadeDRI[f]+=dv*c;
    });
  });
  Object.keys(facadeDRI).forEach(k=>facadeDRI[k]=Math.round(facadeDRI[k]*10)/10);
  const di=vals.indexOf(Math.max(...vals));
  return {data:[{type:'barpolar',r:vals,theta:_C16,name:'DRI',marker:{color:'#1f77b4'}}],
    dominantDir:_C16[di], facadeDRI,
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
  const s=pts;
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

function _buildArcDiag() {
  const showAvg = document.getElementById('cb-wind-avg').checked;
  const showGust = document.getElementById('cb-wind-gust').checked;
  const useGust = showGust && !showAvg;
  const meta = useGust ? ((ALL_DATA.raw||{}).arcMetaGust||(ALL_DATA.raw||{}).arcMeta) : (ALL_DATA.raw||{}).arcMeta;
  const arcBands = useGust ? ((ALL_DATA.raw||{}).arcBandsGust||(ALL_DATA.raw||{}).arcBands) : (ALL_DATA.raw||{}).arcBands;
  if (!meta || !arcBands) return null;

  const toU = v => Math.round(wToUnit(v) * 1000) / 1000;
  const hx = meta.hist_x.map(toU);
  const calmHi = toU(arcBands[0].hi_kph);
  const tailX = toU(meta.tail_x_kph);
  const ymax = Math.max(...meta.hist_y) * 1.15;
  const bw = Math.round(wToUnit(meta.band_width_kph) * 100) / 100;
  const barW = meta.hist_x.length > 1 ? Math.round(wToUnit(meta.hist_x[1]-meta.hist_x[0])*10000)/10000 : Math.round(wToUnit(0.5)*10000)/10000;

  const traces = [];

  // Calm shading
  traces.push({type:'scatter', mode:'lines', x:[0,calmHi,calmHi,0,0], y:[0,0,ymax,ymax,0],
    fill:'toself', fillcolor:'rgba(180,180,180,0.2)', line:{width:0},
    name:'Calm (0–0.1 m/s)', hoverinfo:'skip', showlegend:true});

  // Calm boundary line
  traces.push({type:'scatter', mode:'lines', x:[calmHi,calmHi], y:[0,ymax],
    name:'Calm boundary (0.1 m/s)', line:{color:'rgba(80,80,80,0.7)', width:1.5, dash:'dot'},
    showlegend:true, hoverinfo:'skip'});

  // Histogram of non-calm speeds
  traces.push({type:'bar', x:hx, y:meta.hist_y, name:'Speed distribution',
    marker:{color:'#aac4e0'}, opacity:0.7, showlegend:true, width:barW});

  // Equal-width band boundaries (blue dashes, within dense region)
  arcBands.slice(1, -1).forEach((b, i) => {
    if (b.hi_kph == null) return;
    const bx = toU(b.hi_kph);
    traces.push({type:'scatter', mode:'lines', x:[bx,bx], y:[0,ymax],
      line:{color:'rgba(50,100,200,0.55)', width:1.5, dash:'dash'},
      showlegend:false, hoverinfo:'skip'});
  });

  // Tail threshold (bold red dash)
  traces.push({type:'scatter', mode:'lines', x:[tailX,tailX], y:[0,ymax],
    name:`Tail threshold (P${meta.tail_pct} = ${Math.round(tailX*100)/100} ${wLabel()})`,
    line:{color:'rgba(200,50,50,0.8)', width:2.5, dash:'dash'}, showlegend:true});

  const annotText = `<b>${meta.n_bands} equal bands</b><br>`
    + `Width: ${bw} ${wLabel()}<br>`
    + `Tail: P${meta.tail_pct} = ${Math.round(tailX*100)/100} ${wLabel()}<br>`
    + `Min band count: ${meta.best_min_count}`;

  return {
    data: traces,
    layout: {
      xaxis:{title:`Wind Speed (${wLabel()})`, rangemode:'nonnegative'},
      yaxis:{title:'Count', rangemode:'nonnegative'},
      showlegend:true, bargap:0,
      annotations:[{
        xref:'paper', yref:'paper', x:0.98, y:0.98, xanchor:'right', yanchor:'top',
        text:annotText, showarrow:false,
        bgcolor:'rgba(255,255,255,0.85)', bordercolor:'#ccc', borderwidth:1, font:{size:11},
      }],
    },
  };
}

function _buildWindCategoryDist(raw) {
  const KN_TO_KPH = 463/250, MS_TO_KPH = 3.6;
  const sys = state.windCatSystem || 'beaufort';
  // Defined early so arc band label construction can use it
  function fmtKph(kph) { const v=wToUnit(kph); return v>0&&v<1 ? Math.round(v*100)/100 : Math.round(v*10)/10; }
  const showAvg = document.getElementById('cb-wind-avg').checked;
  const showGust = document.getElementById('cb-wind-gust').checked;
  const useGust = showGust && !showAvg;

  // Diagnostic mode: show distribution histogram with band boundaries overlaid
  if (state.arcDiagMode && sys === 'arc') return _buildArcDiag();

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
  } else if (sys === 'arc') {
    const arcBands = useGust ? ((ALL_DATA.raw||{}).arcBandsGust||(ALL_DATA.raw||{}).arcBands) : (ALL_DATA.raw||{}).arcBands;
    if (!arcBands || !arcBands.length) return null;
    bands = arcBands.map((b, i) => {
      const lo = b.lo_kph, hi = b.hi_kph == null ? Infinity : b.hi_kph;
      const lbl = i === 0 ? 'Calm'
        : (hi === Infinity ? fmtKph(lo)+'+ '+wLabel() : fmtKph(lo)+'–'+fmtKph(hi)+' '+wLabel());
      return {label: lbl, lo_kph: lo, hi_kph: hi};
    });
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
    const parts = [];
    if (u === 'months') {
      const mo = Math.floor(mins / 43830); mins -= mo * 43830;
      if (mo) parts.push(mo + (mo===1?' month':' months'));
    }
    if (u === 'months' || u === 'weeks') {
      const wk = Math.floor(mins / 10080); mins -= wk * 10080;
      if (wk) parts.push(wk + (wk===1?' week':' weeks'));
    }
    if (u === 'months' || u === 'weeks' || u === 'days') {
      const d = Math.floor(mins / 1440); mins -= d * 1440;
      if (d) parts.push(d + (d===1?' day':' days'));
    }
    const h = Math.floor(mins / 60);
    const m = Math.round(mins - h * 60);
    if (h) parts.push(h + (h===1?' hr':' hrs'));
    if (m) parts.push(m + ' min');
    return parts.length ? parts.join(', ') : '0 min';
  }

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
    const seriesIdx = traceData.length;
    const barOpacity = series.length === 1 ? 1 : (seriesIdx === 0 ? 1 : 0.55);
    traceData.push({
      type:'bar', orientation:'h', name: s.name,
      y: bands.map(b=>b.label), x: xVals,
      marker: {color: _catColors(bands.length), opacity: barOpacity},
      hovertemplate: hover, customdata: counts,
      text: series.length === 1 ? rangeText : null,
      textposition: 'outside',
      textfont: {size: 10, color: '#555'},
      cliponaxis: false,
      showlegend: false,
      legendgroup: s.name,
    });
    // Phantom trace for a clean, representative legend swatch
    traceData.push({
      type:'scatter', x:[null], y:[null], name: s.name,
      mode:'markers',
      marker: {symbol:'square', size:10, color: seriesIdx === 0 ? '#222' : '#999'},
      showlegend: series.length > 1,
      legendgroup: s.name,
    });
  });

  // Indoor ventilation overlay: vertical line segments + hatched fill via scatter on hidden y2 axis
  let x2Max = 0.001;
  const n = bands.length;

  if (state.indoorVent && raw && raw.avgWind) {
    const iv = state.indoorVent;
    const speedArr = raw.avgWind;

    function _ivMeanByBand(meshFactor) {
      const sums = new Array(n).fill(0);
      const cnts = new Array(n).fill(0);
      speedArr.forEach(v => {
        if (v == null) return;
        for (let i = 0; i < n; i++) {
          if (v >= bands[i].lo_kph && v < bands[i].hi_kph) {
            let spd = _computeIndoorSpeedMs(v, iv);
            if (meshFactor != null) spd *= meshFactor;
            sums[i] += spd;
            cnts[i]++;
            break;
          }
        }
      });
      return sums.map((s, i) => cnts[i] > 0 ? Math.round(s / cnts[i] * 10000) / 10000 : null);
    }

    if (iv.layers.length > 0) {
      const meansHi = _ivMeanByBand(iv.reductions.optimistic);
      const meansLo = _ivMeanByBand(iv.reductions.pessimistic);
      const xFill = [], yFill = [], xHi = [], yHi = [], xLo = [], yLo = [];
      for (let i = 0; i < n; i++) {
        const hi = meansHi[i], lo = meansLo[i];
        if (hi == null || lo == null) continue;
        x2Max = Math.max(x2Max, hi);
        xFill.push(lo, hi, hi, lo, lo, null);
        yFill.push(i-0.42, i-0.42, i+0.42, i+0.42, i-0.42, null);
        xHi.push(hi, hi, null);
        yHi.push(i-0.42, i+0.42, null);
        xLo.push(lo, lo, null);
        yLo.push(i-0.42, i+0.42, null);
      }
      traceData.push({
        type: 'scatter', mode: 'none', hoverinfo: 'skip', showlegend: false,
        x: xFill, y: yFill, xaxis: 'x2', yaxis: 'y2',
        fill: 'toself', fillcolor: 'rgba(44,160,44,0.08)',
        fillpattern: {shape: '/', size: 6, fgcolor: 'rgba(44,160,44,0.55)', fillmode: 'overlay'},
      });
      traceData.push({
        type: 'scatter', mode: 'lines', hoverinfo: 'skip', showlegend: false,
        x: xHi, y: yHi, xaxis: 'x2', yaxis: 'y2',
        line: {color: '#2ca02c', width: 2.5},
      });
      traceData.push({
        type: 'scatter', mode: 'lines', hoverinfo: 'skip', showlegend: false,
        x: xLo, y: yLo, xaxis: 'x2', yaxis: 'y2',
        line: {color: '#2ca02c', width: 1.5, dash: 'dot'},
      });
      traceData.push({
        type: 'scatter', mode: 'lines', name: 'Indoor — optimistic (m/s)',
        x: [null], y: [null], yaxis: 'y2', xaxis: 'x2',
        line: {color: '#2ca02c', width: 2.5}, showlegend: true,
      });
      traceData.push({
        type: 'scatter', mode: 'lines', name: 'Indoor — conservative (m/s)',
        x: [null], y: [null], yaxis: 'y2', xaxis: 'x2',
        line: {color: '#2ca02c', width: 1.5, dash: 'dot'}, showlegend: true,
      });
    } else {
      const meansNoLayers = _ivMeanByBand(null);
      const xLine = [], yLine = [];
      for (let i = 0; i < n; i++) {
        const spd = meansNoLayers[i];
        if (spd == null) continue;
        x2Max = Math.max(x2Max, spd);
        xLine.push(spd, spd, null);
        yLine.push(i-0.42, i+0.42, null);
      }
      traceData.push({
        type: 'scatter', mode: 'lines', hoverinfo: 'skip', showlegend: false,
        x: xLine, y: yLine, xaxis: 'x2', yaxis: 'y2',
        line: {color: '#d62728', width: 2.5},
      });
      traceData.push({
        type: 'scatter', mode: 'lines', name: 'Indoor, no layers (m/s)',
        x: [null], y: [null], yaxis: 'y2', xaxis: 'x2',
        line: {color: '#d62728', width: 2.5}, showlegend: true,
      });
    }
  }

  const layoutObj = {
    xaxis: {title: xTitle},
    yaxis: {autorange:'reversed', title:'', automargin: true},
    showlegend: true,
    barmode: 'group',
    margin: {l: 10, r: 150, t: 30, b: 50},
  };
  if (state.indoorVent) {
    layoutObj.xaxis2 = {
      title: {text: '<b>Mean indoor air speed (m/s)</b>', font: {color: '#2ca02c', size: 12}},
      side: 'top', overlaying: 'x',
      showgrid: false, zeroline: false,
      range: [0, x2Max * 1.3],
      tickformat: '.3f',
      tickfont: {color: '#2ca02c'},
    };
    layoutObj.yaxis2 = {
      overlaying: 'y',
      range: [n - 0.5, -0.5],
      showticklabels: false, showgrid: false,
      zeroline: false, showline: false, fixedrange: true,
    };
    layoutObj.margin = Object.assign({}, layoutObj.margin, {t: 55});
  }
  return {data: traceData, layout: layoutObj, total: grandTotal};
}

// ── Indoor Ventilation Calculator ─────────────────────────────────────────────
// ΔCp values (inlet face minus leeward outlet face) from AIVC TN44 Table 3.5
// (i) exposed, (ii) suburban, (iii) urban; low-rise buildings (up to 3 storeys).
// Direct: Face 1 @0 deg vs Face 3 @0 deg.
// 45 deg: Face 1 @45 deg vs adjacent Face 4 @45 deg.
// Side-on: side Face 2 vs side Face 4 @0 deg wind (small differential).
const _IV_DELTA_CP = {
  exposed:  { direct: 1.20, angle45: 0.75, sideon: 0.30 },
  suburban: { direct: 0.70, angle45: 0.45, sideon: 0.10 },
  urban:    { direct: 0.45, angle45: 0.35, sideon: 0.05 },
};

function _computeIndoorSpeedMs(v_kph, iv) {
  if (v_kph == null || v_kph <= 0) return 0;
  const v_ms = v_kph / 3.6;
  const dCp = (_IV_DELTA_CP[iv.shielding] || _IV_DELTA_CP.suburban)[iv.windDir] || 0.45;
  const dP = dCp * 0.5 * 1.2 * v_ms * v_ms;
  const Ao = (iv.inletArea * iv.outletArea) / Math.sqrt(iv.inletArea ** 2 + iv.outletArea ** 2);
  const Q = 0.6 * Ao * Math.sqrt(2 * dP / 1.2);
  return Q / (Math.sqrt(iv.floorArea) * iv.roomHeight);
}

// Reduction layer definitions with empirically supported and indicative values
const _REDUCTION_LAYERS = {
  'mosquito-mesh': {
    name: 'Mosquito Mesh',
    optimistic: 0.48,
    pessimistic: 0.36,
    empiricallySupported: true
  },
  'perforated-screen': {
    name: 'Perforated Screen',
    optimistic: 0.65,
    pessimistic: 0.45,
    empiricallySupported: false
  },
  'venetian-blind': {
    name: 'Venetian Blind',
    optimistic: 0.70,
    pessimistic: 0.50,
    empiricallySupported: false
  }
};

let _activeLayerCount = 0;

function addReductionLayer() {
  const layerType = document.getElementById('iv-layer-type').value;
  const layerDef = _REDUCTION_LAYERS[layerType];
  if (!layerDef) return;
  _activeLayerCount++;
  const layerId = `layer-${_activeLayerCount}`;
  const container = document.getElementById('iv-layers-container');
  const layerDiv = document.createElement('div');
  layerDiv.id = layerId;
  layerDiv.style.cssText = 'display:flex;align-items:center;gap:4px;padding:2px 4px;border:1px solid #ddd;border-radius:3px;background:#f9f9f9;font-size:10px';
  layerDiv.innerHTML = `
    <span style="flex:1;color:#333">${layerDef.name}</span>
    <button onclick="muteLayer('${layerId}')" id="${layerId}-mute"
            style="font-size:9px;padding:1px 4px;border:1px solid #666;border-radius:2px;cursor:pointer;background:#fff;color:#666;min-width:35px">Mute</button>
    <button onclick="soloLayer('${layerId}')" id="${layerId}-solo"
            style="font-size:9px;padding:1px 4px;border:1px solid #f39c12;border-radius:2px;cursor:pointer;background:#fff;color:#f39c12;min-width:35px">Solo</button>
    <button onclick="removeLayer('${layerId}')"
            style="font-size:9px;padding:1px 4px;border:1px solid #d9534f;border-radius:2px;cursor:pointer;background:#f2dede;color:#a94442;min-width:35px">Remove</button>
  `;
  layerDiv.dataset.layerType = layerType;
  layerDiv.dataset.muted = 'false';
  layerDiv.dataset.soloed = 'false';
  container.appendChild(layerDiv);
  updateLayerVisuals();
  if (state.indoorVent) { applyIndoorVent(); }
}

function removeLayer(layerId) {
  const layerElement = document.getElementById(layerId);
  if (layerElement) {
    layerElement.remove();
    updateLayerVisuals();
    if (state.indoorVent) { applyIndoorVent(); }
  }
}

function muteLayer(layerId) {
  const layerElement = document.getElementById(layerId);
  const muteButton = document.getElementById(`${layerId}-mute`);
  if (!layerElement || !muteButton) return;
  const isMuted = layerElement.dataset.muted === 'true';
  if (isMuted) {
    layerElement.dataset.muted = 'false';
    muteButton.style.background = '#fff';
    muteButton.style.color = '#666';
    muteButton.style.borderColor = '#666';
    muteButton.textContent = 'Mute';
  } else {
    layerElement.dataset.muted = 'true';
    muteButton.style.background = '#d9534f';
    muteButton.style.color = '#fff';
    muteButton.style.borderColor = '#d9534f';
    muteButton.textContent = 'Muted';
  }
  updateLayerVisuals();
  if (state.indoorVent) { applyIndoorVent(); }
}

function soloLayer(layerId) {
  const layerElement = document.getElementById(layerId);
  const soloButton = document.getElementById(`${layerId}-solo`);
  if (!layerElement || !soloButton) return;
  const isSoloed = layerElement.dataset.soloed === 'true';
  if (isSoloed) {
    layerElement.dataset.soloed = 'false';
    soloButton.style.background = '#fff';
    soloButton.style.color = '#f39c12';
    soloButton.style.borderColor = '#f39c12';
    soloButton.textContent = 'Solo';
  } else {
    layerElement.dataset.soloed = 'true';
    soloButton.style.background = '#f39c12';
    soloButton.style.color = '#fff';
    soloButton.style.borderColor = '#f39c12';
    soloButton.textContent = 'Solo\'d';
  }
  updateLayerVisuals();
  if (state.indoorVent) { applyIndoorVent(); }
}

function updateLayerVisuals() {
  const container = document.getElementById('iv-layers-container');
  const layers = Array.from(container.children);
  const anySoloed = layers.some(layer => layer.dataset.soloed === 'true');
  layers.forEach(layer => {
    layer.style.opacity = getLayerActiveState(layer, anySoloed) ? '1' : '0.3';
  });
}

function getLayerActiveState(layerElement, anySoloed) {
  if (anySoloed) { return layerElement.dataset.soloed === 'true'; }
  return layerElement.dataset.muted === 'false';
}

function getActiveLayers() {
  const container = document.getElementById('iv-layers-container');
  const layers = Array.from(container.children);
  const anySoloed = layers.some(layer => layer.dataset.soloed === 'true');
  return layers
    .filter(layer => getLayerActiveState(layer, anySoloed))
    .map(layer => _REDUCTION_LAYERS[layer.dataset.layerType]);
}

function computeCompoundReduction() {
  const activeLayers = getActiveLayers();
  if (activeLayers.length === 0) { return { optimistic: 1.0, pessimistic: 1.0 }; }
  let optimistic = 1.0, pessimistic = 1.0;
  for (const layer of activeLayers) {
    optimistic *= layer.optimistic;
    pessimistic *= layer.pessimistic;
  }
  return { optimistic, pessimistic };
}

function _readIndoorVentParams() {
  const inletArea  = parseFloat(document.getElementById('iv-inlet-area').value);
  const outletArea = parseFloat(document.getElementById('iv-outlet-area').value);
  const floorArea  = parseFloat(document.getElementById('iv-floor-area').value);
  const roomHeight = parseFloat(document.getElementById('iv-room-height').value);
  const shielding  = document.getElementById('iv-shielding').value;
  const windDir    = document.getElementById('iv-wind-dir').value;
  const layers     = getActiveLayers();
  const reductions = computeCompoundReduction();
  const status     = document.getElementById('iv-status');
  if (!inletArea || !outletArea || !floorArea || !roomHeight ||
      inletArea <= 0 || outletArea <= 0 || floorArea <= 0 || roomHeight <= 0) {
    if (status) status.textContent = 'Enter all values > 0.';
    return null;
  }
  if (status) status.textContent = '';
  return { inletArea, outletArea, floorArea, roomHeight, shielding, windDir, layers, reductions };
}

function autoUpdateIndoorVent() {
  if (state.indoorVent) { applyIndoorVent(); }
}

function toggleIndoorVent() {
  const toggleBtn = document.getElementById('iv-toggle-btn');
  if (state.indoorVent) {
    clearIndoorVent();
    toggleBtn.textContent = 'Apply';
    toggleBtn.style.background = '#e8f0ff';
    toggleBtn.style.color = '#1a4a8a';
    toggleBtn.style.borderColor = '#4a90d9';
  } else {
    applyIndoorVent();
    toggleBtn.textContent = 'Remove';
    toggleBtn.style.background = '#f2dede';
    toggleBtn.style.color = '#a94442';
    toggleBtn.style.borderColor = '#d9534f';
  }
}

function applyIndoorVent() {
  const iv = _readIndoorVentParams();
  if (!iv) return;
  state.indoorVent = iv;
  const status = document.getElementById('iv-status');
  if (status) {
    let html = 'Overlay active';
    if (ALL_DATA && ALL_DATA.raw && ALL_DATA.raw.avgWind) {
      const spds = ALL_DATA.raw.avgWind
        .filter(v => v != null && v > 0)
        .map(v => _computeIndoorSpeedMs(v, iv))
        .sort((a, b) => a - b);
      if (spds.length) {
        const p50 = spds[Math.floor(spds.length * 0.50)];
        const p90 = spds[Math.floor(spds.length * 0.90)];
        const mx  = spds[spds.length - 1];
        const f = (v, factor) => (v * factor).toFixed(3);
        const stats = iv.layers.length > 0
          ? `Median ${f(p50,iv.reductions.pessimistic)}–${f(p50,iv.reductions.optimistic)}&nbsp;m/s, p90 ${f(p90,iv.reductions.pessimistic)}–${f(p90,iv.reductions.optimistic)}&nbsp;m/s, max ${f(mx,iv.reductions.pessimistic)}–${f(mx,iv.reductions.optimistic)}&nbsp;m/s`
          : `Median ${f(p50,1)}&nbsp;m/s, p90 ${f(p90,1)}&nbsp;m/s, max ${f(mx,1)}&nbsp;m/s`;
        html += `<br><span style="font-size:10px;color:#555">${stats}</span>`;
      }
    }
    status.innerHTML = html;
  }
  updatePlot();
}

function clearIndoorVent() {
  state.indoorVent = null;
  const status = document.getElementById('iv-status');
  if (status) status.textContent = '';
  updatePlot();
}

function setWindCatSystem(sys) {
  state.windCatSystem = sys;
  const sel = document.getElementById('wind-cat-system');
  if (sel) sel.value = sys;
  document.getElementById('wind-cat-custom-section').style.display = sys==='custom' ? '' : 'none';
  document.getElementById('arc-diag-section').style.display = sys==='arc' ? '' : 'none';
  if (sys==='custom' && !state.windCatCustomBands) {
    state.windCatCustomBands = _DEFAULT_CUSTOM_BANDS.map(b => Object.assign({},b));
  }
  if (sys !== 'arc') {
    state.arcDiagMode = false;
    const cb = document.getElementById('arc-diag-cb');
    if (cb) cb.checked = false;
  }
  updatePlot();
}
function setArcDiagMode(on) {
  state.arcDiagMode = on;
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
  document.getElementById('chart-note').textContent = chart.note || '';
  _applyFilterStatus(ct);

  // If wind rose slider is active, delegate rendering to slider
  if (ct === 'wind-rose' && _wrSlider.on) {
    _wrSliderBuildSteps();
    _wrSliderRender();
    updateStatsPanel();
    return;
  }

  const config = {responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['zoom2d','pan2d','select2d','lasso2d','zoomIn2d','zoomOut2d','resetScale2d','sendDataToCloud','hoverClosestCartesian','hoverCompareCartesian','toggleSpikelines','toImage']};

  // Pre-compute filtered chart for aggregated chart types (needed by stats panel)
  _computedChart = null;
  const _dfApplies = getActiveDataFilters().length > 0 && chartSupportsFilter(ct);
  if (_RAW_BUILDERS[ct] && ALL_DATA.raw) {
    const alwaysCompute = ct === 'wind-category-dist' || ct === 'wind-rose' || ct === 'solar-distribution';
    const use1h = ct === 'driving-rain' && state.driResample === '1h';

    if (use1h && state.timeMode === 'all' && !_dfApplies) {
      // Use Python-precomputed hourly DRI (avoids resampling 13k readings in JS)
      _computedChart = ALL_DATA.driHourly || null;
    } else if (alwaysCompute || state.timeMode !== 'all' || use1h || _dfApplies) {
      const {start, end} = getTimeRange();
      let raw = (alwaysCompute && state.timeMode === 'all' && !_dfApplies) ? ALL_DATA.raw : filterRaw(start, end);
      if (_dfApplies) raw = applyDataFilter(raw);
      if (_dfApplies && !raw) { updateSidebarControls(); _showNoData(); updateStatsPanel(); return; }
      if (use1h && raw) raw = _resampleRawHourly(raw);
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
    Plotly.react(chartEl, _computedChart.data, layout, config);
    if (ct === 'wind-rose') _addWrArrows(chartEl);
    state.savedZoom = null;
    return;
  }

  // Build Plotly traces
  const traces = [];
  let chartData = chart.data || [];
  if (ct === 'ventilation-availability' && chart.ventHist) {
    chartData = _buildVentTraces(_computeVentDaily(chart.ventHist, state.ventThreshKph || 3.5));
  }
  const {start: rngStart, end: rngEnd} = getTimeRange();

  // For x_ms-path charts that support filtering, build a set of passing timestamps.
  let _xmsPassingTs = null;
  if (getActiveDataFilters().length > 0 && FILTER_XMSCHARTS.has(ct)) {
    const _fraw = applyDataFilter(filterRaw(rngStart, rngEnd));
    _xmsPassingTs = _fraw ? new Set(_fraw.ts) : new Set();
    if (_xmsPassingTs.size === 0) { updateSidebarControls(); _showNoData(); updateStatsPanel(); return; }
  }

  for (const trace of chartData) {
    const t = Object.assign({}, trace);

    // Convert x_ms timestamps to EAT strings, applying time range filter + value filter
    if (t.x_ms) {
      const xms = t.x_ms;
      const keys = Object.keys(t).filter(k => k !== 'x_ms' && Array.isArray(t[k]) && t[k].length === xms.length);
      const mask = xms.map(ms => ms >= rngStart && ms <= rngEnd && (_xmsPassingTs === null || _xmsPassingTs.has(ms)));
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


  // ── Wind unit conversion for pre-computed traces ──────────────────────────
  // (Only reached when _computedChart is null, i.e. timeMode==='all' or no raw data)
  // Clone nested axis objects before modifying to avoid mutating embedded chart data.
  if (layout.xaxis) layout.xaxis = Object.assign({}, layout.xaxis);
  if (layout.yaxis) layout.yaxis = Object.assign({}, layout.yaxis);
  const _needsConv = state.windUnit !== 'kmh';
  const _cvt = v => v != null ? wToUnit(v) : null;
  if (ct === 'wind-rose') {
    const binLabels = state.windUnit === 'ms' ? _WL_MS : state.windUnit === 'kn' ? _WL_KN : _WL;
    traces.forEach((tr, i) => {
      if (i < binLabels.length) {
        tr.name = binLabels[i] + ' ' + wLabel();
        tr.hovertemplate = '%{theta}<br>Frequency: %{r:.2f}%<br>' + tr.name + '<extra></extra>';
      }
    });
  } else if (ct === 'wind-timeseries') {
    if (_needsConv) traces.forEach(tr => { if (tr.y) tr.y = tr.y.map(_cvt); });
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
    // Apply series visibility from checkboxes
    const showAvg = document.getElementById('cb-wind-avg').checked;
    const showGust = document.getElementById('cb-wind-gust').checked;
    const show24h = document.getElementById('cb-wind-24h').checked;
    if (traces[0]) { traces[0].visible = showAvg; traces[0].hovertemplate = '%{x}<br>Avg Wind: %{y:.1f} ' + wLabel() + '<extra></extra>'; }
    if (traces[1]) { traces[1].visible = showGust; traces[1].hovertemplate = '%{x}<br>Peak Gust: %{y:.1f} ' + wLabel() + '<extra></extra>'; }
    if (traces[2]) { traces[2].visible = show24h; traces[2].hovertemplate = '%{x}<br>12h Mean: %{y:.1f} ' + wLabel() + '<extra></extra>'; }
  } else if (ct === 'diurnal-wind') {
    // Traces 0-2: wind speed (mean, +1SD, -1SD); trace 3: Calm % on y2
    if (_needsConv) traces.slice(0, 3).forEach(tr => { if (tr.y) tr.y = tr.y.map(_cvt); });
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
    if (traces[0]) traces[0].hovertemplate = '%{x}:00 EAT<br>Mean Wind: %{y:.1f} ' + wLabel() + '<extra></extra>';
    if (traces[1]) traces[1].hoverinfo = 'skip';
    if (traces[2]) traces[2].hoverinfo = 'skip';
    if (traces[3]) traces[3].hovertemplate = '%{x}:00 EAT<br>Calm: %{y:.1f}%<extra></extra>';
  } else if (ct === 'wind-distribution') {
    if (_needsConv) traces.forEach(tr => { if (tr.x) tr.x = tr.x.map(v => wToUnit(v)); });
    if (layout.xaxis) layout.xaxis.title = 'Wind Speed (' + wLabel() + ')';
    if (traces[0]) traces[0].hovertemplate = 'Wind Speed: %{x:.1f} ' + wLabel() + '<br>Count: %{y}<extra></extra>';
    if (traces[1] && traces[1].type === 'scatter') traces[1].hovertemplate = 'Wind Speed: %{x:.1f} ' + wLabel() + '<br>Fitted: %{y:.1f}<extra></extra>';
  } else if (ct === 'gust-factor') {
    if (_needsConv) {
      traces.forEach(tr => {
        if (tr.x) tr.x = tr.x.map(v => wToUnit(v));
        if (tr.customdata) tr.customdata = tr.customdata.map(row => [row[0], wToUnit(row[1]), row[2]]);
      });
      if (layout.shapes) layout.shapes = layout.shapes.map(s => {
        if (s.x0 != null && s.x1 != null) return Object.assign({}, s, {x0: wToUnit(s.x0), x1: wToUnit(s.x1)});
        return s;
      });
    }
    if (layout.xaxis) layout.xaxis.title = 'Average Wind Speed (' + wLabel() + ')';
    traces.forEach(tr => {
      tr.hovertemplate =
        'Time: %{customdata[2]} EAT<br>' +
        'Hour: %{customdata[0]}:00<br>' +
        'Avg speed: %{x:.1f} ' + wLabel() + '<br>' +
        'Peak gust: %{customdata[1]:.1f} ' + wLabel() + '<br>' +
        'Gust factor: %{y:.2f}<extra></extra>';
    });
  } else if (ct === 'calm-periods') {
    if (traces[0]) traces[0].hovertemplate = 'Duration: %{y}<br>Count: %{x}<extra></extra>';
  } else if (ct === 'ventilation-availability') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Effective: %{y:.1f} h<extra></extra>';
    if (traces[1]) traces[1].hovertemplate = '%{x}<br>Marginal: %{y:.1f} h<extra></extra>';
    if (traces[2]) traces[2].hovertemplate = '%{x}<br>Calm: %{y:.1f} h<extra></extra>';
  } else if (ct === 'solar-timeseries') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Solar Radiation: %{y:.1f} W/m²<extra></extra>';
  } else if (ct === 'daily-insolation') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Insolation: %{y:.3f} kWh/m²<extra></extra>';
  } else if (ct === 'diurnal-solar') {
    if (traces[0]) traces[0].hovertemplate = '%{x}:00 EAT<br>Mean Radiation: %{y:.1f} W/m²<extra></extra>';
    if (traces[1]) traces[1].hoverinfo = 'skip';
    if (traces[2]) traces[2].hoverinfo = 'skip';
  } else if (ct === 'solar-distribution') {
    if (traces[0]) traces[0].hovertemplate = 'Radiation: %{x:.0f} W/m²<br>Count: %{y}<extra></extra>';
  } else if (ct === 'clearness-index') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Clearness Index (Kt): %{y:.3f}<extra></extra>';
  } else if (ct === 'peak-solar-hours') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Peak Solar Hours: %{y:.2f} h<extra></extra>';
  } else if (ct === 'cumulative-rainfall') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Cumulative Rainfall: %{y:.1f} mm<extra></extra>';
  } else if (ct === 'daily-rainfall') {
    if (traces[0]) traces[0].hovertemplate = '%{x}<br>Daily Rainfall: %{y:.2f} mm<extra></extra>';
  } else if (ct === 'rainfall-intensity') {
    if (traces[0]) traces[0].hovertemplate = 'Rate: %{x} mm/h<br>Count: %{y}<extra></extra>';
  } else if (ct === 'diurnal-rainfall') {
    if (traces[0]) traces[0].hovertemplate = '%{x}:00 EAT<br>Mean Rainfall: %{y:.3f} mm<extra></extra>';
    if (traces[1]) traces[1].hovertemplate = '%{x}:00 EAT<br>Rain Frequency: %{y:.1f}%<extra></extra>';
  } else if (ct === 'dry-spells') {
    if (traces[0]) traces[0].hovertemplate = 'Duration: %{y}<br>Count: %{x}<extra></extra>';
  } else if (ct === 'driving-rain') {
    if (traces[0]) traces[0].hovertemplate = 'Direction: %{theta}<br>DRI: %{r:.2f}<extra></extra>';
  } else if (ct === 'wind-rain') {
    if (traces[0]) traces[0].hovertemplate = 'Wind: %{x} km/h<br>Rain rate: %{y} mm/h<br>Count: %{z}<extra></extra>';
  } else if (ct === 'solar-wind') {
    if (_needsConv && traces[0] && traces[0].y) traces[0].y = traces[0].y.map(_cvt);
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
    if (traces[0]) {
      const _htHour = traces[0].customdata ? '<br>Hour: %{customdata}:00 EAT' : '';
      traces[0].hovertemplate = 'Solar: %{x:.1f} W/m²<br>Wind: %{y:.1f} ' + wLabel() + _htHour + '<extra></extra>';
    }
  } else if (ct === 'ventilation-windows') {
    if (traces[0] && traces[0].z) {
      const _condLabels = ['No Data', 'Effective', 'Marginal', 'Closed'];
      traces[0].text = traces[0].z.map(row => row.map(v => _condLabels[v] || ''));
      traces[0].hovertemplate = 'Hour: %{x}:00 EAT<br>Date: %{y}<br>Condition: %{text}<extra></extra>';
    }
  } else if (ct === 'pre-storm') {
    if (_needsConv && traces[0] && traces[0].y) traces[0].y = traces[0].y.map(_cvt);
    if (traces[0]) traces[0].name = 'Wind Speed (' + wLabel() + ')';
    if (layout.yaxis) layout.yaxis.title = 'Wind Speed (' + wLabel() + ')';
    if (traces[0]) traces[0].hovertemplate = 'Hours from rain start: %{x:.2f}<br>Wind: %{y:.1f} ' + wLabel() + '<extra></extra>';
    if (traces[1]) traces[1].hovertemplate = 'Hours from rain start: %{x:.2f}<br>Solar: %{y:.1f} W/m²<extra></extra>';
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
  // Value filter: build passing-timestamp set from raw data if any filters are active.
  let _paPassingTs = null;
  if (getActiveDataFilters().length > 0) {
    const _paRaw = applyDataFilter(filterRaw(paRngStart, paRngEnd));
    _paPassingTs = _paRaw ? new Set(_paRaw.ts) : new Set();
  }
  for (let i = 0; i < x_ms.length; i++) {
    if (x_ms[i] < paRngStart || x_ms[i] > paRngEnd) continue;
    if (_paPassingTs !== null && !_paPassingTs.has(x_ms[i])) continue;
    const ci = getCategoryIdx(x_ms[i]);
    if (ci < 0 || ci >= nCats) continue;
    const v = vals[i];
    if (v == null || !isFinite(v)) continue;
    sums[ci] += v;
    sumsq[ci] += v * v;
    counts[ci]++;
    if (ct === 'avg-wind-profiles') {
      if (rawY[i] != null && isFinite(rawY[i]) && rawY[i] <= _CALM_KPH) calmCounts[ci]++;
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
    traces.push({type:'bar', name:'Calm % (' + calmLabel() + ')', x:xArr, y:calmPcts, yaxis:'y2', marker:{color:'rgba(180,180,180,0.5)'}, textposition:'none', hovertemplate:'%{x}<br>Calm (' + calmLabel() + '): %{y:.1f}%<extra></extra>'});
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
    const probTrace = {type:'scatter', x:xArr, y:rainProbs, name:'Rain Frequency %', yaxis:'y2', hovertemplate:'%{x}<br>Rain freq: %{y:.1f}%<extra></extra>'};
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
    y2cfg = {title:'Rain Frequency %', overlaying:'y', side:'right', range:[0,100], showgrid:false};
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
  let fetchDateDisplay = df.fetchTime || 'Unknown';
  if (df.fetchTime) {
    const parts = df.fetchTime.split(' ');
    if (parts.length >= 2) {
      const fetchDate = new Date(parts[0] + 'T' + parts[1] + ':00Z');
      const now = new Date();
      const diffDays = (now - fetchDate) / DAY_MS;
      if (diffDays > 2) {
        warnHtml = ' <span class="stale-warn" title="' + t('staleWarning') + '">\u26a0</span>';
      }
      const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
      const d = fetchDate.getUTCDate();
      const suffix = (d === 1 || d === 21 || d === 31) ? 'st' : (d === 2 || d === 22) ? 'nd' : (d === 3 || d === 23) ? 'rd' : 'th';
      fetchDateDisplay = d + suffix + ' ' + months[fetchDate.getUTCMonth()] + ' ' + fetchDate.getUTCFullYear();
    }
  }
  if (df.dateMax) {
    const lastReading = new Date(df.dateMax.replace(' ', 'T'));
    const diffDays = (new Date() - lastReading) / DAY_MS;
    if (diffDays > 2) {
      warnHtml += ' <span class="stale-warn" title="' + t('sensorStaleWarning') + '">⚠</span>';
    }
  }
  lines.push('Omnisense last updated: ' + fetchDateDisplay + warnHtml);
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

// ── Chart data export ────────────────────────────────────────────────────────
// Exports exactly what is plotted right now, for the period currently selected.
// Reads the traces back off the chart element rather than from any particular
// render function, so it stays correct for every chart type.
//
// CSV and XLSX are both produced from the same array of rows, so the two
// formats cannot drift apart.

function csvField(v) {
  const s = String(v == null ? '' : v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function rowsToCsv(rows) {
  return rows.map(r => r.map(csvField).join(',')).join('\n');
}

function axisLabelText(ax) {
  const tt = ax && ax.title;
  const s = typeof tt === 'string' ? tt : (tt && tt.text) || '';
  return s.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
}

// Legend entries are drawn as traces holding a single null point, and threshold
// bands carry no data worth exporting.
function visibleDataTraces(traces) {
  return traces.filter(tr => {
    if (tr.visible === false || tr.visible === 'legendonly') return false;
    if (tr.type === 'table') return false;
    const n = Math.max((tr.x && tr.x.length) || 0, (tr.y && tr.y.length) || 0);
    if (n === 0) return false;
    const xNull = !tr.x || tr.x.every(v => v == null);
    const yNull = !tr.y || tr.y.every(v => v == null);
    if (xNull && yNull) return false;
    return true;
  });
}

// The plotted data as an array of rows. Returns null when nothing is plotted.
function chartExportRows() {
  const el = document.getElementById('chart');
  const traces = visibleDataTraces((el && el.data) || []);
  const layout = (el && el.layout) || {};
  if (traces.length === 0) return null;

  const xLabel = axisLabelText(layout.xaxis) || 'X';
  const yLabel = axisLabelText(layout.yaxis) || 'Value';
  const names = traces.map((tr, i) =>
    (tr.name ? String(tr.name).replace(/<[^>]*>/g, '').trim() : '') || ('Series ' + (i + 1)));

  const rows = [];
  const titleEl = document.getElementById('bar-title');
  if (titleEl && titleEl.textContent) rows.push([titleEl.textContent]);
  rows.push(['Exported', new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC']);
  rows.push([]);

  // A shared x-axis (timestamps, categories) pivots into one column per series,
  // which is what a spreadsheet wants. Scatter and histogram data have no shared
  // x, so those fall back to one row per point.
  const canPivot = traces.every(tr =>
    tr.x && tr.y && tr.x.length === tr.y.length && tr.x.every(v => v == null || typeof v === 'string'));

  if (canPivot) {
    const pivot = new Map();
    traces.forEach((tr, ti) => {
      for (let i = 0; i < tr.x.length; i++) {
        const k = tr.x[i];
        if (k == null) continue;
        let row = pivot.get(k);
        if (!row) { row = new Array(traces.length).fill(''); pivot.set(k, row); }
        row[ti] = tr.y[i] == null ? '' : tr.y[i];
      }
    });
    let keys = [...pivot.keys()];
    // Timestamps sort chronologically; category axes keep the plotted order.
    if (keys.every(k => !isNaN(Date.parse(k)))) keys.sort((a, b) => Date.parse(a) - Date.parse(b));
    rows.push([xLabel, ...names]);
    for (const k of keys) rows.push([k, ...pivot.get(k)]);
  } else {
    rows.push(['Series', xLabel, yLabel]);
    traces.forEach((tr, ti) => {
      const n = Math.max((tr.x && tr.x.length) || 0, (tr.y && tr.y.length) || 0);
      for (let i = 0; i < n; i++) {
        const xv = tr.x ? tr.x[i] : '';
        const yv = tr.y ? tr.y[i] : '';
        if (xv == null && yv == null) continue;
        rows.push([names[ti], xv == null ? '' : xv, yv == null ? '' : yv]);
      }
    });
  }
  return rows;
}

// ── XLSX writing ─────────────────────────────────────────────────────────────
// An .xlsx file is a ZIP of XML parts. Both are written here by hand, because
// the dashboards are single self-contained pages that cannot pull in a library.

function xmlEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&apos;')
                  // Control characters are not legal in XML 1.0 and corrupt the file.
                  .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '');
}

// 0 -> A, 25 -> Z, 26 -> AA. Needed because a wide export runs past column Z.
function colRef(n) {
  let s = '';
  n += 1;
  while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = (n - r - 1) / 26; }
  return s;
}

// Values that are numbers, or strings that are wholly numeric, become real
// numeric cells so a spreadsheet can sort and chart them.
function isNumericCell(v) {
  if (typeof v === 'number') return isFinite(v);
  if (typeof v !== 'string' || v.trim() === '') return false;
  return /^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(v.trim());
}

function sheetXml(rows) {
  const parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'];
  rows.forEach((row, ri) => {
    const r = ri + 1;
    let cells = '';
    row.forEach((v, ci) => {
      if (v == null || v === '') return;          // sparse rows are legal and smaller
      const ref = colRef(ci) + r;
      if (isNumericCell(v)) cells += `<c r="${ref}"><v>${Number(v)}</v></c>`;
      else cells += `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xmlEsc(v)}</t></is></c>`;
    });
    parts.push(`<row r="${r}">${cells}</row>`);
  });
  parts.push('</sheetData></worksheet>');
  return parts.join('');
}

const _XLSX_PARTS = {
  '[Content_Types].xml':
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    + '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    + '</Types>',
  '_rels/.rels':
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    + '</Relationships>',
  'xl/_rels/workbook.xml.rels':
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    + '</Relationships>',
};

function workbookXml(sheetName) {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    + ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    + `<sheets><sheet name="${xmlEsc(sheetName)}" sheetId="1" r:id="rId1"/></sheets></workbook>`;
}

let _crcTable = null;
function crc32(bytes) {
  if (!_crcTable) {
    _crcTable = new Int32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      _crcTable[i] = c;
    }
  }
  let c = -1;
  for (let i = 0; i < bytes.length; i++) c = (c >>> 8) ^ _crcTable[(c ^ bytes[i]) & 0xFF];
  return (c ^ -1) >>> 0;
}

// Raw DEFLATE via the platform, when it offers it. A wide "All time" export runs
// to tens of megabytes as XML, so this matters. Falls back to storing the parts
// uncompressed, which is still a valid ZIP.
async function deflateRaw(bytes) {
  if (typeof CompressionStream === 'undefined') return null;
  try {
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream('deflate-raw'));
    const out = new Uint8Array(await new Response(stream).arrayBuffer());
    return out.length < bytes.length ? out : null;
  } catch (e) { return null; }
}

async function zipBytes(files) {
  const enc = new TextEncoder();
  const chunks = [], central = [];
  let offset = 0;
  const u16 = v => [v & 0xFF, (v >>> 8) & 0xFF];
  const u32 = v => [v & 0xFF, (v >>> 8) & 0xFF, (v >>> 16) & 0xFF, (v >>> 24) & 0xFF];

  for (const f of files) {
    const nameBytes = enc.encode(f.name);
    const raw = enc.encode(f.data);
    const crc = crc32(raw);
    const deflated = await deflateRaw(raw);
    const body = deflated || raw;
    const method = deflated ? 8 : 0;
    const header = [...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(method),
      ...u16(0), ...u16(0),                       // DOS time/date, left at zero
      ...u32(crc), ...u32(body.length), ...u32(raw.length),
      ...u16(nameBytes.length), ...u16(0)];
    chunks.push(Uint8Array.from(header), nameBytes, body);
    central.push({name: nameBytes, crc, csize: body.length, usize: raw.length, offset, method});
    offset += header.length + nameBytes.length + body.length;
  }

  const cdStart = offset;
  for (const e of central) {
    const rec = [...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(e.method),
      ...u16(0), ...u16(0), ...u32(e.crc), ...u32(e.csize), ...u32(e.usize),
      ...u16(e.name.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(0), ...u32(e.offset)];
    chunks.push(Uint8Array.from(rec), e.name);
    offset += rec.length + e.name.length;
  }
  chunks.push(Uint8Array.from([...u32(0x06054b50), ...u16(0), ...u16(0),
    ...u16(central.length), ...u16(central.length),
    ...u32(offset - cdStart), ...u32(cdStart), ...u16(0)]));

  const total = chunks.reduce((a, c) => a + c.length, 0);
  const out = new Uint8Array(total);
  let p = 0;
  for (const c of chunks) { out.set(c, p); p += c.length; }
  return out;
}

async function rowsToXlsx(rows, sheetName) {
  const files = Object.entries(_XLSX_PARTS).map(([name, data]) => ({name, data}));
  files.push({name: 'xl/workbook.xml', data: workbookXml(sheetName || 'Data')});
  files.push({name: 'xl/worksheets/sheet1.xml', data: sheetXml(rows)});
  return zipBytes(files);
}

// ── Download plumbing ────────────────────────────────────────────────────────

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function triggerCsvDownload(text, filename) {
  // BOM so Excel reads the degree and delta symbols as UTF-8.
  triggerDownload(new Blob(['﻿' + text], {type: 'text/csv;charset=utf-8;'}), filename);
}

async function triggerXlsxDownload(rows, sheetName, filename) {
  const bytes = await rowsToXlsx(rows, sheetName);
  triggerDownload(new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}), filename);
}

// The title bar already names the dataset, chart and period, which makes it the
// natural basis for the filename.
function exportFilename(ext) {
  const titleEl = document.getElementById('bar-title');
  const slug = s => s.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 90);
  const base = titleEl && titleEl.textContent ? slug(titleEl.textContent) : 'chart';
  const n = new Date(), p = v => String(v).padStart(2, '0');
  const ts = `${n.getFullYear()}${p(n.getMonth() + 1)}${p(n.getDate())}_${p(n.getHours())}${p(n.getMinutes())}`;
  return `ARC_${base}_${ts}.${ext}`;
}

function exportChartCsv() {
  const rows = chartExportRows();
  if (!rows) return;
  triggerCsvDownload(rowsToCsv(rows), exportFilename('csv'));
}

function exportChartXlsx() {
  const rows = chartExportRows();
  if (!rows) return;
  triggerXlsxDownload(rows, 'Chart data', exportFilename('xlsx'));
}

function exportCurrentCsv() { exportChartCsv(); }

function exportCurrentXlsx() { exportChartXlsx(); }

// ── Download PNG ─────────────────────────────────────────────────────────────
// Renders the current chart to PNG. Invoked from the export menu.
function downloadChartPng() {
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
}

// Export menu: acts on selection, then returns to its placeholder label.
document.getElementById('download-menu').addEventListener('change', function() {
  const choice = this.value;
  this.selectedIndex = 0;
  if (choice === 'png') downloadChartPng();
  else if (choice === 'csv') exportCurrentCsv();
  else if (choice === 'xlsx') exportCurrentXlsx();
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

  // Magnetic declination model expiry warning
  if (ALL_DATA.declModelExpired) {
    const banner = document.getElementById('decl-expired-banner');
    document.getElementById('decl-expiry-label').textContent = ALL_DATA.declModelExpiry || 'January 2030';
    banner.style.display = '';
  }

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

  // Set wind rose threshold slider max to actual data max
  const _wrSlInit = document.getElementById('wr-thresh-slider');
  if (_wrSlInit) _wrSlInit.max = Math.ceil(wToUnit(ALL_DATA.stats.wind.maxSpeed));

  // Initial render
  updatePlot();
}

init();

// Initialize with a default mosquito mesh layer
document.getElementById('iv-layer-type').value = 'mosquito-mesh';
addReductionLayer();
</script>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build ARC Weather Station dashboard")
    parser.add_argument("--csv", help="Path to specific CSV file")
    args = parser.parse_args()

    build_dashboard(csv_path=args.csv)
