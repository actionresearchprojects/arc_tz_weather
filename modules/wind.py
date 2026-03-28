"""
Wind data processing and chart generation.
Wind rose, time series, diurnal, distribution, gust factor, calm periods, ventilation availability.
"""

import math
from collections import Counter

import pandas as pd
import numpy as np

from .common import (
    COMPASS_DIRS_16, COMPASS_DIRS_8, WIND_SPEED_BINS, WIND_SPEED_LABELS,
    WIND_SPEED_COLORS, BEAUFORT_SCALE, VENTILATION_COLORS, WIND_CLASSIFICATIONS,
    KN_TO_KPH, TIMEZONE,
    spike_filter, compass_bin, beaufort_number, weibull_fit, to_eat_ms,
    get_season_boundaries,
)


def process(df):
    """Process wind data and return chart configs, stats, and sidebar HTML.

    Args:
        df: Full weather station DataFrame (already parsed and cleaned).
    Returns:
        Dict with keys: "charts", "stats", "sidebar_html"
    """
    wdf = df.copy()

    # Apply spike filter to peak wind
    wdf["peak_wind_kph"] = spike_filter(wdf["peak_wind_kph"], 150)

    # Compass direction labels
    wdf["compass_16"] = wdf["wind_dir"].apply(lambda d: compass_bin(d, 16))
    wdf["compass_8"] = wdf["wind_dir"].apply(lambda d: compass_bin(d, 8))

    charts = []
    stats = {}

    # ── Summary Statistics ────────────────────────────────────────────────
    total_readings = len(wdf)
    calm_readings = (wdf["avg_wind_kph"] == 0).sum()
    calm_pct = round(calm_readings / total_readings * 100, 1) if total_readings else 0

    non_calm = wdf[wdf["avg_wind_kph"] > 0]
    mean_speed = round(wdf["avg_wind_kph"].mean(), 1)
    mean_speed_noncalm = round(non_calm["avg_wind_kph"].mean(), 1) if len(non_calm) else 0
    max_speed = round(wdf["avg_wind_kph"].max(), 1)
    max_gust = round(wdf["peak_wind_kph"].max(), 1) if wdf["peak_wind_kph"].notna().any() else 0
    median_speed = round(wdf["avg_wind_kph"].median(), 1)
    p95_speed = round(wdf["avg_wind_kph"].quantile(0.95), 1)

    # Prevailing direction (mode of non-calm readings)
    if len(non_calm) > 0:
        dir_counts = Counter(non_calm["compass_16"].dropna())
        prevailing = dir_counts.most_common(1)[0][0] if dir_counts else "N/A"
    else:
        prevailing = "N/A"

    # Weibull fit
    k_val, c_val = weibull_fit(non_calm["avg_wind_kph"].values)

    stats = {
        "totalReadings": total_readings,
        "calmReadings": calm_readings,
        "calmPct": calm_pct,
        "meanSpeed": mean_speed,
        "meanSpeedNonCalm": mean_speed_noncalm,
        "maxSpeed": max_speed,
        "maxGust": max_gust,
        "medianSpeed": median_speed,
        "p95Speed": p95_speed,
        "prevailingDir": prevailing,
    }

    # ── 1. Wind Rose ──────────────────────────────────────────────────────
    wind_rose = _build_wind_rose(wdf, 16)
    charts.append(wind_rose)

    # ── 2. Wind Speed Time Series ─────────────────────────────────────────
    charts.append(_build_wind_timeseries(wdf))

    # ── 3. Diurnal Wind Pattern ───────────────────────────────────────────
    charts.append(_build_diurnal_wind(wdf))

    # ── 4. Wind Speed Distribution ────────────────────────────────────────
    charts.append(_build_wind_distribution(wdf, k_val, c_val))

    # ── 5. Gust Factor Analysis ───────────────────────────────────────────
    charts.append(_build_gust_factor(wdf))

    # ── 6. Calm Period Analysis ───────────────────────────────────────────
    charts.append(_build_calm_periods(wdf))

    # ── 7. Ventilation Availability ───────────────────────────────────────
    charts.append(_build_ventilation_availability(wdf))

    # ── 8. Wind Speed Category Distribution ───────────────────────────────
    charts.append(_build_wind_category_distribution(wdf))

    return {"charts": charts, "stats": stats}


