"""
Shared utilities for the ARC Tanzania Weather Station dashboard.
CSV parsing, time helpers, colour palettes, data quality filters.
"""

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

TIMEZONE = pytz.timezone("Africa/Dar_es_Salaam")
SENSOR_ID = "30B40014"
LATITUDE = -7.065   # ARC ecovillage near Mkuranga
LONGITUDE = 39.18
SOLAR_CONSTANT = 1361  # W/m2

# ── Beaufort Scale (WMO definition, classified in knots) ──────────────────────
# The Beaufort scale is defined in knots. All other units are derived using the
# exact conversion factors below to avoid boundary errors.
#
# Exact conversion factors (from 1 nautical mile = 1852 m exactly):
#   1 kn = 463/250 km/h   (= 1.852 km/h, exact rational value)
#   1 kn = 463/900 m/s    (= 0.51444... m/s, exact rational value)
KN_TO_KPH = 463 / 250   # km/h per knot
KN_TO_MS  = 463 / 900   # m/s per knot
KPH_TO_KN = 250 / 463   # knots per km/h
MS_TO_KN  = 900 / 463   # knots per m/s

# Thresholds in knots (WMO integer boundaries). range_kn is [lo, hi).
BEAUFORT_SCALE = {
    0:  {"range_kn": (0,   1),   "label": "Calm"},
    1:  {"range_kn": (1,   4),   "label": "Light air"},
    2:  {"range_kn": (4,   7),   "label": "Light breeze"},
    3:  {"range_kn": (7,  11),   "label": "Gentle breeze"},
    4:  {"range_kn": (11, 17),   "label": "Moderate breeze"},
    5:  {"range_kn": (17, 22),   "label": "Fresh breeze"},
    6:  {"range_kn": (22, 28),   "label": "Strong breeze"},
    7:  {"range_kn": (28, 34),   "label": "Near gale"},
    8:  {"range_kn": (34, 41),   "label": "Gale"},
    9:  {"range_kn": (41, 48),   "label": "Strong gale"},
    10: {"range_kn": (48, 56),   "label": "Storm"},
    11: {"range_kn": (56, 64),   "label": "Violent storm"},
    12: {"range_kn": (64, 999),  "label": "Hurricane force"},
}

