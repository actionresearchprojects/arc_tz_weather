"""
Precipitation data processing and chart generation.
Cumulative rainfall, daily bars, intensity distribution, diurnal pattern, dry spells, rain events.
"""

import pandas as pd
import numpy as np

from .common import (
    RAIN_INTENSITY_COLORS, RAIN_DAILY_COLORS, TIMEZONE,
    detect_precip_resets, to_eat_ms, get_season_boundaries, compass_bin,
)


def process(df):
    """Process precipitation data and return chart configs, stats."""
    pdf = df.copy()

    # Correct cumulative precipitation resets
    pdf["precip_corrected"] = detect_precip_resets(pdf["precip_total_mm"])

    # Compute incremental rainfall
    pdf["precip_incr"] = pdf["precip_corrected"].diff().clip(lower=0).fillna(0)

    pdf["date"] = pdf["timestamp"].dt.date

    charts = []

    # Rain event detection
    events = _detect_rain_events(pdf)

    # Daily totals
    daily = _compute_daily_rainfall(pdf)

    # ── Summary Statistics ────────────────────────────────────────────────
    total_rainfall = round(pdf["precip_corrected"].iloc[-1], 1) if len(pdf) else 0
    rainy_days = len([d for d in daily if d["total_mm"] > 0.2])
    total_days = len(daily)
    mean_daily_rainy = round(
        sum(d["total_mm"] for d in daily if d["total_mm"] > 0.2) / rainy_days, 1
    ) if rainy_days else 0
    max_daily = round(max((d["total_mm"] for d in daily), default=0), 1)

    # Rate stats
    rates = pdf[pdf["precip_rate_mmh"] > 0]["precip_rate_mmh"]
    median_rate = round(rates.median(), 1) if len(rates) else 0
    p95_rate = round(rates.quantile(0.95), 1) if len(rates) else 0
    max_rate = round(rates.max(), 1) if len(rates) else 0

    stats = {
        "totalRainfall": total_rainfall,
        "rainyDays": rainy_days,
        "totalDays": total_days,
        "meanDailyRainy": mean_daily_rainy,
        "maxDailyRainfall": max_daily,
        "medianIntensity": median_rate,
        "p95Intensity": p95_rate,
        "maxIntensity": max_rate,
        "eventCount": len(events),
        "eventsPerWeek": round(len(events) / (total_days / 7), 1) if total_days else 0,
    }

    # ── 1. Cumulative Rainfall ────────────────────────────────────────────
    charts.append(_build_cumulative_rainfall(pdf))

    # ── 2. Daily Rainfall Bar Chart ───────────────────────────────────────
    charts.append(_build_daily_rainfall(daily))

    # ── 3. Rainfall Intensity Distribution ────────────────────────────────
    charts.append(_build_intensity_distribution(pdf))

    # ── 4. Diurnal Rainfall Pattern ───────────────────────────────────────
    charts.append(_build_diurnal_rainfall(pdf))

    # ── 5. Dry Spell Analysis ─────────────────────────────────────────────
    charts.append(_build_dry_spells(pdf))

    # ── 6. Rain Events Table ──────────────────────────────────────────────
    charts.append(_build_rain_events(events))

    return {"charts": charts, "stats": stats}


def _compute_daily_rainfall(pdf):
    """Compute daily rainfall totals from corrected cumulative series."""
    daily = []
    for date, group in pdf.groupby("date"):
        group = group.sort_values("timestamp")
        total = group["precip_incr"].sum()
        daily.append({
            "date": date,
            "date_ms": int(pd.Timestamp(date).tz_localize(TIMEZONE).timestamp() * 1000),
            "total_mm": round(float(total), 2),
        })
    return daily


def _detect_rain_events(pdf, gap_tolerance_min=15, min_depth_mm=0.5):
    """Detect rain events by grouping consecutive readings with rate > 0.

    Allows gaps of up to gap_tolerance_min minutes. Events with total depth
    below min_depth_mm (WMO trace threshold) are excluded.
    """
    # Compute median sampling interval for minimum duration fallback
    median_interval_min = pdf["timestamp"].diff().dt.total_seconds().median() / 60

    raining = pdf["precip_rate_mmh"] > 0
    events = []
    in_event = False
    event_start = None
    last_rain_idx = None
    event_rows = []

    for i, (idx, row) in enumerate(pdf.iterrows()):
        if raining.iloc[i]:
            if not in_event:
                in_event = True
                event_start = idx
                event_rows = [i]
            else:
                event_rows.append(i)
            last_rain_idx = i
        else:
            if in_event:
                # Check gap
                if last_rain_idx is not None and i < len(pdf) - 1:
                    time_since = (row["timestamp"] - pdf.iloc[last_rain_idx]["timestamp"]).total_seconds() / 60
                    if time_since <= gap_tolerance_min:
                        continue  # Still within tolerance
                # End event
                in_event = False
                event_data = pdf.iloc[event_rows[0]:last_rain_idx + 1]
                if len(event_data) > 0:
                    summary = _summarize_event(event_data, pdf, median_interval_min)
                    if summary["total_mm"] >= min_depth_mm:
                        events.append(summary)
                event_rows = []

    # Handle event at end of data
    if in_event and event_rows:
        event_data = pdf.iloc[event_rows[0]:last_rain_idx + 1]
        if len(event_data) > 0:
            summary = _summarize_event(event_data, pdf, median_interval_min)
            if summary["total_mm"] >= min_depth_mm:
                events.append(summary)

    return events


