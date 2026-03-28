"""
Solar radiation processing and chart generation.
Time series, daily insolation, diurnal pattern, distribution, clearness index, peak solar hours.
"""

import math

import pandas as pd
import numpy as np

from .common import (
    SOLAR_COLORS, LATITUDE, LONGITUDE, TIMEZONE, to_eat_ms,
    extraterrestrial_radiation, get_season_boundaries,
)


def process(df):
    """Process solar radiation data and return chart configs, stats, and sidebar HTML."""
    sdf = df.copy()

    charts = []

    # ── Summary Statistics ────────────────────────────────────────────────
    daytime = sdf[sdf["solar_wm2"] > 0]
    mean_daytime = round(daytime["solar_wm2"].mean(), 1) if len(daytime) else 0
    max_radiation = round(sdf["solar_wm2"].max(), 1)
    high_pct = round((daytime["solar_wm2"] > 500).sum() / len(daytime) * 100, 1) if len(daytime) else 0

    # Daily insolation
    sdf["date"] = sdf["timestamp"].dt.date
    daily_insolation = _compute_daily_insolation(sdf)
    mean_insolation = round(daily_insolation["insolation_kwh"].mean(), 2) if len(daily_insolation) else 0
    max_insolation = round(daily_insolation["insolation_kwh"].max(), 2) if len(daily_insolation) else 0
    min_insolation = round(daily_insolation["insolation_kwh"].min(), 2) if len(daily_insolation) else 0

    # Clearness index
    daily_kt = _compute_clearness_index(daily_insolation)
    mean_kt = round(daily_kt["kt"].mean(), 2) if len(daily_kt) else 0

    # Daytime hours detection
    daytime_hours = _compute_daytime_hours(sdf)
    mean_daytime_h = round(np.mean(daytime_hours) if daytime_hours else 0, 1)

    # Peak solar hours
    mean_psh = round(mean_insolation, 1)  # PSH = insolation / 1 kW/m2

    stats = {
        "meanDaytimeIrradiance": mean_daytime,
        "maxRadiation": max_radiation,
        "highRadiationPct": high_pct,
        "meanDailyInsolation": mean_insolation,
        "maxDailyInsolation": max_insolation,
        "minDailyInsolation": min_insolation,
        "meanClearnessIndex": mean_kt,
        "meanDaytimeHours": mean_daytime_h,
        "meanPeakSolarHours": mean_psh,
    }

    # ── 1. Solar Radiation Time Series ────────────────────────────────────
    charts.append(_build_solar_timeseries(sdf))

    # ── 2. Daily Insolation Profile ───────────────────────────────────────
    charts.append(_build_daily_insolation(daily_insolation))

    # ── 3. Diurnal Solar Pattern ──────────────────────────────────────────
    charts.append(_build_diurnal_solar(sdf))

    # ── 4. Solar Distribution Histogram ───────────────────────────────────
    charts.append(_build_solar_distribution(sdf))

    # ── 5. Clearness Index Time Series ────────────────────────────────────
    charts.append(_build_clearness_index(daily_kt))

    # ── 6. Peak Solar Hours ───────────────────────────────────────────────
    charts.append(_build_peak_solar_hours(daily_insolation))

    return {"charts": charts, "stats": stats}


def _compute_daily_insolation(sdf):
    """Compute daily solar insolation (kWh/m2/day) by integrating 5-min readings."""
    daily = []
    for date, group in sdf.groupby("date"):
        group = group.sort_values("timestamp")
        if len(group) < 2:
            continue
        # Trapezoidal integration of W/m2 over time
        times = group["timestamp"].values
        values = group["solar_wm2"].values

        total_wh = 0
        for i in range(1, len(times)):
            dt_hours = (times[i] - times[i - 1]) / np.timedelta64(1, "h")
            avg_w = (values[i] + values[i - 1]) / 2
            total_wh += avg_w * dt_hours

        kwh = total_wh / 1000
        doy = pd.Timestamp(date).timetuple().tm_yday
        daily.append({
            "date": date,
            "date_ms": int(pd.Timestamp(date).tz_localize(TIMEZONE).timestamp() * 1000),
            "insolation_kwh": round(kwh, 3),
            "day_of_year": doy,
        })

    return pd.DataFrame(daily)


