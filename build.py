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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from modules.common import (
    load_weather_csv, find_latest_csv, build_available_periods,
    spike_filter, to_eat_ms, TIMEZONE,
)
from modules import wind, solar, precipitation, cross_variable


# ── Configuration ─────────────────────────────────────────────────────────────
OUTPUT_FILE = Path("index.html")
LOGO_TRIM_PATH = Path("logo/logotrim.png")
LOGO_FULL_PATH = Path("logo/logo.png")

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

    data_blob = {
        "meta": periods,
        "charts": all_charts,
        "stats": all_stats,
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
    json_str = json.dumps(data_blob, separators=(',', ':'), default=str)

    html = HTML_TEMPLATE
    html = html.replace('__DATA__', json_str)
    html = html.replace('__LOGO_B64__', header_logo_b64)
    html = html.replace('__LOGO_ASPECT__', str(round(header_logo_aspect, 4)))
    html = html.replace('__WATERMARK_LOGO_B64__', watermark_logo_b64)
    html = html.replace('__WATERMARK_LOGO_ASPECT__', str(round(watermark_logo_aspect, 4)))

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
.section{border:1px solid #e0e0e0;border-radius:6px;padding:8px;background:#fafafa}
.section-title{font-weight:600;font-size:12px;color:#555;margin-bottom:6px;display:flex;align-items:center;gap:4px}
select,button,input{font-family:inherit}
select,input[type="date"],input[type="number"]{font-size:12px;padding:3px 5px;border:1px solid #ccc;border-radius:4px;background:white}
select{cursor:pointer;max-width:100%}
select:focus{outline:none;border-color:#4a90d9}
label{font-size:12px}
.cb-label{display:flex;align-items:center;gap:4px;font-size:12px;cursor:pointer;padding:2px 0}
.control-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.control-row label{font-size:12px;color:#666;white-space:nowrap}
.hidden{display:none!important}
.info-i{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;background:#999;color:white;font-size:9px;font-style:italic;font-weight:700;cursor:help;flex-shrink:0;line-height:1;font-family:Georgia,'Times New Roman',serif}
.info-i:hover{background:#666}
#info-fixed-tip,.info-tip-fixed{display:none;position:fixed;background:#333;color:white;font-size:12px;font-family:'Ubuntu',sans-serif;padding:6px 9px;border-radius:4px;line-height:1.5;width:320px;max-width:90vw;z-index:9999;pointer-events:none;white-space:normal}
#chart-info-tip{display:none;position:fixed;background:#333;color:white;font-size:12px;font-family:'Ubuntu',sans-serif;padding:6px 9px;border-radius:4px;line-height:1.5;width:320px;max-width:90vw;z-index:9999;pointer-events:none;white-space:normal}
.stats-panel{background:#f0f8f0;border:1px solid #c8e6c9;border-radius:6px;padding:8px;font-size:12px;margin-top:6px}
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
#rain-events-table th{background:#f0f0f0;padding:4px 6px;text-align:left;cursor:pointer;border-bottom:2px solid #ddd;position:sticky;top:0}
#rain-events-table th:hover{background:#e0e0e0}
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

    <!-- Stats Panel (populated by JS) -->
    <div class="stats-panel" id="stats-panel">
      <h4 data-i18n="statistics">Statistics</h4>
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
            <option value="combined" data-i18n="combinedGroup">Combined</option>
          </select>
          <select id="chart-select">
            <option value="wind-rose" data-i18n="windRose">Wind Rose</option>
            <option value="wind-timeseries" data-i18n="windTimeSeries">Wind Speed (Time Series)</option>
            <option value="diurnal-wind" data-i18n="diurnalWind">Diurnal Wind Pattern</option>
            <option value="wind-distribution" data-i18n="windDistribution">Wind Speed Distribution</option>
            <option value="gust-factor" data-i18n="gustFactor">Gust Factor</option>
            <option value="calm-periods" data-i18n="calmPeriods">Calm Periods</option>
            <option value="ventilation-availability" data-i18n="ventAvailability">Ventilation Availability</option>
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
            <th data-i18n="evStart">Start</th>
            <th data-i18n="evEnd">End</th>
            <th data-i18n="evDuration">Duration</th>
            <th data-i18n="evTotal">Total (mm)</th>
            <th data-i18n="evPeakRate">Peak (mm/h)</th>
            <th data-i18n="evMeanRate">Mean (mm/h)</th>
            <th data-i18n="evWindDir">Wind Dir</th>
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

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  chartType: 'wind-rose',
  timeMode: 'all',
  betweenStart: null,
  betweenEnd: null,
  selectedSeason: null,
  selectedMonth: null,
  selectedWeek: null,
  selectedDay: null,
  savedZoom: null,
};

let currentLang = 'en';

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
    diurnalWind: 'Diurnal Wind Pattern',
    windDistribution: 'Wind Speed Distribution',
    gustFactor: 'Gust Factor',
    calmPeriods: 'Calm Periods',
    ventAvailability: 'Ventilation Availability',
    solarTimeSeries: 'Solar Radiation (Time Series)',
    dailyInsolation: 'Daily Insolation',
    diurnalSolar: 'Diurnal Solar Pattern',
    solarDistribution: 'Solar Distribution',
    clearnessIndex: 'Clearness Index',
    peakSolarHours: 'Peak Solar Hours',
    cumulativeRainfall: 'Cumulative Rainfall',
    dailyRainfall: 'Daily Rainfall',
    rainfallIntensity: 'Rainfall Intensity',
    diurnalRainfall: 'Diurnal Rainfall Pattern',
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
    infoSolarTS: 'Continuous time series of global horizontal irradiance (W/m2). Shows solar intensity patterns, cloudy vs. clear days, and seasonal trends. Directly related to solar heat gain through windows and roofing.',
    infoDailyInsol: 'Daily solar insolation (kWh/m2/day) calculated by integrating 5-minute radiation readings. The dashed red line shows the typical clear-sky reference for this latitude (~5.5 kWh/m2/day). Days below this line indicate significant cloud cover.',
    infoDiurnalSolar: 'Mean solar radiation by hour, with standard deviation shading. The shape of the diurnal curve (and deviation from clear-sky) characterises the site solar regime. Asymmetry (morning vs. afternoon) affects orientation-dependent heat gain.',
    infoSolarDist: 'Distribution of solar radiation readings during daylight hours (excluding night-time zeros). Bimodal distributions indicate frequent cloud interruption; unimodal high peaks indicate clear-sky dominance.',
    infoClearness: 'Daily clearness index Kt = measured insolation / theoretical extraterrestrial radiation. Colour bands indicate: clear (Kt > 0.65, green), partly cloudy (0.35-0.65, yellow), overcast (Kt < 0.35, blue). Separates the effects of season from weather.',
    infoPSH: 'Peak solar hours = daily insolation divided by 1 kW/m2. Equivalent to the number of hours at full 1000 W/m2 irradiance. A standard metric for solar energy assessment and solar heat gain potential.',
    infoCumRain: 'Corrected cumulative rainfall over the entire period. The raw sensor totals are corrected for counter resets by detecting negative jumps and adding the pre-reset total. The slope indicates rain intensity.',
    infoDailyRain: 'Daily rainfall totals derived from the corrected cumulative series. Colour indicates intensity category: light (< 2.5 mm, green), moderate (2.5-7.5 mm, yellow), heavy (7.5-25 mm, orange), very heavy (> 25 mm, red).',
    infoRainIntensity: 'Distribution of instantaneous rainfall rates during rain events. Log scale because most rain is light but rare intense events matter most for building design. The 95th percentile intensity is a key design parameter.',
    infoDiurnalRain: 'For each hour, shows mean rainfall amount (bars) and the probability that it is raining (red line). In tropical coastal locations, rain often follows a diurnal pattern with afternoon convective storms.',
    infoDrySpells: 'Distribution of consecutive periods with no rainfall. Dry spells indicate periods when windows can remain open without rain risk. Extended dry spells during the wet season may indicate unusual weather patterns.',
    infoRainEvents: 'Each detected rain event shown as a table row with start time, duration, total rainfall, peak and mean intensity, and prevailing wind direction. Events detected by grouping consecutive readings with rate > 0, allowing up to 15-minute gaps.',
    infoDRI: 'The driving rain index (DRI) quantifies wind-driven rain exposure on building facades. The polar chart shows which directions deliver the most driving rain. This directly informs which facades need the most weather protection.',
    infoWindRainCo: 'Joint frequency distribution of wind speed and rainfall rate during rain events. Shows how often rain coincides with strong winds. If most rain falls during calm periods, windows can have rain shelters and stay open.',
    infoSolarWind: 'Explores the relationship between solar heating and wind speed. In coastal tropical locations, solar heating drives thermal convection, which may correlate with afternoon sea breezes. Colour indicates hour of day.',
    infoPreStorm: 'Composite plot showing the average behaviour of wind speed and solar radiation around rain events. Created by aligning all detected rain events at t=0 (event start) and averaging. Shows whether there are reliable pre-storm signatures.',
    infoVentWin: 'For each hour of each day, classifies the ventilation condition as: Effective (green, adequate wind, no rain), Marginal (yellow, some wind or light rain), or Closed (red, heavy rain). This is the synthesis chart combining all three weather variables.',
    infoPeriod: 'Select a time period to filter the data. "All time" shows the complete dataset. Other options let you zoom into specific seasons, months, weeks, or individual days.',
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
    diurnalWind: 'Mtindo wa Upepo wa Kila Siku',
    windDistribution: 'Usambazaji wa Kasi ya Upepo',
    gustFactor: 'Kipengele cha Upepo Mkali',
    calmPeriods: 'Vipindi vya Utulivu',
    ventAvailability: 'Upatikanaji wa Hewa',
    solarTimeSeries: 'Mionzi ya Jua (Mfuatano)',
    dailyInsolation: 'Jua la Kila Siku',
    diurnalSolar: 'Mtindo wa Jua wa Kila Siku',
    solarDistribution: 'Usambazaji wa Jua',
    clearnessIndex: 'Fahirisi ya Uwazi',
    peakSolarHours: 'Masaa ya Jua Kali',
    cumulativeRainfall: 'Mvua ya Jumla',
    dailyRainfall: 'Mvua ya Kila Siku',
    rainfallIntensity: 'Kiwango cha Mvua',
    diurnalRainfall: 'Mtindo wa Mvua wa Kila Siku',
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
  },
};

function t(key) { return (I18N[currentLang] || I18N.en)[key] || I18N.en[key] || key; }

// ── Helpers ──────────────────────────────────────────────────────────────────
function toEATString(ms) {
  return new Date(ms + 3 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' ');
}

function formatDuration(minutes) {
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
  'diurnal-wind': 'infoDiurnalWind',
  'wind-distribution': 'infoWindDist',
  'gust-factor': 'infoGustFactor',
  'calm-periods': 'infoCalmPeriods',
  'ventilation-availability': 'infoVentAvail',
  'solar-timeseries': 'infoSolarTS',
  'daily-insolation': 'infoDailyInsol',
  'diurnal-solar': 'infoDiurnalSolar',
  'solar-distribution': 'infoSolarDist',
  'clearness-index': 'infoClearness',
  'peak-solar-hours': 'infoPSH',
  'cumulative-rainfall': 'infoCumRain',
  'daily-rainfall': 'infoDailyRain',
  'rainfall-intensity': 'infoRainIntensity',
  'diurnal-rainfall': 'infoDiurnalRain',
  'dry-spells': 'infoDrySpells',
  'rain-events': 'infoRainEvents',
  'driving-rain': 'infoDRI',
  'wind-rain': 'infoWindRainCo',
  'solar-wind': 'infoSolarWind',
  'pre-storm': 'infoPreStorm',
  'ventilation-windows': 'infoVentWin',
};

const CATEGORY_CHARTS = {
  wind: [
    {value: 'wind-rose', i18n: 'windRose', en: 'Wind Rose'},
    {value: 'wind-timeseries', i18n: 'windTimeSeries', en: 'Wind Speed (Time Series)'},
    {value: 'diurnal-wind', i18n: 'diurnalWind', en: 'Diurnal Wind Pattern'},
    {value: 'wind-distribution', i18n: 'windDistribution', en: 'Wind Speed Distribution'},
    {value: 'gust-factor', i18n: 'gustFactor', en: 'Gust Factor'},
    {value: 'calm-periods', i18n: 'calmPeriods', en: 'Calm Periods'},
    {value: 'ventilation-availability', i18n: 'ventAvailability', en: 'Ventilation Availability'},
  ],
  solar: [
    {value: 'solar-timeseries', i18n: 'solarTimeSeries', en: 'Solar Radiation (Time Series)'},
    {value: 'daily-insolation', i18n: 'dailyInsolation', en: 'Daily Insolation'},
    {value: 'diurnal-solar', i18n: 'diurnalSolar', en: 'Diurnal Solar Pattern'},
    {value: 'solar-distribution', i18n: 'solarDistribution', en: 'Solar Distribution'},
    {value: 'clearness-index', i18n: 'clearnessIndex', en: 'Clearness Index'},
    {value: 'peak-solar-hours', i18n: 'peakSolarHours', en: 'Peak Solar Hours'},
  ],
  precipitation: [
    {value: 'cumulative-rainfall', i18n: 'cumulativeRainfall', en: 'Cumulative Rainfall'},
    {value: 'daily-rainfall', i18n: 'dailyRainfall', en: 'Daily Rainfall'},
    {value: 'rainfall-intensity', i18n: 'rainfallIntensity', en: 'Rainfall Intensity'},
    {value: 'diurnal-rainfall', i18n: 'diurnalRainfall', en: 'Diurnal Rainfall Pattern'},
    {value: 'dry-spells', i18n: 'drySpells', en: 'Dry Spells'},
    {value: 'rain-events', i18n: 'rainEvents', en: 'Rain Events'},
  ],
  combined: [
    {value: 'driving-rain', i18n: 'drivingRain', en: 'Driving Rain Index'},
    {value: 'wind-rain', i18n: 'windRain', en: 'Wind-Rain Coincidence'},
    {value: 'solar-wind', i18n: 'solarWind', en: 'Solar-Wind Correlation'},
    {value: 'pre-storm', i18n: 'preStorm', en: 'Pre-Storm Signatures'},
    {value: 'ventilation-windows', i18n: 'ventWindows', en: 'Ventilation Windows'},
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
}

// ── Stats Panel ──────────────────────────────────────────────────────────────
function updateStatsPanel() {
  const ct = state.chartType;
  const content = document.getElementById('stats-content');
  const panel = document.getElementById('stats-panel');
  let html = '';

  const chart = getChartById(ct);
  const ws = ALL_DATA.stats.wind;
  const ss = ALL_DATA.stats.solar;
  const ps = ALL_DATA.stats.precipitation;
  const cs = ALL_DATA.stats.cross;

  if (ct.startsWith('wind') || ct === 'diurnal-wind' || ct === 'gust-factor' || ct === 'calm-periods' || ct === 'ventilation-availability') {
    html += statsRow('Mean speed', ws.meanSpeed + ' km/h');
    html += statsRow('Max speed', ws.maxSpeed + ' km/h');
    html += statsRow('Max gust', ws.maxGust + ' km/h');
    html += statsRow('Calm %', ws.calmPct + '%');
    html += statsRow('Prevailing dir', ws.prevailingDir);
    html += statsRow('Median', ws.medianSpeed + ' km/h');
    html += statsRow('95th percentile', ws.p95Speed + ' km/h');
    if (ws.weibullK) html += statsRow('Weibull k', ws.weibullK);
    if (ws.weibullC) html += statsRow('Weibull c', ws.weibullC);
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
  } else if (ct.startsWith('solar') || ct === 'daily-insolation' || ct === 'diurnal-solar' || ct === 'clearness-index' || ct === 'peak-solar-hours') {
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
  } else if (ct.startsWith('cumulative') || ct.startsWith('daily-rain') || ct.startsWith('rainfall') || ct === 'diurnal-rainfall' || ct === 'dry-spells' || ct === 'rain-events') {
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
    if (ct === 'diurnal-rainfall' && chart) {
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

// ── Chart Rendering ──────────────────────────────────────────────────────────
function updatePlot() {
  const ct = state.chartType;
  const chart = getChartById(ct);
  if (!chart) return;

  const chartEl = document.getElementById('chart');
  const sel = document.getElementById('chart-select');
  const titleEl = document.getElementById('bar-title');
  titleEl.textContent = currentLang === 'sw' ? (chart.title_sw || chart.title) : chart.title;

  updateSidebarControls();
  updateStatsPanel();

  // Handle rain events table
  if (ct === 'rain-events') {
    renderRainEventsTable(chart);
    return;
  }

  // Build Plotly traces
  const traces = [];
  const chartData = chart.data || [];

  for (const trace of chartData) {
    const t = Object.assign({}, trace);

    // Convert x_ms timestamps to EAT strings
    if (t.x_ms) {
      t.x = t.x_ms.map(ms => toEATString(ms));
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

  const config = {responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['zoom2d','pan2d','select2d','lasso2d','zoomIn2d','zoomOut2d','resetScale2d','sendDataToCloud','hoverClosestCartesian','hoverCompareCartesian','toggleSpikelines','toImage']};

  Plotly.react(chartEl, traces, layout, config);
  state.savedZoom = null;
}

function renderRainEventsTable(chart) {
  const tbody = document.getElementById('rain-events-body');
  tbody.innerHTML = '';
  const events = chart.events || [];
  for (const ev of events) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + toEATString(ev.start_ms) + '</td>' +
      '<td>' + toEATString(ev.end_ms) + '</td>' +
      '<td>' + formatDuration(ev.duration_min) + '</td>' +
      '<td>' + ev.total_mm + '</td>' +
      '<td>' + ev.peak_rate + '</td>' +
      '<td>' + ev.mean_rate + '</td>' +
      '<td>' + ev.wind_dir + '</td>';
    tbody.appendChild(tr);
  }
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
}

// ── Period Selectors ─────────────────────────────────────────────────────────
function populatePeriodSelectors() {
  const m = ALL_DATA.meta;
  if (!m) return;

  const ssel = document.getElementById('season-select');
  const msel = document.getElementById('month-select');
  const wsel = document.getElementById('week-select');
  const dsel = document.getElementById('day-select');

  if (m.availableSeasons) m.availableSeasons.forEach(s => ssel.add(new Option(s.label, JSON.stringify(s))));
  if (m.availableMonths) m.availableMonths.forEach(s => msel.add(new Option(s.label, JSON.stringify(s))));
  if (m.availableWeeks) m.availableWeeks.forEach(s => wsel.add(new Option(s.label, JSON.stringify(s))));
  if (m.availableDays) m.availableDays.forEach(s => dsel.add(new Option(s.label, JSON.stringify(s))));

  // Default date range
  if (m.dateRange) {
    const fmt = ms => new Date(ms).toISOString().slice(0, 10);
    document.getElementById('date-start').value = fmt(m.dateRange.min);
    document.getElementById('date-end').value = fmt(m.dateRange.max);
  }
}

function updateTimeModeVisibility() {
  const mode = state.timeMode;
  document.getElementById('between-inputs').classList.toggle('hidden', mode !== 'between');
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
  const lines = [];
  let warnHtml = '';
  if (df.fetchTime) {
    const parts = df.fetchTime.split(' ');
    if (parts.length >= 2) {
      const fetchDate = new Date(parts[0] + 'T' + parts[1] + ':00Z');
      const now = new Date();
      const diffDays = (now - fetchDate) / (1000 * 60 * 60 * 24);
      if (diffDays > 2) {
        warnHtml = ' <span class="stale-warn" title="' + t('staleWarning') + '">\u26a0</span>';
      }
    }
  }
  lines.push('Omnisense last updated: ' + (df.fetchTime || 'Unknown') + warnHtml);
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