def _summarize_event(event_data, full_df, min_duration_min=5):
    """Summarize a single rain event."""
    start = event_data["timestamp"].iloc[0]
    end = event_data["timestamp"].iloc[-1]
    duration_min = (end - start).total_seconds() / 60

    total_mm = event_data["precip_incr"].sum()
    peak_rate = event_data["precip_rate_mmh"].max()
    mean_rate = event_data[event_data["precip_rate_mmh"] > 0]["precip_rate_mmh"].mean()

    # Prevailing wind direction during event
    wind_during = event_data[event_data["avg_wind_kph"] > 0]
    if len(wind_during) > 0:
        dirs = wind_during["wind_dir"].dropna()
        if len(dirs) > 0:
            prevailing_wind = compass_bin(dirs.mode().iloc[0] if len(dirs.mode()) > 0 else dirs.median())
        else:
            prevailing_wind = "N/A"
    else:
        prevailing_wind = "Calm"

    return {
        "start_ms": to_eat_ms(start),
        "end_ms": to_eat_ms(end),
        "duration_min": round(float(duration_min), 1),
        "total_mm": round(float(total_mm), 2),
        "peak_rate": round(float(peak_rate), 1),
        "mean_rate": round(float(mean_rate), 1) if not pd.isna(mean_rate) else 0,
        "wind_dir": prevailing_wind,
    }


def _build_cumulative_rainfall(pdf):
    """Build cumulative rainfall step chart."""
    timestamps = [to_eat_ms(t) for t in pdf["timestamp"]]
    values = [round(v, 1) for v in pdf["precip_corrected"]]
    season_bounds = get_season_boundaries(pdf)

    traces = [{
        "type": "scatter",
        "mode": "lines",
        "name": "Cumulative Rainfall",
        "x_ms": timestamps,
        "y": values,
        "line": {"color": "#1f77b4", "width": 2, "shape": "hv"},
        "fill": "tozeroy",
        "fillcolor": "rgba(31,119,180,0.15)",
    }]

    layout = {
        "yaxis": {"title": "Cumulative Rainfall (mm)"},
        "xaxis": {"title": "Date (EAT)"},
    }

    return {
        "id": "cumulative-rainfall",
        "title": "Cumulative Rainfall",
        "title_sw": "Mvua ya Jumla",
        "data": traces,
        "layout": layout,
        "seasonBoundaries": season_bounds,
    }


def _build_daily_rainfall(daily):
    """Build daily rainfall bar chart with intensity coloring."""
    dates_ms = [d["date_ms"] for d in daily]
    totals = [d["total_mm"] for d in daily]

    colors = []
    for v in totals:
        if v < 2.5:
            colors.append(RAIN_DAILY_COLORS["light"])
        elif v < 7.5:
            colors.append(RAIN_DAILY_COLORS["moderate"])
        elif v < 25:
            colors.append(RAIN_DAILY_COLORS["heavy"])
        else:
            colors.append(RAIN_DAILY_COLORS["very_heavy"])

    traces = [{
        "type": "bar",
        "name": "Daily Rainfall",
        "x_ms": dates_ms,
        "y": totals,
        "marker": {"color": colors},
    }]

    layout = {
        "yaxis": {"title": "Rainfall (mm)"},
        "xaxis": {"title": "Date (EAT)"},
    }

    return {
        "id": "daily-rainfall",
        "title": "Daily Rainfall",
        "title_sw": "Mvua ya Kila Siku",
        "data": traces,
        "layout": layout,
    }


def _build_intensity_distribution(pdf):
    """Build rainfall intensity histogram (non-zero rates, log y-axis)."""
    rates = pdf[pdf["precip_rate_mmh"] > 0]["precip_rate_mmh"].values

    if len(rates) == 0:
        return {"id": "rainfall-intensity", "title": "Rainfall Intensity",
                "title_sw": "Kiwango cha Mvua", "data": [], "layout": {}}

    bin_edges = [0, 2, 5, 10, 20, 50, 100, 200]
    bin_labels = ["0-2", "2-5", "5-10", "10-20", "20-50", "50-100", "100+"]
    bin_colors = [
        RAIN_INTENSITY_COLORS["light"],
        RAIN_INTENSITY_COLORS["light"],
        RAIN_INTENSITY_COLORS["moderate"],
        RAIN_INTENSITY_COLORS["moderate"],
        RAIN_INTENSITY_COLORS["heavy"],
        RAIN_INTENSITY_COLORS["heavy"],
        RAIN_INTENSITY_COLORS["very_heavy"],
    ]

    counts = []
    for i in range(len(bin_labels)):
        if i < len(bin_labels) - 1:
            c = int(((rates >= bin_edges[i]) & (rates < bin_edges[i + 1])).sum())
        else:
            c = int((rates >= bin_edges[i]).sum())
        counts.append(c)

    traces = [{
        "type": "bar",
        "name": "Frequency",
        "x": bin_labels,
        "y": counts,
        "marker": {"color": bin_colors},
    }]

    layout = {
        "xaxis": {"title": "Rainfall Rate (mm/h)"},
        "yaxis": {"title": "Count", "type": "log"},
        "bargap": 0.1,
    }

    return {
        "id": "rainfall-intensity",
        "title": "Rainfall Intensity Distribution",
        "title_sw": "Usambazaji wa Kiwango cha Mvua",
        "data": traces,
        "layout": layout,
    }


