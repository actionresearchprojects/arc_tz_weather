"""
Cross-variable analyses combining wind, solar, and precipitation data.
Driving rain index, wind-rain coincidence, solar-wind correlation,
pre-storm signatures, ventilation window analysis.
"""

import math

import pandas as pd
import numpy as np

from .common import (
    VENTILATION_COLORS, to_eat_ms, compass_bin,
    get_season_boundaries,
)


def process(df, rain_events=None):
    """Process cross-variable analyses and return chart configs, stats."""
    xdf = df.copy()

    charts = []

    # ── 1. Driving Rain Index ─────────────────────────────────────────────
    charts.append(_build_driving_rain_index(xdf))

    # ── 2. Wind-Rain Coincidence ──────────────────────────────────────────
    charts.append(_build_wind_rain_coincidence(xdf))

    # ── 3. Solar-Wind Correlation ─────────────────────────────────────────
    charts.append(_build_solar_wind_correlation(xdf))

    # ── 4. Pre-Storm Signatures ───────────────────────────────────────────
    charts.append(_build_pre_storm_signatures(xdf, rain_events))

    # ── 5. Ventilation Window Analysis ────────────────────────────────────
    charts.append(_build_ventilation_windows(xdf))

    # ── Stats ─────────────────────────────────────────────────────────────
    # Wind-rain coincidence stats
    raining = xdf["precip_rate_mmh"] > 0
    windy = xdf["avg_wind_kph"] > 3.5
    rain_with_wind = (raining & windy).sum()
    rain_total = raining.sum()
    rain_wind_pct = round(rain_with_wind / rain_total * 100, 1) if rain_total else 0

    # Ventilation window stats
    effective = (xdf["avg_wind_kph"] >= 3.5) & (xdf["precip_rate_mmh"] == 0)
    vent_pct = round(effective.sum() / len(xdf) * 100, 1) if len(xdf) else 0

    stats = {
        "rainWithWindPct": rain_wind_pct,
        "ventilationWindowPct": vent_pct,
    }

    return {"charts": charts, "stats": stats}


def _build_driving_rain_index(xdf):
    """Build Driving Rain Index polar chart and time series."""
    # Only readings with both wind and rain
    wr = xdf[(xdf["avg_wind_kph"] > 0) & (xdf["precip_rate_mmh"] > 0)].copy()

    if len(wr) == 0:
        return {"id": "driving-rain", "title": "Driving Rain Index",
                "title_sw": "Fahirisi ya Mvua ya Upepo", "data": [], "layout": {},
                "directionalDRI": {}}

    # Convert wind speed to m/s
    wr["wind_ms"] = wr["avg_wind_kph"] / 3.6

    # DRI = v * r^(8/9) for each reading (direction-independent magnitude)
    wr["dri"] = wr["wind_ms"] * (wr["precip_rate_mmh"] ** (8 / 9))

    # Directional DRI: accumulate by wind direction (16-point)
    wr["compass"] = wr["wind_dir"].apply(lambda d: compass_bin(d, 16))
    dir_labels = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

    dir_dri = []
    for dl in dir_labels:
        total = wr[wr["compass"] == dl]["dri"].sum()
        dir_dri.append(round(float(total), 2))

    # Dominant direction
    max_idx = int(np.argmax(dir_dri)) if dir_dri else 0
    dominant_dir = dir_labels[max_idx] if dir_dri else "N/A"

    # Facade DRI (N, E, S, W)
    facade_dri = {}
    for facade_dir in [0, 90, 180, 270]:
        facade_label = {0: "N", 90: "E", 180: "S", 270: "W"}[facade_dir]
        total = 0
        for _, row in wr.iterrows():
            if pd.notna(row["wind_dir"]):
                angle_diff = math.radians(row["wind_dir"] - facade_dir)
                cos_val = math.cos(angle_diff)
                if cos_val > 0:
                    total += row["dri"] * cos_val
        facade_dri[facade_label] = round(total, 1)

    # Polar chart traces
    traces = [{
        "type": "barpolar",
        "r": dir_dri,
        "theta": dir_labels,
        "name": "DRI",
        "marker": {"color": "#1f77b4"},
    }]

    layout = {
        "polar": {
            "angularaxis": {
                "direction": "clockwise",
                "rotation": 90,
            },
            "radialaxis": {"visible": True},
        },
    }

    # Time series of DRI
    ts_timestamps = [to_eat_ms(t) for t in wr["timestamp"]]
    ts_dri = [round(v, 2) for v in wr["dri"]]

    return {
        "id": "driving-rain",
        "title": "Driving Rain Index",
        "title_sw": "Fahirisi ya Mvua ya Upepo",
        "data": traces,
        "layout": layout,
        "timeseries": {
            "x_ms": ts_timestamps,
            "y": ts_dri,
        },
        "directionalDRI": dir_dri,
        "facadeDRI": facade_dri,
        "dominantDir": dominant_dir,
    }