def _build_wind_rose(wdf, n_points):
    """Build wind rose polar bar chart data."""
    dirs = COMPASS_DIRS_16 if n_points == 16 else COMPASS_DIRS_8
    dir_labels = [d[0] for d in dirs]
    col = "compass_16" if n_points == 16 else "compass_8"

    non_calm = wdf[wdf["avg_wind_kph"] > 0].copy()
    total = len(wdf)
    calm_pct = round((wdf["avg_wind_kph"] == 0).sum() / total * 100, 1) if total else 0

    # Speed bins: 0-5, 5-10, 10-15, 15-20, 20+
    traces = []
    for i in range(len(WIND_SPEED_LABELS)):
        lo = WIND_SPEED_BINS[i]
        hi = WIND_SPEED_BINS[i + 1]
        label = WIND_SPEED_LABELS[i]
        mask = (non_calm["avg_wind_kph"] >= lo) & (non_calm["avg_wind_kph"] < hi)
        subset = non_calm[mask]

        freqs = []
        for dl in dir_labels:
            count = (subset[col] == dl).sum()
            freqs.append(round(count / total * 100, 2) if total else 0)

        # Map direction labels to degrees for Plotly
        dir_degrees = []
        for d in dirs:
            mid = (d[1] + d[2]) / 2
            if d[0] == "N" and d[1] == 0:
                mid = 0
            dir_degrees.append(mid)

        traces.append({
            "type": "barpolar",
            "r": freqs,
            "theta": dir_labels,
            "name": f"{label} km/h",
            "marker": {"color": WIND_SPEED_COLORS[i]},
        })

    layout = {
        "polar": {
            "angularaxis": {
                "direction": "clockwise",
                "rotation": 90,
                "tickmode": "array",
                "tickvals": list(range(0, 360, 360 // n_points)),
                "ticktext": dir_labels,
            },
            "radialaxis": {
                "ticksuffix": "%",
                "angle": 45,
            },
        },
        "barmode": "stack",
        "bargap": 0,
        "showlegend": True,
        "legend": {"x": 1.1, "y": 1},
    }

    return {
        "id": "wind-rose",
        "title": "Wind Rose",
        "title_sw": "Mwelekeo wa Upepo",
        "data": traces,
        "layout": layout,
        "calmPct": calm_pct,
    }


def _build_wind_timeseries(wdf):
    """Build wind speed time series with average and gust."""
    timestamps = [to_eat_ms(t) for t in wdf["timestamp"]]

    # 24-hour running mean
    wdf_sorted = wdf.set_index("timestamp").sort_index()
    running_mean = wdf_sorted["avg_wind_kph"].rolling("24h", min_periods=1).mean()
    rm_ts = [to_eat_ms(t) for t in running_mean.index]
    rm_vals = [round(v, 1) if not pd.isna(v) else None for v in running_mean.values]

    avg_vals = [round(v, 1) if not pd.isna(v) else None for v in wdf["avg_wind_kph"]]
    peak_vals = [round(v, 1) if not pd.isna(v) else None for v in wdf["peak_wind_kph"]]

    season_bounds = get_season_boundaries(wdf)

    traces = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Average",
            "x_ms": timestamps,
            "y": avg_vals,
            "line": {"color": "#1f77b4", "width": 1},
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Peak Gust",
            "x_ms": timestamps,
            "y": peak_vals,
            "line": {"color": "#ff7f0e", "width": 0.5, "dash": "dot"},
            "opacity": 0.6,
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "24h Mean",
            "x_ms": rm_ts,
            "y": rm_vals,
            "line": {"color": "#d62728", "width": 2},
        },
    ]

    layout = {
        "yaxis": {"title": "Wind Speed (km/h)"},
        "xaxis": {"title": "Date (EAT)"},
        "showlegend": True,
        "legend": {"x": 0, "y": 1.12, "orientation": "h"},
    }

    return {
        "id": "wind-timeseries",
        "title": "Wind Speed Time Series",
        "title_sw": "Mfuatano wa Kasi ya Upepo",
        "data": traces,
        "layout": layout,
        "seasonBoundaries": season_bounds,
    }