def _compute_clearness_index(daily_df):
    """Compute daily clearness index Kt = measured / extraterrestrial."""
    if daily_df.empty:
        return pd.DataFrame()

    kt_data = []
    for _, row in daily_df.iterrows():
        h0 = extraterrestrial_radiation(row["day_of_year"], LATITUDE)
        if h0 > 0:
            kt = min(row["insolation_kwh"] / h0, 1.0)
        else:
            kt = 0
        kt_data.append({
            "date": row["date"],
            "date_ms": row["date_ms"],
            "kt": round(kt, 3),
            "measured": row["insolation_kwh"],
            "extraterrestrial": round(h0, 3),
        })

    return pd.DataFrame(kt_data)


def _compute_daytime_hours(sdf):
    """Detect daytime hours from first/last non-zero radiation reading per day."""
    hours = []
    for date, group in sdf.groupby("date"):
        daytime = group[group["solar_wm2"] > 0]
        if len(daytime) < 2:
            continue
        first = daytime["timestamp"].min()
        last = daytime["timestamp"].max()
        h = (last - first).total_seconds() / 3600
        hours.append(h)
    return hours


def _build_solar_timeseries(sdf):
    """Build solar radiation time series area chart."""
    timestamps = [to_eat_ms(t) for t in sdf["timestamp"]]
    values = [round(v, 1) if not pd.isna(v) else None for v in sdf["solar_wm2"]]
    season_bounds = get_season_boundaries(sdf)

    traces = [{
        "type": "scatter",
        "mode": "lines",
        "name": "Solar Radiation",
        "x_ms": timestamps,
        "y": values,
        "fill": "tozeroy",
        "fillcolor": "rgba(255,200,0,0.3)",
        "line": {"color": "#ff8c00", "width": 1},
    }]

    layout = {
        "yaxis": {"title": "Solar Radiation (W/m\u00b2)"},
        "xaxis": {"title": "Date (EAT)"},
    }

    return {
        "id": "solar-timeseries",
        "title": "Solar Radiation Time Series",
        "title_sw": "Mfuatano wa Mionzi ya Jua",
        "data": traces,
        "layout": layout,
        "seasonBoundaries": season_bounds,
    }


def _build_daily_insolation(daily_df):
    """Build daily insolation bar chart."""
    if daily_df.empty:
        return {"id": "daily-insolation", "title": "Daily Insolation",
                "title_sw": "Jua la Kila Siku", "data": [], "layout": {}}

    dates_ms = daily_df["date_ms"].tolist()
    values = daily_df["insolation_kwh"].tolist()

    # Color by intensity
    colors = []
    for v in values:
        if v < 3:
            colors.append("#4575b4")
        elif v < 4.5:
            colors.append("#fee090")
        elif v < 5.5:
            colors.append("#fc8d59")
        else:
            colors.append("#d73027")

    traces = [{
        "type": "bar",
        "name": "Daily Insolation",
        "x_ms": dates_ms,
        "y": values,
        "marker": {"color": colors},
    }]

    layout = {
        "yaxis": {"title": "Insolation (kWh/m\u00b2/day)"},
        "xaxis": {"title": "Date (EAT)"},
        "shapes": [{
            "type": "line",
            "x0": 0, "x1": 1, "xref": "paper",
            "y0": 5.5, "y1": 5.5,
            "line": {"color": "red", "width": 1, "dash": "dash"},
        }],
        "annotations": [{
            "x": 1, "xref": "paper", "y": 5.5,
            "text": "Clear-sky reference (5.5 kWh/m\u00b2/day)",
            "showarrow": False,
            "xanchor": "right",
            "font": {"size": 10, "color": "red"},
        }],
    }

    return {
        "id": "daily-insolation",
        "title": "Daily Insolation",
        "title_sw": "Jua la Kila Siku",
        "data": traces,
        "layout": layout,
    }