def _build_wind_rain_coincidence(xdf):
    """Build wind-rain coincidence heatmap."""
    raining = xdf[xdf["precip_rate_mmh"] > 0].copy()

    if len(raining) == 0:
        return {"id": "wind-rain", "title": "Wind-Rain Coincidence",
                "title_sw": "Upepo na Mvua Wakati Mmoja", "data": [], "layout": {}}

    # Wind speed bins
    wind_bins = [0, 2, 5, 10, 15, 20, 999]
    wind_labels = ["0-2", "2-5", "5-10", "10-15", "15-20", "20+"]

    # Rain rate bins
    rain_bins = [0, 2, 5, 10, 20, 50, 999]
    rain_labels = ["0-2", "2-5", "5-10", "10-20", "20-50", "50+"]

    total_rainy = len(raining)

    # 2D histogram: percentage of all rainy periods in each cell
    z_pct = []
    text_vals = []
    for ri in range(len(rain_labels)):
        row_pct = []
        row_text = []
        for wi in range(len(wind_labels)):
            mask = (
                (raining["avg_wind_kph"] >= wind_bins[wi]) &
                (raining["avg_wind_kph"] < wind_bins[wi + 1]) &
                (raining["precip_rate_mmh"] >= rain_bins[ri]) &
                (raining["precip_rate_mmh"] < rain_bins[ri + 1])
            )
            count = int(mask.sum())
            pct = round(count / total_rainy * 100, 1) if total_rainy else 0
            row_pct.append(pct)
            row_text.append(f"{pct}%" if pct > 0 else "")
        z_pct.append(row_pct)
        text_vals.append(row_text)

    traces = [{
        "type": "heatmap",
        "x": wind_labels,
        "y": rain_labels,
        "z": z_pct,
        "text": text_vals,
        "texttemplate": "%{text}",
        "textfont": {"size": 11},
        "colorscale": "Blues",
        "colorbar": {"title": "% of rainy<br>periods"},
    }]

    layout = {
        "xaxis": {"title": "Wind Speed (km/h)"},
        "yaxis": {"title": "Rain Rate (mm/h)"},
    }

    return {
        "id": "wind-rain",
        "title": "Wind-Rain Coincidence",
        "title_sw": "Upepo na Mvua Wakati Mmoja",
        "data": traces,
        "layout": layout,
    }


def _build_solar_wind_correlation(xdf):
    """Build solar-wind correlation scatter plot."""
    # Daytime only
    daytime = xdf[xdf["solar_wm2"] > 0].copy()

    if len(daytime) == 0:
        return {"id": "solar-wind", "title": "Solar-Wind Correlation",
                "title_sw": "Uhusiano wa Jua na Upepo", "data": [], "layout": {}}

    daytime["hour"] = daytime["timestamp"].dt.hour

    # Subsample if too many points
    if len(daytime) > 5000:
        daytime = daytime.sample(5000, random_state=42)

    traces = [{
        "type": "scatter",
        "mode": "markers",
        "name": "Readings",
        "x": [round(v, 1) for v in daytime["solar_wm2"]],
        "y": [round(v, 1) for v in daytime["avg_wind_kph"]],
        "marker": {
            "color": daytime["hour"].tolist(),
            "colorscale": "Viridis",
            "colorbar": {"title": "Hour"},
            "size": 3,
            "opacity": 0.4,
        },
    }]

    # Correlation coefficient
    corr = round(float(daytime["solar_wm2"].corr(daytime["avg_wind_kph"])), 3)

    layout = {
        "xaxis": {"title": "Solar Radiation (W/m\u00b2)"},
        "yaxis": {"title": "Wind Speed (km/h)"},
    }

    return {
        "id": "solar-wind",
        "title": "Solar-Wind Correlation",
        "title_sw": "Uhusiano wa Jua na Upepo",
        "data": traces,
        "layout": layout,
        "correlation": corr,
    }