def _build_diurnal_wind(wdf):
    """Build diurnal wind pattern: mean speed by hour with SD band."""
    wdf_c = wdf.copy()
    wdf_c["hour"] = wdf_c["timestamp"].dt.hour

    hourly = wdf_c.groupby("hour")["avg_wind_kph"].agg(["mean", "std", "count"])
    hourly["std"] = hourly["std"].fillna(0)

    # Calm percentage by hour
    calm_by_hour = wdf_c.groupby("hour").apply(
        lambda g: (g["avg_wind_kph"] == 0).sum() / len(g) * 100
    ).round(1)

    # Optional: separate by month
    wdf_c["month"] = wdf_c["timestamp"].dt.month
    monthly_diurnal = {}
    for month, group in wdf_c.groupby("month"):
        h = group.groupby("hour")["avg_wind_kph"].mean()
        monthly_diurnal[int(month)] = {
            "hours": h.index.tolist(),
            "means": [round(v, 2) for v in h.values],
        }

    hours = list(range(24))
    means = [round(hourly.loc[h, "mean"], 2) if h in hourly.index else 0 for h in hours]
    sds = [round(hourly.loc[h, "std"], 2) if h in hourly.index else 0 for h in hours]
    upper = [round(m + s, 2) for m, s in zip(means, sds)]
    lower = [max(0, round(m - s, 2)) for m, s in zip(means, sds)]
    calm_pcts = [round(calm_by_hour.get(h, 0), 1) for h in hours]

    traces = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Mean Wind Speed",
            "x": hours,
            "y": means,
            "line": {"color": "#1f77b4", "width": 2},
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
            "fillcolor": "rgba(31,119,180,0.15)",
            "line": {"width": 0},
            "showlegend": False,
        },
        {
            "type": "bar",
            "name": "Calm %",
            "x": hours,
            "y": calm_pcts,
            "yaxis": "y2",
            "marker": {"color": "rgba(200,200,200,0.5)"},
        },
    ]

    layout = {
        "xaxis": {"title": "Hour of Day (EAT)", "dtick": 1},
        "yaxis": {"title": "Wind Speed (km/h)"},
        "yaxis2": {
            "title": "Calm %",
            "overlaying": "y",
            "side": "right",
            "range": [0, 100],
        },
        "showlegend": True,
        "legend": {"x": 0, "y": 1.12, "orientation": "h"},
    }

    return {
        "id": "diurnal-wind",
        "title": "Diurnal Wind Pattern",
        "title_sw": "Mtindo wa Upepo wa Kila Siku",
        "data": traces,
        "layout": layout,
        "monthlyDiurnal": monthly_diurnal,
    }