# ── Wind Classification Systems ─────────────────────────────────────────────
# All systems defined in their original published units.
# Use the conversion constants above to convert to the working unit at runtime.
#
# native_unit: 'kn' = knots, 'ms' = m/s, 'computed' = derived from dataset.
# bands: list of {label, lo, hi} where hi=None means no upper limit.
WIND_CLASSIFICATIONS = {
    "beaufort": {
        "label": "Beaufort",
        "native_unit": "kn",
        "source": "WMO/Beaufort 1805",
        "notes": "Sea-level scale; Bf 9-12 merged into Severe+ for pedestrian relevance. Force numbers available as metadata.",
        "bands": [
            {"label": "Calm",            "lo": 0,  "hi": 1,    "force": 0},
            {"label": "Light Air",       "lo": 1,  "hi": 4,    "force": 1},
            {"label": "Light Breeze",    "lo": 4,  "hi": 7,    "force": 2},
            {"label": "Gentle Breeze",   "lo": 7,  "hi": 11,   "force": 3},
            {"label": "Moderate Breeze", "lo": 11, "hi": 17,   "force": 4},
            {"label": "Fresh Breeze",    "lo": 17, "hi": 22,   "force": 5},
            {"label": "Strong Breeze",   "lo": 22, "hi": 28,   "force": 6},
            {"label": "Near Gale",       "lo": 28, "hi": 34,   "force": 7},
            {"label": "Gale",            "lo": 34, "hi": 41,   "force": 8},
            {"label": "Severe+",         "lo": 41, "hi": None, "force": "9+"},
        ],
    },
    "lawson": {
        "label": "Lawson 2001",
        "native_unit": "ms",
        "source": "Lawson T.V., 2001, pedestrian comfort criteria, assessed at 1.5m height",
        "notes": "Upper-bound thresholds. Use this version, not LDDC.",
        "bands": [
            {"label": "Sitting",          "lo": 0,  "hi": 4},
            {"label": "Standing",         "lo": 4,  "hi": 6},
            {"label": "Strolling",        "lo": 6,  "hi": 8},
            {"label": "Business Walking", "lo": 8,  "hi": 10},
            {"label": "Uncomfortable",    "lo": 10, "hi": None},
        ],
    },
    "davenport": {
        "label": "Davenport",
        "native_unit": "ms",
        "source": "Davenport A.G., 1975, first published pedestrian wind comfort criterion, exceedance probability 1.5%",
        "notes": "Upper-bound thresholds.",
        "bands": [
            {"label": "Long Sitting",    "lo": 0,   "hi": 3.6},
            {"label": "Short Sitting",   "lo": 3.6, "hi": 5.3},
            {"label": "Walking Quietly", "lo": 5.3, "hi": 7.6},
            {"label": "Walking Fast",    "lo": 7.6, "hi": 9.8},
            {"label": "Uncomfortable",   "lo": 9.8, "hi": None},
        ],
    },
    "percentile": {
        "label": "Percentiles",
        "native_unit": "computed",
        "source": "WMO climatological quintile practice",
        "notes": "Boundaries computed at runtime from the full available dataset. Quintile-based: P20, P50, P80, P95.",
        "bands": [
            {"label": "Calm",        "percentile_hi": 20},
            {"label": "Light",       "percentile_lo": 20, "percentile_hi": 50},
            {"label": "Moderate",    "percentile_lo": 50, "percentile_hi": 80},
            {"label": "Strong",      "percentile_lo": 80, "percentile_hi": 95},
            {"label": "Exceptional", "percentile_lo": 95},
        ],
    },
}

# ── 16-point Compass ─────────────────────────────────────────────────────────
COMPASS_DIRS_16 = [
    ("N", 0, 11.25), ("NNE", 11.25, 33.75), ("NE", 33.75, 56.25),
    ("ENE", 56.25, 78.75), ("E", 78.75, 101.25), ("ESE", 101.25, 123.75),
    ("SE", 123.75, 146.25), ("SSE", 146.25, 168.75), ("S", 168.75, 191.25),
    ("SSW", 191.25, 213.75), ("SW", 213.75, 236.25), ("WSW", 236.25, 258.75),
    ("W", 258.75, 281.25), ("WNW", 281.25, 303.75), ("NW", 303.75, 326.25),
    ("NNW", 326.25, 348.75),
]

COMPASS_DIRS_8 = [
    ("N", 0, 22.5), ("NE", 22.5, 67.5), ("E", 67.5, 112.5),
    ("SE", 112.5, 157.5), ("S", 157.5, 202.5), ("SW", 202.5, 247.5),
    ("W", 247.5, 292.5), ("NW", 292.5, 337.5),
]

# ── Colour Palettes ──────────────────────────────────────────────────────────
WIND_SPEED_COLORS = [
    "#4575b4",  # 0-5 km/h
    "#91bfdb",  # 5-10 km/h
    "#fee090",  # 10-15 km/h
    "#fc8d59",  # 15-20 km/h
    "#d73027",  # 20+ km/h
]

WIND_SPEED_BINS = [0, 5, 10, 15, 20, 999]
WIND_SPEED_LABELS = ["0-5", "5-10", "10-15", "15-20", "20+"]

SOLAR_COLORS = {
    "low": "#4575b4",       # < 200 W/m2
    "moderate": "#fee090",  # 200-500 W/m2
    "high": "#fc8d59",      # 500-800 W/m2
    "very_high": "#d73027", # > 800 W/m2
}

RAIN_INTENSITY_COLORS = {
    "light": "#a6d96a",     # < 2.5 mm/h
    "moderate": "#fee08b",  # 2.5-7.5 mm/h
    "heavy": "#fdae61",     # 7.5-25 mm/h
    "very_heavy": "#d73027", # > 25 mm/h
}