def _build_diurnal_rainfall(pdf):
    """Build diurnal rainfall pattern: mean hourly rainfall + rain probability."""
    pdf_c = pdf.copy()
    pdf_c["hour"] = pdf_c["timestamp"].dt.hour

    # Mean rainfall per hour (from incremental)
    hourly_rain = pdf_c.groupby("hour")["precip_incr"].mean()

    # Rain probability by hour
    hourly_prob = pdf_c.groupby("hour").apply(
        lambda g: (g["precip_rate_mmh"] > 0).sum() / len(g) * 100
    )

    hours = list(range(24))
    rain_means = [round(hourly_rain.get(h, 0), 3) for h in hours]
    rain_probs = [round(hourly_prob.get(h, 0), 1) for h in hours]

    traces = [
        {
            "type": "bar",
            "name": "Mean Rainfall (mm)",
            "x": hours,
            "y": rain_means,
            "marker": {"color": "#1f77b4"},
        },
        {
            "type": "scatter",
            "mode": "lines+markers",
            "name": "Rain Probability (%)",
            "x": hours,
            "y": rain_probs,
            "yaxis": "y2",
            "line": {"color": "#d62728", "width": 2},
            "marker": {"size": 5},
        },
    ]

    layout = {
        "xaxis": {"title": "Hour of Day (EAT)", "dtick": 1},
        "yaxis": {"title": "Mean Rainfall (mm)"},
        "yaxis2": {
            "title": "Rain Probability (%)",
            "overlaying": "y",
            "side": "right",
            "range": [0, max(rain_probs) * 1.2] if rain_probs else [0, 100],
        },
        "showlegend": True,
        "legend": {"x": 0, "y": 1.12, "orientation": "h"},
    }

    # Find peak rainfall hour
    peak_hour = int(np.argmax(rain_means)) if rain_means else 0

    return {
        "id": "diurnal-rainfall",
        "title": "Diurnal Rainfall Pattern",
        "title_sw": "Mtindo wa Mvua wa Kila Siku",
        "data": traces,
        "layout": layout,
        "peakHour": peak_hour,
    }


def _build_dry_spells(pdf):
    """Build dry spell analysis: distribution of dry period durations."""
    is_dry = (pdf["precip_rate_mmh"] == 0).values
    timestamps = pdf["timestamp"].values

    dry_spells = []
    in_dry = False
    start_idx = 0

    for i, d in enumerate(is_dry):
        if d and not in_dry:
            in_dry = True
            start_idx = i
        elif not d and in_dry:
            in_dry = False
            duration_h = (timestamps[i - 1] - timestamps[start_idx]) / np.timedelta64(1, "h")
            dry_spells.append(round(float(duration_h), 1))

    if in_dry:
        duration_h = (timestamps[-1] - timestamps[start_idx]) / np.timedelta64(1, "h")
        dry_spells.append(round(float(duration_h), 1))

    # Duration bins
    bin_edges = [0, 6, 12, 24, 72, 168, 99999]
    bin_labels = ["<6h", "6-12h", "12-24h", "1-3 days", "3-7 days", "7+ days"]
    bin_counts = [0] * len(bin_labels)
    for dur in dry_spells:
        for j in range(len(bin_edges) - 1):
            if bin_edges[j] <= dur < bin_edges[j + 1]:
                bin_counts[j] += 1
                break

    longest = max(dry_spells) if dry_spells else 0
    mean_dry = round(sum(dry_spells) / len(dry_spells), 1) if dry_spells else 0

    traces = [{
        "type": "bar",
        "orientation": "h",
        "name": "Dry Spells",
        "y": bin_labels,
        "x": bin_counts,
        "marker": {"color": "#ff8c00"},
    }]

    layout = {
        "xaxis": {"title": "Number of Spells"},
        "yaxis": {"title": "Duration", "autorange": "reversed"},
    }

    return {
        "id": "dry-spells",
        "title": "Dry Spell Analysis",
        "title_sw": "Uchambuzi wa Vipindi vya Ukame",
        "data": traces,
        "layout": layout,
        "longestDryH": round(longest, 1),
        "meanDryH": mean_dry,
        "spellCount": len(dry_spells),
    }


def _build_rain_events(events):
    """Build rain event summary data for interactive table."""
    return {
        "id": "rain-events",
        "title": "Rain Events",
        "title_sw": "Matukio ya Mvua",
        "data": [],
        "layout": {},
        "events": events[:100],  # Limit for JSON size
        "isTable": True,
    }