def _build_wind_distribution(wdf, k_val, c_val):
    """Build wind speed histogram with optional Weibull overlay."""
    speeds = wdf["avg_wind_kph"].dropna().values

    # Histogram bins of 0.5 km/h
    max_speed = min(float(np.nanmax(speeds)) + 1, 50)
    bins = np.arange(0, max_speed + 0.5, 0.5)
    hist, bin_edges = np.histogram(speeds, bins=bins)

    # Separate calm bar
    calm_count = int((speeds == 0).sum())
    bin_centers = [(bin_edges[i] + bin_edges[i + 1]) / 2 for i in range(len(hist))]

    traces = [{
        "type": "bar",
        "name": "Frequency",
        "x": [round(b, 1) for b in bin_centers],
        "y": hist.tolist(),
        "marker": {"color": "#1f77b4"},
    }]

    # Weibull overlay if available
    weibull_x = []
    weibull_y = []
    if k_val and c_val and k_val > 0:
        non_zero_count = int((speeds > 0).sum())
        bin_width = 0.5
        wx = np.arange(0.25, max_speed, 0.5)
        for x in wx:
            pdf = (k_val / c_val) * (x / c_val) ** (k_val - 1) * math.exp(-(x / c_val) ** k_val)
            weibull_x.append(round(float(x), 2))
            weibull_y.append(round(float(pdf * non_zero_count * bin_width), 2))

        traces.append({
            "type": "scatter",
            "mode": "lines",
            "name": f"Weibull fit (k={k_val}, c={c_val})",
            "x": weibull_x,
            "y": weibull_y,
            "line": {"color": "#d62728", "width": 2, "dash": "dash"},
        })

    layout = {
        "xaxis": {"title": "Wind Speed (km/h)"},
        "yaxis": {"title": "Count"},
        "showlegend": True,
        "bargap": 0.05,
    }

    return {
        "id": "wind-distribution",
        "title": "Wind Speed Distribution",
        "title_sw": "Usambazaji wa Kasi ya Upepo",
        "data": traces,
        "layout": layout,
        "calmCount": calm_count,
        "weibullK": k_val,
        "weibullC": c_val,
    }


def _build_gust_factor(wdf):
    """Build gust factor scatter plot (gust factor vs avg speed, colored by hour)."""
    valid = wdf[(wdf["avg_wind_kph"] > 0) & wdf["peak_wind_kph"].notna()].copy()
    valid["gust_factor"] = valid["peak_wind_kph"] / valid["avg_wind_kph"]
    valid["hour"] = valid["timestamp"].dt.hour

    # Limit to reasonable gust factors (filter extreme outliers)
    valid = valid[valid["gust_factor"] < 20]

    timestamps = [to_eat_ms(t) for t in valid["timestamp"]]

    traces = [{
        "type": "scatter",
        "mode": "markers",
        "name": "Gust Factor",
        "x_ms": timestamps,
        "x_speed": [round(v, 1) for v in valid["avg_wind_kph"]],
        "y": [round(v, 2) for v in valid["gust_factor"]],
        "marker": {
            "color": valid["hour"].tolist(),
            "colorscale": "Viridis",
            "colorbar": {"title": "Hour"},
            "size": 4,
            "opacity": 0.6,
        },
    }]

    # Mean gust factor
    mean_gf = round(valid["gust_factor"].mean(), 2) if len(valid) else 0
    median_gf = round(valid["gust_factor"].median(), 2) if len(valid) else 0

    layout = {
        "xaxis": {"title": "Average Wind Speed (km/h)"},
        "yaxis": {"title": "Gust Factor (peak/avg)"},
        "shapes": [{
            "type": "line",
            "x0": 0, "x1": float(valid["avg_wind_kph"].max()) if len(valid) else 30,
            "y0": 2.0, "y1": 2.0,
            "line": {"color": "red", "width": 1, "dash": "dash"},
        }],
    }

    return {
        "id": "gust-factor",
        "title": "Gust Factor Analysis",
        "title_sw": "Uchambuzi wa Kipengele cha Upepo Mkali",
        "data": traces,
        "layout": layout,
        "meanGustFactor": mean_gf,
        "medianGustFactor": median_gf,
    }