def _build_pre_storm_signatures(xdf, rain_events):
    """Build composite pre-storm signature plot."""
    if not rain_events or len(rain_events) < 3:
        return {"id": "pre-storm", "title": "Pre-Storm Signatures",
                "title_sw": "Dalili za Kabla ya Dhoruba", "data": [], "layout": {},
                "eventCount": 0}

    # For each rain event, extract 2h before to 2h after
    window_min = 120  # minutes before/after
    interval_min = 5

    # Aligned arrays
    n_bins = int(2 * window_min / interval_min) + 1
    rel_minutes = np.linspace(-window_min, window_min, n_bins)

    wind_aligned = []
    solar_aligned = []

    xdf_indexed = xdf.set_index("timestamp").sort_index()

    for event in rain_events[:50]:  # Limit for performance
        event_start = pd.Timestamp(event["start_ms"], unit="ms", tz="UTC").tz_convert("Africa/Dar_es_Salaam")

        wind_event = []
        solar_event = []

        for rel_m in rel_minutes:
            target_time = event_start + pd.Timedelta(minutes=rel_m)
            # Find nearest reading within 3 minutes
            nearby = xdf_indexed.index.get_indexer([target_time], method="nearest")
            idx = nearby[0]
            if 0 <= idx < len(xdf_indexed):
                actual_time = xdf_indexed.index[idx]
                if abs((actual_time - target_time).total_seconds()) < 180:
                    wind_event.append(float(xdf_indexed.iloc[idx]["avg_wind_kph"]))
                    solar_event.append(float(xdf_indexed.iloc[idx]["solar_wm2"]))
                else:
                    wind_event.append(np.nan)
                    solar_event.append(np.nan)
            else:
                wind_event.append(np.nan)
                solar_event.append(np.nan)

        wind_aligned.append(wind_event)
        solar_aligned.append(solar_event)

    # Average across events
    wind_arr = np.array(wind_aligned)
    solar_arr = np.array(solar_aligned)

    wind_mean = np.nanmean(wind_arr, axis=0)
    solar_mean = np.nanmean(solar_arr, axis=0)

    rel_hours = [round(m / 60, 2) for m in rel_minutes]
    wind_vals = [round(v, 1) if not np.isnan(v) else None for v in wind_mean]
    solar_vals = [round(v, 1) if not np.isnan(v) else None for v in solar_mean]

    traces = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Wind Speed (km/h)",
            "x": rel_hours,
            "y": wind_vals,
            "line": {"color": "#1f77b4", "width": 2},
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Solar Radiation (W/m\u00b2)",
            "x": rel_hours,
            "y": solar_vals,
            "yaxis": "y2",
            "line": {"color": "#ff8c00", "width": 2},
        },
    ]

    layout = {
        "xaxis": {"title": "Hours Relative to Rain Start", "zeroline": True},
        "yaxis": {"title": "Wind Speed (km/h)"},
        "yaxis2": {
            "title": "Solar Radiation (W/m\u00b2)",
            "overlaying": "y",
            "side": "right",
        },
        "shapes": [{
            "type": "line",
            "x0": 0, "x1": 0,
            "y0": 0, "y1": 1, "yref": "paper",
            "line": {"color": "red", "width": 2, "dash": "dash"},
        }],
        "annotations": [{
            "x": 0, "y": 1.05, "yref": "paper",
            "text": "Rain Start",
            "showarrow": False,
            "font": {"color": "red"},
        }],
        "showlegend": True,
        "legend": {"x": 0, "y": 1.15, "orientation": "h"},
    }

    return {
        "id": "pre-storm",
        "title": "Pre-Storm Signatures",
        "title_sw": "Dalili za Kabla ya Dhoruba",
        "data": traces,
        "layout": layout,
        "eventCount": len(rain_events),
    }


def _build_ventilation_windows(xdf):
    """Build ventilation window heatmap (hour x date, coloured by condition)."""
    xdf_c = xdf.copy()
    xdf_c["date"] = xdf_c["timestamp"].dt.date
    xdf_c["hour"] = xdf_c["timestamp"].dt.hour

    dates = sorted(xdf_c["date"].unique())
    hours = list(range(24))

    # For each date-hour combination, classify ventilation condition
    # 0 = no data, 1 = effective (green), 2 = marginal (yellow), 3 = closed (red)
    z = []
    date_labels = []
    for date in dates:
        row = []
        day_data = xdf_c[xdf_c["date"] == date]
        for hour in hours:
            hour_data = day_data[day_data["hour"] == hour]
            if len(hour_data) == 0:
                row.append(0)
                continue

            mean_wind = hour_data["avg_wind_kph"].mean()
            max_rain_rate = hour_data["precip_rate_mmh"].max()

            if max_rain_rate >= 2.5:
                row.append(3)  # Closed
            elif mean_wind >= 3.5 and max_rain_rate == 0:
                row.append(1)  # Effective
            else:
                row.append(2)  # Marginal

        z.append(row)
        date_labels.append(str(date))

    # Custom colorscale: 0=grey, 1=green, 2=yellow, 3=red
    colorscale = [
        [0, "#e0e0e0"],
        [0.33, "#2ca02c"],
        [0.67, "#ffbf00"],
        [1.0, "#d62728"],
    ]

    traces = [{
        "type": "heatmap",
        "x": hours,
        "y": date_labels,
        "z": z,
        "colorscale": colorscale,
        "zmin": 0,
        "zmax": 3,
        "showscale": False,
    }]

    layout = {
        "xaxis": {"title": "Hour of Day (EAT)", "dtick": 1},
        "yaxis": {"title": "Date", "autorange": "reversed"},
    }

    # Overall stats
    total_cells = sum(len(row) for row in z)
    effective_cells = sum(sum(1 for c in row if c == 1) for row in z)
    marginal_cells = sum(sum(1 for c in row if c == 2) for row in z)
    closed_cells = sum(sum(1 for c in row if c == 3) for row in z)
    data_cells = effective_cells + marginal_cells + closed_cells

    eff_pct = round(effective_cells / data_cells * 100, 1) if data_cells else 0
    marg_pct = round(marginal_cells / data_cells * 100, 1) if data_cells else 0
    closed_pct = round(closed_cells / data_cells * 100, 1) if data_cells else 0

    return {
        "id": "ventilation-windows",
        "title": "Ventilation Windows",
        "title_sw": "Madirisha ya Hewa",
        "data": traces,
        "layout": layout,
        "effectivePct": eff_pct,
        "marginalPct": marg_pct,
        "closedPct": closed_pct,
    }