RAIN_DAILY_COLORS = {
    "light": "#a6d96a",     # < 2.5 mm
    "moderate": "#fee08b",  # 2.5-7.5 mm
    "heavy": "#fdae61",     # 7.5-25 mm
    "very_heavy": "#d73027", # > 25 mm
}

VENTILATION_COLORS = {
    "effective": "#2ca02c",
    "marginal": "#ffbf00",
    "closed": "#d62728",
}

# Tanzanian seasons
SEASONS = [
    {"name": "Kiangazi", "name_sw": "Kiangazi", "months": [1, 2]},
    {"name": "Masika", "name_sw": "Masika", "months": [3, 4, 5]},
    {"name": "Kiangazi", "name_sw": "Kiangazi", "months": [6, 7, 8, 9, 10]},
    {"name": "Vuli", "name_sw": "Vuli", "months": [11, 12]},
]


def load_weather_csv(csv_path):
    """Parse the Omnisense CSV and extract weather station sensor 30B40014.

    Returns a DataFrame with columns:
        timestamp, avg_wind_kph, peak_wind_kph, wind_dir, solar_wm2,
        precip_total_mm, precip_rate_mmh, battery_v
    """
    with open(csv_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the weather station header row
    header_idx = None
    for i, line in enumerate(lines):
        if "avg_wind_speed_kph" in line:
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(f"Could not find weather station section in {csv_path}")

    # Find the end of the weather section
    end_idx = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].strip().startswith("sensor_desc,site_name"):
            end_idx = i
            break

    # Parse the section
    header = lines[header_idx].strip().split(",")
    rows = []
    for i in range(header_idx + 1, end_idx):
        line = lines[i].strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < len(header):
            continue
        if parts[0] != SENSOR_ID:
            continue
        rows.append(parts)

    if not rows:
        raise ValueError(f"No rows found for sensor {SENSOR_ID}")

    df = pd.DataFrame(rows, columns=header)

    # Rename and type columns
    df = df.rename(columns={
        "read_date": "timestamp",
        "avg_wind_speed_kph": "avg_wind_kph",
        "peak_wind_kph": "peak_wind_kph",
        "wind_direction": "wind_dir",
        "solar_radiation": "solar_wm2",
        "total_percipitation_mm": "precip_total_mm",
        "rate_percipitation_mm_h": "precip_rate_mmh",
        "battery_voltage": "battery_v",
    })

    df = df[["timestamp", "avg_wind_kph", "peak_wind_kph", "wind_dir",
             "solar_wm2", "precip_total_mm", "precip_rate_mmh", "battery_v"]]

    # Convert types
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["timestamp"] = df["timestamp"].dt.tz_localize(TIMEZONE)
    for col in ["avg_wind_kph", "peak_wind_kph", "solar_wm2",
                 "precip_total_mm", "precip_rate_mmh", "battery_v"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["wind_dir"] = pd.to_numeric(df["wind_dir"], errors="coerce").astype("Int64")

    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def find_latest_csv(data_dir="data/omnisense"):
    """Find the most recent omnisense CSV file."""
    csv_files = sorted(Path(data_dir).glob("omnisense_*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No omnisense_*.csv files found in {data_dir}")
    return str(csv_files[-1])


def to_eat_ms(dt):
    """Convert a timezone-aware datetime to EAT epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def filter_date_range(df, start=None, end=None):
    """Slice DataFrame by date range."""
    mask = pd.Series(True, index=df.index)
    if start is not None:
        if not hasattr(start, "tzinfo") or start.tzinfo is None:
            start = TIMEZONE.localize(start)
        mask &= df["timestamp"] >= start
    if end is not None:
        if not hasattr(end, "tzinfo") or end.tzinfo is None:
            end = TIMEZONE.localize(end)
        mask &= df["timestamp"] <= end
    return df[mask].copy()


def detect_precip_resets(series):
    """Detect resets in cumulative precipitation and return corrected cumulative totals.

    The total_percipitation_mm column is a running total that occasionally resets.
    We detect negative jumps and reconstruct a true cumulative total.
    """
    diffs = series.diff().fillna(0)
    # Negative differences indicate resets; set those increments to 0
    diffs = diffs.clip(lower=0)
    return diffs.cumsum()


def spike_filter(series, max_val):
    """Replace values above threshold with NaN."""
    return series.where(series <= max_val)


def compass_bin(degrees, n_points=16):
    """Assign a compass direction label to a bearing in degrees."""
    dirs = COMPASS_DIRS_16 if n_points == 16 else COMPASS_DIRS_8
    if pd.isna(degrees):
        return None
    d = degrees % 360
    for label, lo, hi in dirs:
        if lo <= d < hi:
            return label
    # Handle wrap-around for N (348.75 to 360 maps to N)
    return dirs[0][0]


def beaufort_number(speed_kph):
    """Return the Beaufort number for a given wind speed in km/h.

    Classification is performed in knots (WMO definition) to avoid
    boundary errors from rounded secondary-unit thresholds.
    """
    speed_kn = speed_kph * KPH_TO_KN
    for num, info in BEAUFORT_SCALE.items():
        lo, hi = info["range_kn"]
        if lo <= speed_kn < hi:
            return num
    return 12


def solar_declination(day_of_year):
    """Solar declination angle in radians (Spencer, 1971)."""
    B = (2 * math.pi / 365) * (day_of_year - 1)
    return (0.006918 - 0.399912 * math.cos(B) + 0.070257 * math.sin(B)
            - 0.006758 * math.cos(2 * B) + 0.000907 * math.sin(2 * B)
            - 0.002697 * math.cos(3 * B) + 0.00148 * math.sin(3 * B))


def extraterrestrial_radiation(day_of_year, latitude_deg):
    """Daily extraterrestrial radiation on a horizontal surface (MJ/m2/day).

    Uses the standard method from Duffie & Beckman.
    Returns value in kWh/m2/day for convenience.
    """
    lat = math.radians(latitude_deg)
    decl = solar_declination(day_of_year)

    # Earth-Sun distance correction factor
    B = (2 * math.pi / 365) * (day_of_year - 1)
    E0 = 1.00011 + 0.034221 * math.cos(B) + 0.00128 * math.sin(B) + \
         0.000719 * math.cos(2 * B) + 0.000077 * math.sin(2 * B)

    # Sunset hour angle
    cos_ws = -math.tan(lat) * math.tan(decl)
    if cos_ws < -1:
        ws = math.pi  # 24-hour daylight
    elif cos_ws > 1:
        ws = 0        # 24-hour darkness
    else:
        ws = math.acos(cos_ws)

    # Daily extraterrestrial radiation (MJ/m2/day)
    H0_mj = (24 * 3600 * SOLAR_CONSTANT * E0 / math.pi) * (
        ws * math.sin(lat) * math.sin(decl) +
        math.cos(lat) * math.cos(decl) * math.sin(ws)
    ) / 1e6

    # Convert MJ/m2/day to kWh/m2/day
    return H0_mj / 3.6


def get_season(month):
    """Return season name for a given month number."""
    for s in SEASONS:
        if month in s["months"]:
            return s["name"]
    return "Unknown"


def get_season_boundaries(df):
    """Get season boundary timestamps for marking on charts."""
    boundaries = []
    if df.empty:
        return boundaries
    min_date = df["timestamp"].min()
    max_date = df["timestamp"].max()

    # Season start months
    season_starts = [1, 3, 6, 11]
    year = min_date.year
    while year <= max_date.year + 1:
        for m in season_starts:
            dt = TIMEZONE.localize(datetime(year, m, 1))
            if min_date <= dt <= max_date:
                season_name = get_season(m)
                boundaries.append({"ts": to_eat_ms(dt), "label": season_name})
        year += 1
    return boundaries


def build_available_periods(df):
    """Build available time periods for the date range selector."""
    if df.empty:
        return {}

    min_ts = df["timestamp"].min()
    max_ts = df["timestamp"].max()
    dates = df["timestamp"].dt.date.unique()

    # Years
    years = sorted(df["timestamp"].dt.year.unique().tolist())

    # Months
    months = []
    seen_months = set()
    for d in sorted(dates):
        key = (d.year, d.month)
        if key not in seen_months:
            seen_months.add(key)
            from calendar import month_name
            label = f"{month_name[d.month]} {d.year}"
            months.append({"label": label, "year": d.year, "month": d.month})

    # Seasons
    seasons = []
    seen_seasons = set()
    for d in sorted(dates):
        s_name = get_season(d.month)
        s_idx = next(i for i, s in enumerate(SEASONS) if s["name"] == s_name)
        key = (d.year, s_idx)
        if key not in seen_seasons:
            seen_seasons.add(key)
            _MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
            m_first = _MONTH_ABBR[SEASONS[s_idx]['months'][0] - 1]
            m_last  = _MONTH_ABBR[SEASONS[s_idx]['months'][-1] - 1]
            seasons.append({
                "label": f"{s_name} ({m_first}\u2013{m_last}) {d.year}",
                "year": d.year, "season": s_idx
            })

    # Weeks (ISO weeks)
    weeks = []
    seen_weeks = set()
    for d in sorted(dates):
        iso_year, iso_week, _ = d.isocalendar()
        key = (iso_year, iso_week)
        if key not in seen_weeks:
            seen_weeks.add(key)
            weeks.append({
                "label": "W/s " + date.fromisocalendar(iso_year, iso_week, 1).strftime("%d/%m/%y"),
                "year": iso_year, "week": iso_week
            })

    # Days
    days = []
    for d in sorted(dates):
        dt = TIMEZONE.localize(datetime(d.year, d.month, d.day))
        days.append({
            "label": d.strftime("%d %b %Y"),
            "ts": to_eat_ms(dt)
        })

    return {
        "availableYears": years,
        "availableMonths": months,
        "availableSeasons": seasons,
        "availableWeeks": weeks,
        "availableDays": days,
        "dateRange": {
            "min": to_eat_ms(min_ts),
            "max": to_eat_ms(max_ts),
        },
    }


def weibull_fit(speeds):
    """Simple iterative Weibull MLE fit for non-zero wind speeds.

    Returns (k, c) shape and scale parameters.
    Falls back to method of moments if iteration doesn't converge.
    """
    import numpy as np
    x = np.array([s for s in speeds if s > 0], dtype=float)
    if len(x) < 10:
        return (None, None)

    # Method of moments initial estimate
    mean_x = np.mean(x)
    std_x = np.std(x)
    if std_x == 0:
        return (None, None)

    # Approximate k from coefficient of variation
    cv = std_x / mean_x
    k = (cv) ** (-1.086)  # empirical approximation
    k = max(0.5, min(k, 10))

    # Newton-Raphson iteration for MLE
    ln_x = np.log(x)
    n = len(x)
    for _ in range(50):
        x_k = x ** k
        sum_xk = np.sum(x_k)
        sum_xk_lnx = np.sum(x_k * ln_x)
        if sum_xk == 0:
            break
        f = (sum_xk_lnx / sum_xk) - (1 / k) - np.mean(ln_x)
        # Derivative
        sum_xk_lnx2 = np.sum(x_k * ln_x ** 2)
        df = (sum_xk_lnx2 * sum_xk - sum_xk_lnx ** 2) / (sum_xk ** 2) + (1 / k ** 2)
        if abs(df) < 1e-12:
            break
        k_new = k - f / df
        if k_new <= 0:
            k_new = k / 2
        if abs(k_new - k) < 1e-6:
            k = k_new
            break
        k = k_new

    k = max(0.1, min(k, 20))
    c = (np.mean(x ** k)) ** (1 / k)
    return (round(k, 3), round(c, 3))