def _build_calm_periods(wdf):
    """Build calm period analysis: distribution of consecutive calm durations."""
    is_calm = (wdf["avg_wind_kph"] == 0).values
    timestamps = wdf["timestamp"].values

    # Find consecutive calm periods
    calm_periods = []
    in_calm = False
    start_idx = 0
    for i, c in enumerate(is_calm):
        if c and not in_calm:
            in_calm = True
            start_idx = i
        elif not c and in_calm:
            in_calm = False
            duration_min = (timestamps[i - 1] - timestamps[start_idx]) / np.timedelta64(1, "m")
            calm_periods.append({
                "start_ms": int(pd.Timestamp(timestamps[start_idx]).timestamp() * 1000),
                "end_ms": int(pd.Timestamp(timestamps[i - 1]).timestamp() * 1000),
                "duration_min": round(float(duration_min), 1),
            })
    if in_calm:
        duration_min = (timestamps[-1] - timestamps[start_idx]) / np.timedelta64(1, "m")
        calm_periods.append({
            "start_ms": int(pd.Timestamp(timestamps[start_idx]).timestamp() * 1000),
            "end_ms": int(pd.Timestamp(timestamps[-1]).timestamp() * 1000),
            "duration_min": round(float(duration_min), 1),
        })

    # Duration bins
    bin_edges = [0, 5, 30, 60, 180, 360, 720, 1440, 99999]
    bin_labels = ["<5min", "5-30min", "30min-1h", "1-3h", "3-6h", "6-12h", "12-24h", "24h+"]
    bin_counts = [0] * len(bin_labels)
    for cp in calm_periods:
        d = cp["duration_min"]
        for j in range(len(bin_edges) - 1):
            if bin_edges[j] <= d < bin_edges[j + 1]:
                bin_counts[j] += 1
                break

    # Stats
    durations = [cp["duration_min"] for cp in calm_periods]
    longest = max(durations) if durations else 0
    mean_dur = round(sum(durations) / len(durations), 1) if durations else 0
    total_days = (wdf["timestamp"].max() - wdf["timestamp"].min()).total_seconds() / 86400
    calms_per_day = round(len(calm_periods) / total_days, 1) if total_days > 0 else 0

    traces = [{
        "type": "bar",
        "orientation": "h",
        "name": "Calm Periods",
        "y": bin_labels,
        "x": bin_counts,
        "marker": {"color": "#999"},
    }]

    layout = {
        "xaxis": {"title": "Number of Periods"},
        "yaxis": {"title": "Duration", "autorange": "reversed"},
    }

    return {
        "id": "calm-periods",
        "title": "Calm Period Analysis",
        "title_sw": "Uchambuzi wa Vipindi vya Utulivu",
        "data": traces,
        "layout": layout,
        "longestCalmMin": round(longest, 1),
        "meanCalmMin": mean_dur,
        "calmsPerDay": calms_per_day,
        "calmPeriodCount": len(calm_periods),
        "calmTimeline": calm_periods[:200],  # Limit for JSON size
    }


