#!/usr/bin/env python3
"""
Build script for the Omnisense weather station dashboard (wind, pressure, solar).

To update with new data:
  1. Move old omnisense_*.csv into data/legacy/
  2. Put new omnisense_DDMMYY.csv into data/
  3. Run: python build.py
  4. git add index.html && git commit -m "update data" && git push

Output: index.html

NOTE FOR CLAUDE: After making any changes to this file or index.html,
add an entry to the Changelog in CLAUDE.md. The heading must include
date and time to the second in CST (Taiwan, UTC+8) — always run `date`
first to get the real time: ### YYYY-MM-DD HH:MM:SS CST
"""

import json
from pathlib import Path

import pandas as pd
import pytz

# ── Configuration ──────────────────────────────────────────────────────────────
TIMEZONE = pytz.timezone("Africa/Dar_es_Salaam")
DATA_FOLDER = Path("data")
OUTPUT_FILE = Path("index.html")

WEATHER_STATION_ID = "30B40014"

# ── Data loading ───────────────────────────────────────────────────────────────
def find_omnisense_file():
    files = sorted(
        p for p in DATA_FOLDER.glob("omnisense_*.csv")
        if not p.name.startswith("~$")
    )
    if not files:
        raise ValueError(f"No omnisense_*.csv found in {DATA_FOLDER} (files in legacy/ are ignored)")
    if len(files) > 1:
        print(f"  Warning: multiple omnisense files found, using {files[-1].name}")
    return files[-1]


def load_weather_station(path):
    """Parse the weather station block (sensor 30B40014) from an Omnisense CSV."""
    with open(path) as f:
        lines = f.readlines()

    expected_cols = [
        "avg_wind_speed_kph", "peak_wind_kph", "wind_direction",
        "solar_radiation", "total_percipitation_mm", "rate_percipitation_mm_h",
    ]

    i = 0
    while i < len(lines):
        if "sensor_desc,site_name" not in lines[i]:
            i += 1
            continue

        col_headers = lines[i + 2].strip().split(",")

        # Identify the weather station block by its unique columns
        if not any(c in col_headers for c in expected_cols):
            i += 1
            continue

        # Find column indices
        sensor_id_idx = col_headers.index("sensorId") if "sensorId" in col_headers else 0
        date_idx = col_headers.index("read_date") if "read_date" in col_headers else 2
        col_idx = {c: col_headers.index(c) for c in expected_cols if c in col_headers}

        # Find data rows
        data_start = i + 3
        data_end = data_start
        for j in range(data_start, len(lines)):
            if "sensor_desc,site_name" in lines[j]:
                data_end = j
                break
            data_end = j + 1

        rows = []
        for row_line in lines[data_start:data_end]:
            row = row_line.strip().split(",")
            if not row or row[0].strip() != WEATHER_STATION_ID:
                continue
            try:
                entry = {"datetime": row[date_idx]}
                for col, idx in col_idx.items():
                    entry[col] = float(row[idx]) if idx < len(row) else None
                rows.append(entry)
            except (IndexError, ValueError):
                continue

        if not rows:
            i = data_end
            continue

        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        for col in col_idx:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime")
        return df

        i = data_end

    raise ValueError(f"Weather station sensor {WEATHER_STATION_ID} not found in {path.name}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    path = find_omnisense_file()
    print(f"Loading {path.name}...")
    df = load_weather_station(path)

    df["datetime"] = (
        pd.to_datetime(df["datetime"], errors="coerce")
        .dt.tz_localize(TIMEZONE, nonexistent="shift_forward", ambiguous="NaT")
    )
    df = df.dropna(subset=["datetime"]).set_index("datetime").sort_index()

    print(f"  {len(df):,} records")
    print(f"  {df.index.min().date()} → {df.index.max().date()}")
    print(f"  Columns: {list(df.columns)}")
    print()
    print("  Dashboard spec pending — index.html not yet generated.")
    print("  Data loading verified OK.")


if __name__ == "__main__":
    main()