def _build_diurnal_solar(sdf):
    """Build diurnal solar pattern with SD band and optional clear-sky reference."""
    sdf_c = sdf.copy()
    sdf_c["hour"] = sdf_c["timestamp"].dt.hour

    hourly = sdf_c.groupby("hour")["solar_wm2"].agg(["mean", "std", "count"])
    hourly["std"] = hourly["std"].fillna(0)

    # Monthly breakdown
    sdf_c["month"] = sdf_c["timestamp"].dt.month
    monthly_diurnal = {}
    for month, group in sdf_c.groupby("month"):
        h = group.groupby("hour")["solar_wm2"].mean()
        monthly_diurnal[int(month)] = {
            "hours": h.index.tolist(),
            "means": [round(v, 1) for v in h.values],
        }

    hours = list(range(24))
    means = [round(hourly.loc[h, "mean"], 1) if h in hourly.index else 0 for h in hours]
    sds = [round(hourly.loc[h, "std"], 1) if h in hourly.index else 0 for h in hours]
    upper = [round(m + s, 1) for m, s in zip(means, sds)]
    lower = [max(0, round(m - s, 1)) for m, s in zip(means, sds)]

    traces = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Mean Solar Radiation",
            "x": hours,
            "y": means,
            "line": {"color": "#ff8c00", "width": 2},
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "+1 SD",
            "x": hours,
            "y": upper,
            "line": {"width": 0},
            "showlegend": False,
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "-1 SD",
            "x": hours,
            "y": lower,
            "fill": "tonexty",
            "fillcolor": "rgba(255,140,0,0.15)",
            "line": {"width": 0},
            "showlegend": False,
        },
    ]

    layout = {
        "xaxis": {"title": "Hour of Day (EAT)", "dtick": 1},
        "yaxis": {"title": "Solar Radiation (W/m\u00b2)"},
        "showlegend": True,
    }

    return {
        "id": "diurnal-solar",
        "title": "Diurnal Solar Pattern",
        "title_sw": "Mtindo wa Jua wa Kila Siku",
        "data": traces,
        "layout": layout,
        "monthlyDiurnal": monthly_diurnal,
    }


def _build_solar_distribution(sdf):
    """Build solar radiation histogram (non-zero values only, daytime)."""
    daytime = sdf[sdf["solar_wm2"] > 0]["solar_wm2"].dropna().values

    if len(daytime) == 0:
        return {"id": "solar-distribution", "title": "Solar Distribution",
                "title_sw": "Usambazaji wa Jua", "data": [], "layout": {}}

    bins = np.arange(0, 1050, 50)
    hist, bin_edges = np.histogram(daytime, bins=bins)
    bin_centers = [(bin_edges[i] + bin_edges[i + 1]) / 2 for i in range(len(hist))]

    # Color by intensity
    colors = []
    for bc in bin_centers:
        if bc < 200:
            colors.append(SOLAR_COLORS["low"])
        elif bc < 500:
            colors.append(SOLAR_COLORS["moderate"])
        elif bc < 800:
            colors.append(SOLAR_COLORS["high"])
        else:
            colors.append(SOLAR_COLORS["very_high"])

    traces = [{
        "type": "bar",
        "name": "Frequency",
        "x": [round(b, 0) for b in bin_centers],
        "y": hist.tolist(),
        "marker": {"color": colors},
    }]

    # Modal bin
    modal_idx = int(np.argmax(hist))
    modal_bin = f"{int(bin_edges[modal_idx])}-{int(bin_edges[modal_idx + 1])} W/m\u00b2"

    layout = {
        "xaxis": {"title": "Solar Radiation (W/m\u00b2)"},
        "yaxis": {"title": "Count (daytime readings)"},
        "bargap": 0.05,
    }

    return {
        "id": "solar-distribution",
        "title": "Solar Distribution",
        "title_sw": "Usambazaji wa Jua",
        "data": traces,
        "layout": layout,
        "modalBin": modal_bin,
    }