def _build_ventilation_availability(wdf):
    """Build ventilation availability stacked area chart."""
    wdf_c = wdf.copy()
    wdf_c["date"] = wdf_c["timestamp"].dt.date

    # Default threshold: 3.5 km/h (~1 m/s)
    # We'll compute for multiple thresholds and let JS select
    thresholds = [1, 2, 3.5, 5, 7, 10]
    threshold_data = {}

    for thresh in thresholds:
        daily = []
        for date, group in wdf_c.groupby("date"):
            total = len(group)
            effective = (group["avg_wind_kph"] >= thresh).sum()
            marginal = ((group["avg_wind_kph"] > 0) & (group["avg_wind_kph"] < thresh)).sum()
            calm = (group["avg_wind_kph"] == 0).sum()

            # Convert to hours (assuming ~5-min intervals)
            interval_h = 5 / 60
            dt = pd.Timestamp(date).tz_localize(TIMEZONE)
            daily.append({
                "date_ms": int(dt.timestamp() * 1000),
                "effective_h": round(effective * interval_h, 1),
                "marginal_h": round(marginal * interval_h, 1),
                "calm_h": round(calm * interval_h, 1),
            })
        threshold_data[str(thresh)] = daily

    # Default threshold stats
    default_thresh = 3.5
    all_effective = (wdf_c["avg_wind_kph"] >= default_thresh).sum()
    all_marginal = ((wdf_c["avg_wind_kph"] > 0) & (wdf_c["avg_wind_kph"] < default_thresh)).sum()
    all_calm = (wdf_c["avg_wind_kph"] == 0).sum()
    total = len(wdf_c)
    eff_pct = round(all_effective / total * 100, 1) if total else 0

    # Build default traces
    default_data = threshold_data["3.5"]
    dates_ms = [d["date_ms"] for d in default_data]
    eff_hours = [d["effective_h"] for d in default_data]
    marg_hours = [d["marginal_h"] for d in default_data]
    calm_hours = [d["calm_h"] for d in default_data]

    traces = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Effective",
            "x_ms": dates_ms,
            "y": eff_hours,
            "fill": "tozeroy",
            "fillcolor": "rgba(44,160,44,0.5)",
            "line": {"color": VENTILATION_COLORS["effective"]},
            "stackgroup": "vent",
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Marginal",
            "x_ms": dates_ms,
            "y": marg_hours,
            "fill": "tonexty",
            "fillcolor": "rgba(255,191,0,0.5)",
            "line": {"color": VENTILATION_COLORS["marginal"]},
            "stackgroup": "vent",
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Calm",
            "x_ms": dates_ms,
            "y": calm_hours,
            "fill": "tonexty",
            "fillcolor": "rgba(214,39,40,0.3)",
            "line": {"color": VENTILATION_COLORS["closed"]},
            "stackgroup": "vent",
        },
    ]

    layout = {
        "xaxis": {"title": "Date (EAT)"},
        "yaxis": {"title": "Hours per Day", "range": [0, 24]},
        "showlegend": True,
        "legend": {"x": 0, "y": 1.12, "orientation": "h"},
    }

    return {
        "id": "ventilation-availability",
        "title": "Ventilation Availability",
        "title_sw": "Upatikanaji wa Hewa",
        "data": traces,
        "layout": layout,
        "thresholdData": threshold_data,
        "effectivePct": eff_pct,
        "defaultThreshold": default_thresh,
    }


def _build_wind_category_distribution(wdf):
    """Build wind speed distribution by classification category (Beaufort default).

    The JS always rebuilds this chart from raw data to support interactive
    switching between classification systems and time denominators. This
    Python function provides the initial fallback for the all-time Beaufort view.
    """
    speeds_kph = wdf["avg_wind_kph"].dropna().values
    n_total = len(speeds_kph)

    # Beaufort bands (thresholds in knots, converted to km/h)
    bf_bands = WIND_CLASSIFICATIONS["beaufort"]["bands"]
    labels = [b["label"] for b in bf_bands]
    counts = []
    for b in bf_bands:
        lo_kph = b["lo"] * KN_TO_KPH
        hi_kph = (b["hi"] or 9999) * KN_TO_KPH
        count = int(((speeds_kph >= lo_kph) & (speeds_kph < hi_kph)).sum())
        counts.append(count)

    # Default: hours per day
    hrs_per_day = [round(c / n_total * 24, 2) if n_total else 0 for c in counts]

    # Sequential palette: blue (calm) -> red (severe), 10 steps
    palette = [
        "#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8",
        "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026",
    ]

    traces = [{
        "type": "bar",
        "orientation": "h",
        "name": "Hours per day",
        "y": labels,
        "x": hrs_per_day,
        "marker": {"color": palette[:len(labels)]},
    }]

    layout = {
        "xaxis": {"title": "Hours per day"},
        "yaxis": {"autorange": "reversed", "title": ""},
        "showlegend": False,
    }

    return {
        "id": "wind-category-dist",
        "title": "Wind Speed Categories",
        "title_sw": "Makundi ya Kasi ya Upepo",
        "data": traces,
        "layout": layout,
    }
