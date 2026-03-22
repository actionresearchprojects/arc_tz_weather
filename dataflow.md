# Data Pipeline: ARC Weather Station Dashboard

This document describes how data flows from the Omnisense weather station to the published dashboard.

## Overview

```
Omnisense Sensor (30B40014)
    |
    v
omnisense.com (cloud storage)
    |
    v  [arc_tz_temp_humid daily workflow fetches CSV]
arc_tz_temp_humid/data/omnisense/omnisense_*.csv
    |
    v  [arc_tz_weather daily workflow copies CSV]
arc_tz_weather/data/omnisense/omnisense_*.csv
    |
    v  [build.py processes data]
arc_tz_weather/index.html (self-contained dashboard)
    |
    v  [GitHub Actions pushes, triggers main site sync]
actionresearchprojects.github.io (published)
```

## Step 1: Omnisense CSV Fetch

The `arc_tz_temp_humid` repository runs `fetch_omnisense.py` daily at 04:00 UTC. This script logs into omnisense.com and downloads the latest sensor CSV covering the past 90 days. The CSV contains multiple sensor sections; the weather station section starts with the header `sensorId,port,read_date,avg_wind_speed_kph,...`.

## Step 2: CSV Copy

This project's GitHub Action (`update-dashboard-data.yml`) runs at 05:00 UTC, one hour after the temp/humidity fetch. It uses git sparse-checkout to copy just the Omnisense CSV from the sibling repository.

## Step 3: Data Processing

`build.py` orchestrates the data pipeline:

1. **Load CSV**: `modules/common.py:load_weather_csv()` scans the CSV for the weather station header row, extracts rows for sensor `30B40014`, parses timestamps as EAT, and converts numeric columns.

2. **Data quality**: Peak wind speeds above 150 km/h are replaced with NaN (sensor glitches). Cumulative precipitation resets are detected and corrected.

3. **Module processing**: Each module (`wind.py`, `solar.py`, `precipitation.py`, `cross_variable.py`) receives the cleaned DataFrame and returns chart configurations (Plotly-compatible JSON) and summary statistics.

4. **HTML generation**: All chart data is serialized to JSON and embedded into the HTML template via the `__DATA__` placeholder. The resulting `index.html` is self-contained, requiring only Plotly.js from CDN.

## Step 4: Publication

If the build produces changes, the GitHub Action commits and pushes, then triggers a `repository_dispatch` event to the main site repository, which syncs the embedded dashboard.

## Data Columns

| Column | Unit | Description |
|---|---|---|
| avg_wind_speed_kph | km/h | 5-minute average wind speed |
| peak_wind_kph | km/h | Peak gust in the 5-minute interval |
| wind_direction | degrees | Compass bearing (0 = North) |
| solar_radiation | W/m2 | Global horizontal irradiance |
| total_percipitation_mm | mm | Cumulative rainfall (with resets) |
| rate_percipitation_mm_h | mm/h | Instantaneous rainfall rate |
| battery_voltage | V | Sensor battery voltage |