def _build_clearness_index(daily_kt):
    """Build clearness index scatter plot.

    Thresholds are calibrated for a humid tropical coastal site (Mkuranga, ~7S).
    Standard temperate-climate thresholds (clear > 0.65, overcast < 0.35) are
    inappropriate here: even on a genuinely clear day, high precipitable water
    vapour and marine aerosols from the Indian Ocean suppress the clear-sky Kt
    ceiling to approximately 0.55-0.65. Using temperate thresholds causes clear
    days to be systematically mis-classified as partly cloudy.

    References:
    - Saunier, Reddy & Kumar (1987), Solar Energy 38(3): 169-177: generalised
      Liu-Jordan CDCs are not suitable for tropical locations; Kmax must be
      derived from local data.
    - Udo (2000), Solar Energy 69(1): 45-53: tropical Nigerian site (~7N, same
      latitude band) confirms Liu-Jordan inapplicability; highest recorded
      clear-sky Kt was 0.64 after rainfall cleared aerosols.
    - Diabate, Blanc & Wald (2004), Solar Energy 76(6): 733-744: Tanzania's
      coastal zone falls in a humid low-Kt climate class with monthly mean
      Kt typically 0.45-0.55 during dry-season months.
    """
    if daily_kt.empty:
        return {"id": "clearness-index", "title": "Clearness Index",
                "title_sw": "Fahirisi ya Uwazi", "data": [], "layout": {}}

    dates_ms = daily_kt["date_ms"].tolist()
    kt_vals = daily_kt["kt"].tolist()

    # Thresholds adjusted for humid tropical coastal climate (see docstring).
    # Clear: Kt > 0.55, Partly cloudy: 0.25-0.55, Overcast: Kt <= 0.25
    KT_CLEAR = 0.55
    KT_OVERCAST = 0.25

    traces = [{
        "type": "scatter",
        "mode": "markers",
        "name": "Clearness Index (Kt)",
        "x_ms": dates_ms,
        "y": kt_vals,
        "marker": {"color": "#000000", "size": 8},
    }]

    layout = {
        "yaxis": {"title": "Clearness Index (Kt)", "range": [0, 1]},
        "xaxis": {"title": "Date (EAT)"},
        "shapes": [
            {"type": "rect", "x0": 0, "x1": 1, "xref": "paper",
             "y0": KT_CLEAR, "y1": 1, "fillcolor": "rgba(44,160,44,0.1)",
             "line": {"width": 0}},
            {"type": "rect", "x0": 0, "x1": 1, "xref": "paper",
             "y0": KT_OVERCAST, "y1": KT_CLEAR, "fillcolor": "rgba(255,191,0,0.1)",
             "line": {"width": 0}},
            {"type": "rect", "x0": 0, "x1": 1, "xref": "paper",
             "y0": 0, "y1": KT_OVERCAST, "fillcolor": "rgba(69,117,180,0.1)",
             "line": {"width": 0}},
        ],
        "annotations": [
            {"x": 1.02, "xref": "paper", "y": 0.77, "text": "Clear",
             "showarrow": False, "font": {"size": 10, "color": "#2ca02c"}},
            {"x": 1.02, "xref": "paper", "y": 0.4, "text": "Partly Cloudy",
             "showarrow": False, "font": {"size": 10, "color": "#b8860b"}},
            {"x": 1.02, "xref": "paper", "y": 0.12, "text": "Overcast",
             "showarrow": False, "font": {"size": 10, "color": "#4575b4"}},
        ],
    }

    # Sky condition distribution
    clear_pct = round((daily_kt["kt"] > KT_CLEAR).sum() / len(daily_kt) * 100, 1)
    partly_pct = round(((daily_kt["kt"] > KT_OVERCAST) & (daily_kt["kt"] <= KT_CLEAR)).sum() / len(daily_kt) * 100, 1)
    overcast_pct = round((daily_kt["kt"] <= KT_OVERCAST).sum() / len(daily_kt) * 100, 1)

    return {
        "id": "clearness-index",
        "title": "Clearness Index",
        "title_sw": "Fahirisi ya Uwazi",
        "data": traces,
        "layout": layout,
        "clearPct": clear_pct,
        "partlyCloudyPct": partly_pct,
        "overcastPct": overcast_pct,
    }


def _build_peak_solar_hours(daily_df):
    """Build peak solar hours bar chart."""
    if daily_df.empty:
        return {"id": "peak-solar-hours", "title": "Peak Solar Hours",
                "title_sw": "Masaa ya Jua Kali", "data": [], "layout": {}}

    dates_ms = daily_df["date_ms"].tolist()
    psh = daily_df["insolation_kwh"].tolist()  # PSH = kWh/m2/day / 1 kW/m2

    traces = [{
        "type": "bar",
        "name": "Peak Solar Hours",
        "x_ms": dates_ms,
        "y": psh,
        "marker": {"color": "#ff8c00"},
    }]

    mean_psh = round(np.mean(psh), 1) if psh else 0

    layout = {
        "yaxis": {"title": "Peak Solar Hours"},
        "xaxis": {"title": "Date (EAT)"},
    }

    return {
        "id": "peak-solar-hours",
        "title": "Peak Solar Hours",
        "title_sw": "Masaa ya Jua Kali",
        "data": traces,
        "layout": layout,
        "meanPSH": mean_psh,
    }
