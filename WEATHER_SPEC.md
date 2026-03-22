# ARC Tanzania Weather Station Dashboard - Specification

This document is the complete blueprint for building the `arc_tz_weather` dashboard, a self-contained, static HTML dashboard for wind, solar radiation, and precipitation data from the Omnisense weather station (sensor ID `30B40014`) at the ARC ecovillage near Mkuranga, Tanzania. It mirrors the architecture of the existing `arc_tz_temp_humid` project.

---

## 1. Project Structure

```
arc_tz_weather/
  CLAUDE.md                 # Project conventions and rules (like temp/humid version)
  build.py                  # Monolithic build script: data processing + HTML template -> index.html
  fetch_omnisense.py        # Copy from arc_tz_temp_humid (or symlink). Not modified.
  dataflow.md               # Plain-English explainer of the data pipeline
  index.html                # GENERATED OUTPUT. Never edit directly.
  logo/
    logotrim.png            # ARC logo (copy from arc_tz_temp_humid)
  data/
    omnisense/
      omnisense_*.csv       # The shared Omnisense CSV (copied from arc_tz_temp_humid)
      legacy/               # Archived previous CSVs
  modules/
    wind.py                 # Wind data processing and chart generation
    solar.py                # Solar radiation processing and chart generation
    precipitation.py        # Precipitation processing and chart generation
    cross_variable.py       # Cross-variable analyses (wind+rain, solar+wind, etc.)
    common.py               # Shared utilities: CSV parsing, time helpers, colour palettes
  .github/
    workflows/
      update-dashboard-data.yml   # Daily build workflow
      notify-main-site.yml       # Push notification to main site
```

### Key differences from `arc_tz_temp_humid`

- **No TinyTag data, no Open-Meteo fetch, no climate cycles, no sensor_snapshot.json.** The only data source is the Omnisense CSV.
- **Modular Python files** under `modules/` rather than everything in `build.py`. Each module exports a function that returns chart data (JSON-serialisable dicts) and HTML snippets. `build.py` orchestrates them.
- **No `--auto` mode needed.** There is only one data source (Omnisense CSV), so the build always reads the latest CSV directly.

---

## 2. Data Pipeline

### 2.1 Data Source

The Omnisense CSV is fetched by the `arc_tz_temp_humid` repository's daily workflow. The weather dashboard does not fetch its own copy. Instead, the GitHub Action copies the latest CSV from the sibling repository.

### 2.2 CSV Structure

The Omnisense CSV contains multiple sensor sections. The weather station section is preceded by:

```
sensor_desc,site_name
Sun, Wind, Rain weather station gateway (in external box),ARC CEV Tanzania
sensorId,port,read_date,avg_wind_speed_kph,peak_wind_kph,wind_direction,solar_radiation,total_percipitation_mm,rate_percipitation_mm_h,battery_voltage
```

### 2.3 Parsing Strategy

1. **Find the weather station section.** Scan the CSV for the header row containing `avg_wind_speed_kph`. Read from that row onward, stopping at the next `sensor_desc,site_name` line or EOF.
2. **Filter by sensor ID.** Only keep rows where `sensorId == '30B40014'`.
3. **Parse timestamps.** The `read_date` column contains `YYYY-MM-DD HH:MM:SS` strings. These are in **EAT (UTC+3)**. Parse them into timezone-aware datetime objects.
4. **Handle the timestamped filename.** Use `glob("omnisense_*.csv")` and take the most recent file (sorted lexicographically, the filename contains a UTC timestamp like `omnisense_20260322_0449.csv`).

### 2.4 Column Definitions

| CSV Column | Type | Unit | Notes |
|---|---|---|---|
| `avg_wind_speed_kph` | float | km/h | 5-minute average wind speed. Value of 0 = calm. |
| `peak_wind_kph` | float | km/h | Peak gust in the 5-minute interval. Range observed: 0 to ~800 (likely erroneous spikes above ~100, see Section 8). |
| `wind_direction` | int | degrees (0-359) | Compass bearing. 0 = North. Only meaningful when avg_wind_speed > 0. |
| `solar_radiation` | float | W/m2 | Global horizontal irradiance. Range: 0 (night) to ~990. |
| `total_percipitation_mm` | float | mm | Cumulative rainfall total. Monotonically increasing within a period, with occasional resets (counter overflow or manual reset). |
| `rate_percipitation_mm_h` | float | mm/h | Instantaneous rainfall rate. 0 when not raining. |
| `battery_voltage` | float | V | Sensor battery. Typically 3.3-3.4V. |

### 2.5 Data Quality Notes

- **~11,200 rows** covering 2026-01-25 to 2026-03-20 (~55 days) at ~5-minute intervals.
- **35% calm readings** (avg_wind_speed = 0), which is significant and must be handled in wind roses and statistics.
- **Peak wind spikes**: Some peak values (e.g., 431, 611, 803 km/h) are clearly erroneous. Apply a spike filter: discard peak_wind_kph values > 150 km/h (Category 2 hurricane threshold; coastal Tanzania rarely exceeds 80 km/h).
- **Cumulative precipitation resets**: The `total_percipitation_mm` column resets at least once (observed: 64 -> 18 on 2026-02-21). The build must detect resets (negative differences) and reconstruct a true cumulative total by summing positive increments. Total rainfall over the period is ~329 mm.
- **Non-zero precipitation in 9,186 of 11,222 rows** for the total column (because it is cumulative), but only **242 rows** have non-zero `rate_percipitation_mm_h`, meaning actual rain events are relatively sparse.
- **5-minute gaps**: Some intervals have missing readings (e.g., 10-minute gaps). The build should be tolerant of irregular spacing.

---

## 3. GitHub Actions

### 3.1 `update-dashboard-data.yml`

Runs daily at **05:00 UTC** (after the temp/humidity workflow at 04:00 UTC, allowing time for it to complete and push the fresh CSV).

```yaml
name: Update weather dashboard

on:
  schedule:
    - cron: '0 5 * * *'  # daily at 05:00 UTC (08:00 EAT)
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.12'

      - name: Install build dependencies
        run: pip install pandas pytz

      - name: Copy latest Omnisense CSV from sibling repo
        run: |
          # Clone just the data/omnisense directory from arc_tz_temp_humid
          git clone --depth 1 --filter=blob:none --sparse \
            https://x-access-token:${{ secrets.MAIN_SITE_PAT }}@github.com/actionresearchprojects/arc_tz_temp_humid.git \
            /tmp/temp_humid
          cd /tmp/temp_humid
          git sparse-checkout set data/omnisense
          cd -
          # Copy the latest CSV
          mkdir -p data/omnisense
          cp /tmp/temp_humid/data/omnisense/omnisense_*.csv data/omnisense/ 2>/dev/null || true
          # Remove legacy subfolder if copied
          rm -rf data/omnisense/legacy
          ls -la data/omnisense/

      - name: Rebuild dashboard
        run: python build.py

      - name: Commit and push if changed
        id: push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "actions@users.noreply.github.com"
          git add data/omnisense/ index.html
          if git diff --cached --quiet; then
            echo "changed=false" >> $GITHUB_OUTPUT
          else
            git commit -m "auto-update weather data $(date -u +%Y-%m-%d)"
            git push
            echo "changed=true" >> $GITHUB_OUTPUT
          fi

      - name: Trigger main site sync
        if: steps.push.outputs.changed == 'true'
        run: |
          curl -X POST \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer ${{ secrets.MAIN_SITE_PAT }}" \
            https://api.github.com/repos/actionresearchprojects/actionresearchprojects.github.io/dispatches \
            -d '{"event_type":"sync-embedded","client_payload":{"source_repo":"arc_tz_weather"}}'
```

### 3.2 `notify-main-site.yml`

Identical pattern to the temp/humidity version:

```yaml
name: Notify main site on push

on:
  push:
    branches: [main]
    paths:
      - 'index.html'
      - 'logo/**'

jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger main site sync
        run: |
          curl -X POST \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer ${{ secrets.MAIN_SITE_PAT }}" \
            https://api.github.com/repos/actionresearchprojects/actionresearchprojects.github.io/dispatches \
            -d '{"event_type":"sync-embedded","client_payload":{"source_repo":"arc_tz_weather"}}'
```

---

## 4. Modules

Each module receives the parsed DataFrame and a date-range filter, and returns:
- **Chart data**: JSON-serialisable dicts ready for Plotly.
- **Summary statistics**: For the sidebar stats panels.
- **HTML snippets**: Any module-specific sidebar controls.

### 4.1 `modules/common.py`

Shared utilities:
- `load_weather_csv(csv_path)` - Parse the Omnisense CSV and extract sensor `30B40014` into a DataFrame with columns: `timestamp` (tz-aware EAT), `avg_wind_kph`, `peak_wind_kph`, `wind_dir`, `solar_wm2`, `precip_total_mm`, `precip_rate_mmh`, `battery_v`.
- `to_eat_string(dt)` - Format datetime for Plotly (avoids browser timezone conversion). Mirror the `toEATString()` JS helper from the existing project.
- `filter_date_range(df, start, end)` - Slice DataFrame by date range.
- `detect_precip_resets(series)` - Detect resets in cumulative precipitation and return corrected cumulative totals.
- `spike_filter(series, max_val)` - Replace values above threshold with NaN.
- `BEAUFORT_SCALE` - Dict mapping Beaufort numbers to speed ranges and descriptions.
- `COMPASS_DIRS` - 16-point compass rose labels and degree ranges.
- Colour palettes for wind speed bins, Beaufort scale, solar radiation levels.

### 4.2 `modules/wind.py`

Processes wind speed, gust, and direction data. Returns chart configs for:

1. **Wind Rose** (see Section 5.1)
2. **Wind Speed Time Series** (see Section 5.2)
3. **Diurnal Wind Pattern** (see Section 5.3)
4. **Wind Speed Distribution** (see Section 5.4)
5. **Gust Factor Analysis** (see Section 5.5)
6. **Calm Period Analysis** (see Section 5.6)
7. **Ventilation Availability** (see Section 5.7)

Derived metrics computed here:
- Hourly, daily, weekly averages
- Gust factor (peak / average per interval)
- Calm percentage (% of readings with avg_wind = 0)
- Prevailing direction (modal compass sector)
- Ventilation availability hours (wind speed above user-selected threshold)

### 4.3 `modules/solar.py`

Processes solar radiation data. Returns chart configs for:

1. **Solar Radiation Time Series** (see Section 5.8)
2. **Daily Insolation Profile** (see Section 5.9)
3. **Diurnal Solar Pattern** (see Section 5.10)
4. **Solar Distribution Histogram** (see Section 5.11)
5. **Clearness Index** (see Section 5.12)
6. **Peak Solar Hours** (see Section 5.13)

Derived metrics:
- Daily insolation (kWh/m2/day) by integrating W/m2 over time
- Clearness index (Kt = measured / theoretical clear-sky irradiance)
- Peak solar hours (equivalent hours at 1000 W/m2)
- Sunrise/sunset detection from radiation data (first/last non-zero reading)
- Daytime hours

### 4.4 `modules/precipitation.py`

Processes rainfall data. Returns chart configs for:

1. **Cumulative Rainfall Time Series** (see Section 5.14)
2. **Daily Rainfall Bar Chart** (see Section 5.15)
3. **Rainfall Intensity Distribution** (see Section 5.16)
4. **Diurnal Rainfall Pattern** (see Section 5.17)
5. **Dry Spell Analysis** (see Section 5.18)
6. **Rain Event Detection** (see Section 5.19)

Derived metrics:
- Daily, weekly, monthly totals (from differencing corrected cumulative values)
- Rain event detection (consecutive readings with rate > 0, with a gap tolerance of 15 minutes)
- Event statistics: duration, total, peak intensity, mean intensity
- Dry spell lengths (consecutive hours/days with no rain)
- Diurnal timing of rainfall (what hours does it typically rain?)

### 4.5 `modules/cross_variable.py`

Analyses that combine two or more variables. Returns chart configs for:

1. **Driving Rain Index** (see Section 5.20)
2. **Wind-Rain Coincidence** (see Section 5.21)
3. **Solar-Wind Correlation** (see Section 5.22)
4. **Pre-Storm Signatures** (see Section 5.23)
5. **Ventilation Window Analysis** (see Section 5.24)

---

## 5. Charts and Analyses

All charts use **Plotly.js** (client-side rendering), consistent with the existing project. All timestamps must use the `toEATString()` helper to prevent browser timezone conversion.

### Wind Module

#### 5.1 Wind Rose
- **Chart type**: Polar bar chart (Plotly `Barpolar`).
- **Description**: Shows the frequency of wind from each of 16 compass directions, with concentric colour bands for speed bins (0-5, 5-10, 10-15, 15+ km/h). Calm readings (speed = 0) shown as a central circle with percentage label.
- **Derived metrics**: Direction frequency (%), calm percentage, prevailing direction.
- **Presentation**: Interactive. Hover shows direction, speed bin, and count. Date range selector applies. Optional toggle to show by Beaufort scale instead of km/h bins.
- **Research value**: Reveals prevailing wind directions for the site, essential for orienting ventilation openings. Calm percentage indicates how often natural ventilation is wind-driven vs. buoyancy-driven.

#### 5.2 Wind Speed Time Series
- **Chart type**: Line chart with two traces (average and peak/gust), plus optional calm threshold band.
- **Description**: Continuous time series of 5-minute average wind speed and peak gust. Shaded band below a user-selectable calm threshold (default 1 km/h). Season boundary lines.
- **Derived metrics**: Running 24-hour mean overlaid as a smoothed line.
- **Presentation**: Interactive zoom/pan. Date range selector. Gust trace as a lighter/dashed line above the average.
- **Research value**: Shows temporal patterns, storm events, and the relationship between average and gust speeds. The calm threshold band highlights ventilation dead zones.

#### 5.3 Diurnal Wind Pattern (Periodic Averages)
- **Chart type**: Line chart with error bands, x-axis = hour of day (0-23).
- **Description**: Mean wind speed by hour of day, with shaded +/- 1 SD band. Separate traces for different months or seasons if enough data. Calm percentage by hour as a secondary y-axis bar chart.
- **Derived metrics**: Peak ventilation hours, calm-dominant hours.
- **Presentation**: Grouped by hour. Hover shows mean, SD, calm %, sample count.
- **Research value**: Identifies the daily ventilation cycle. In coastal Tanzania, sea/land breezes create predictable diurnal patterns. Knowing peak wind hours guides window operation schedules.

#### 5.4 Wind Speed Distribution (Histogram)
- **Chart type**: Histogram with optional Weibull fit overlay.
- **Description**: Distribution of 5-minute average wind speeds, with bins of 0.5 km/h. Separate bar for calm (0 km/h). Optional Weibull probability distribution fit (commonly used in wind analysis).
- **Derived metrics**: Mean, median, mode, 95th percentile, Weibull shape (k) and scale (c) parameters.
- **Presentation**: Stacked or overlay bar mode toggle (matching existing project pattern). Stats panel in sidebar.
- **Research value**: The shape of the distribution (and Weibull parameters) characterises the site's wind regime. A high calm percentage with low mean speed indicates buoyancy-driven ventilation will dominate.

#### 5.5 Gust Factor Analysis
- **Chart type**: Scatter plot (gust factor vs. average wind speed) with colour-coded time of day.
- **Description**: Each 5-minute reading plotted as gust factor (peak/avg) vs. average speed. Only includes readings where avg > 0. Colour represents hour of day. Horizontal reference line at gust factor = 2.0 (typical threshold for turbulent conditions).
- **Derived metrics**: Mean gust factor, gust factor by Beaufort category, turbulence intensity proxy.
- **Presentation**: Interactive scatter with hover details. Marginal histograms optional.
- **Research value**: High gust factors at low speeds indicate turbulent, gusty conditions that may cause discomfort even when mean ventilation is adequate. Relevant for window sizing and occupant comfort.

#### 5.6 Calm Period Analysis
- **Chart type**: Horizontal bar chart + timeline.
- **Description**: Distribution of consecutive calm period durations (0 km/h readings). Bar chart showing frequency of calm durations in bins (5min, 10-30min, 30min-1h, 1-3h, 3-6h, 6-12h, 12-24h, 24h+). Timeline view showing calm periods as blocks on a time axis.
- **Derived metrics**: Longest calm period, mean calm duration, calm periods per day.
- **Presentation**: Sidebar shows summary stats. Timeline is zoomable.
- **Research value**: Extended calm periods are critical for naturally ventilated buildings. If wind-driven ventilation fails for hours, the building relies on stack effect alone. This directly informs whether mechanical backup is needed.

#### 5.7 Ventilation Availability
- **Chart type**: Stacked area chart, x-axis = date, y-axis = hours per day.
- **Description**: For each day, shows hours in three categories: above ventilation threshold (effective wind), below threshold but non-zero (marginal), and calm (0 km/h). Threshold is user-adjustable (default 3.5 km/h, ~1 m/s, a common minimum for cross-ventilation).
- **Derived metrics**: Daily ventilation availability (%), weekly trend.
- **Presentation**: Date range selector. Threshold slider in sidebar (1-10 km/h). Stats panel shows overall availability percentage.
- **Research value**: Directly answers "what fraction of the time is natural ventilation effective?" This is a key metric for building performance assessment.

---

### Solar Module

#### 5.8 Solar Radiation Time Series
- **Chart type**: Area chart (filled line).
- **Description**: Continuous time series of global horizontal irradiance (W/m2). Fill colour uses a gradient from blue (low) through yellow to orange (high). Season boundary lines.
- **Derived metrics**: Daily max, daily total (kWh/m2).
- **Presentation**: Interactive zoom/pan. Date range selector.
- **Research value**: Shows solar intensity patterns, cloudy vs. clear days, and seasonal trends. Directly related to solar heat gain through windows and roofing.

#### 5.9 Daily Insolation Profile
- **Chart type**: Bar chart, one bar per day.
- **Description**: Daily solar insolation (kWh/m2/day) calculated by integrating 5-minute radiation readings. Colour intensity proportional to value. Reference line for typical clear-sky insolation at this latitude (~5.5 kWh/m2/day for Dar es Salaam).
- **Derived metrics**: Mean daily insolation, max, min, coefficient of variation.
- **Presentation**: Date range selector. Hover shows exact value and clearness index for that day.
- **Research value**: Daily insolation determines total solar heat gain and is a key input for thermal simulation. Days below the reference line indicate cloud cover that reduces cooling load.

#### 5.10 Diurnal Solar Pattern (Periodic Averages)
- **Chart type**: Line chart with error band, x-axis = hour of day (0-23).
- **Description**: Mean solar radiation by hour, with +/- 1 SD shading. Optional overlay of theoretical clear-sky envelope for the site's latitude (-7.065 S). Separate traces for different months if data spans multiple months.
- **Derived metrics**: Peak hour, duration of significant radiation (> 50 W/m2).
- **Presentation**: Hover shows mean, SD, clear-sky reference.
- **Research value**: The shape of the diurnal curve (and deviation from clear-sky) characterises the site's solar regime. Asymmetry (morning vs. afternoon) affects orientation-dependent heat gain. Cloud cover timing correlates with rainfall and affects cooling strategies.

#### 5.11 Solar Distribution Histogram
- **Chart type**: Histogram of non-zero radiation values.
- **Description**: Distribution of solar radiation readings during daylight hours (> 0 W/m2), with bins of 50 W/m2. Excludes night-time zeros.
- **Derived metrics**: Mean daytime irradiance, modal bin, percentage of readings above 500 W/m2 (high gain threshold).
- **Presentation**: Stats panel in sidebar.
- **Research value**: Bimodal distributions indicate frequent cloud interruption; unimodal high peaks indicate clear-sky dominance. This shapes expectations for cooling load variability.

#### 5.12 Clearness Index (Kt) Time Series
- **Chart type**: Scatter plot, one point per day, y-axis = Kt (0 to 1).
- **Description**: Daily clearness index Kt = measured daily insolation / theoretical extraterrestrial radiation for the site and date. Theoretical radiation calculated from latitude, day of year, and solar constant. Colour bands: clear (Kt > 0.65), partly cloudy (0.35-0.65), overcast (Kt < 0.35).
- **Derived metrics**: Mean Kt, distribution of sky conditions.
- **Presentation**: Date range selector. Hover shows Kt, measured, and theoretical values.
- **Research value**: Kt is a standard metric in solar engineering. It separates the effects of season (changing day length/sun angle) from weather (cloud cover), giving a clearer picture of actual sky conditions.

#### 5.13 Peak Solar Hours (PSH)
- **Chart type**: Bar chart, one bar per day.
- **Description**: Peak solar hours = daily insolation (kWh/m2) / 1 (kW/m2). Equivalent to the number of hours at full 1000 W/m2 irradiance. Useful for PV sizing and as a normalised measure of solar availability.
- **Derived metrics**: Mean PSH, trend line.
- **Presentation**: Compact bar chart. Reference line at site typical (5-5.5 PSH for coastal Tanzania).
- **Research value**: PSH is a standard metric for solar energy assessment and a quick proxy for solar heat gain potential.

---

### Precipitation Module

#### 5.14 Cumulative Rainfall Time Series
- **Chart type**: Step line chart (monotonically increasing after reset correction).
- **Description**: Corrected cumulative rainfall over the entire period. The raw `total_percipitation_mm` values are corrected for counter resets by detecting negative jumps and adding the pre-reset total. Step function because accumulation happens during rain events only.
- **Derived metrics**: Total rainfall for the period, daily rate.
- **Presentation**: Interactive zoom/pan. Date range selector.
- **Research value**: Shows the overall rainfall pattern and intensity of the wet season. The slope of the cumulative curve indicates rain intensity.

#### 5.15 Daily Rainfall Bar Chart
- **Chart type**: Vertical bar chart, one bar per day.
- **Description**: Daily rainfall totals derived from the corrected cumulative series (difference between end-of-day and start-of-day values). Colour intensity proportional to amount. Bars coloured by intensity category: light (< 2.5 mm), moderate (2.5-7.5 mm), heavy (7.5-25 mm), very heavy (> 25 mm).
- **Derived metrics**: Rainy days count, mean daily rainfall (rainy days only), max daily rainfall.
- **Presentation**: Date range selector. Stats panel in sidebar.
- **Research value**: Daily totals are the standard unit for rainfall analysis. Intensity categories help identify days when window closure was likely necessary.

#### 5.16 Rainfall Intensity Distribution
- **Chart type**: Histogram of non-zero `rate_percipitation_mm_h` values, with log-scale y-axis.
- **Description**: Distribution of instantaneous rainfall rates during rain events. Bins: 0-2, 2-5, 5-10, 10-20, 20-50, 50-100, 100+ mm/h. Log scale because most rain is light but rare intense events matter most for building design.
- **Derived metrics**: Median intensity, 95th percentile, max recorded intensity, percentage of time in each intensity category.
- **Presentation**: Stats panel. Horizontal reference lines for WMO intensity categories (light/moderate/heavy/violent).
- **Research value**: Intensity distribution determines whether rain is gentle (windows can stay open) or violent (driving rain risk, window closure needed). The 95th percentile intensity is a key design parameter.

#### 5.17 Diurnal Rainfall Pattern
- **Chart type**: Dual-axis chart: bar chart (mean hourly rainfall, mm) + line chart (rain probability, %).
- **Description**: For each hour of the day, shows (a) the mean rainfall amount and (b) the probability that it is raining (percentage of readings in that hour with rate > 0). Reveals the typical daily rainfall timing.
- **Derived metrics**: Peak rainfall hour, dry hours, rain probability by time of day.
- **Presentation**: x-axis = hour (0-23). Hover shows both metrics.
- **Research value**: In tropical coastal locations, rain often follows a diurnal pattern (afternoon convective storms). Knowing when rain is most likely guides window operation schedules and ventilation strategy.

#### 5.18 Dry Spell Analysis
- **Chart type**: Horizontal bar chart (distribution of dry spell durations) + timeline.
- **Description**: A dry spell is a consecutive period with no rainfall (rate = 0). Shows distribution of spell durations in bins (< 6h, 6-12h, 12-24h, 1-3 days, 3-7 days, 7+ days). Timeline view shows wet (coloured) and dry (grey) periods.
- **Derived metrics**: Longest dry spell, mean dry spell duration, longest wet spell.
- **Presentation**: Date range selector. Summary stats in sidebar.
- **Research value**: Dry spells indicate periods when windows can remain open without rain risk. Extended dry spells during the wet season may indicate drought stress or unusual weather patterns.

#### 5.19 Rain Event Summary Table
- **Chart type**: Interactive table (HTML table with sortable columns, not a Plotly chart).
- **Description**: Each detected rain event as a row: start time, end time, duration, total rainfall (mm), peak intensity (mm/h), mean intensity (mm/h), prevailing wind direction during event. Events detected by grouping consecutive readings with rate > 0, allowing up to 15-minute gaps.
- **Derived metrics**: Event count, mean event duration, mean event total, events per week.
- **Presentation**: Sortable by any column. Clicking an event could zoom the time series charts to that event's time range.
- **Research value**: Itemised rain events allow researchers to study individual storms and correlate with building observations (e.g., "did water ingress occur during event X?").

---

### Cross-Variable Module

#### 5.20 Driving Rain Index (DRI)
- **Chart type**: Polar bar chart (like wind rose, but weighted by rain) + time series.
- **Description**: The driving rain index (DRI) quantifies wind-driven rain exposure on building facades. Calculated as: DRI = rainfall_rate * wind_speed * cos(angle), where angle is the difference between wind direction and each facade orientation. Shows a directional DRI rose (which directions deliver the most driving rain) and a time series of DRI values.
- **Derived metrics**: Dominant driving rain direction, cumulative DRI by facade orientation (N/S/E/W), annual DRI estimate.
- **Presentation**: Polar chart for directional analysis. Time series with date range selector. Sidebar control to set building orientation (default: 0 degrees = North).
- **Research value**: Driving rain is the primary cause of moisture ingress through walls and windows. The DRI rose directly informs which facades need the most weather protection and which windows should be closed during storms.

#### 5.21 Wind-Rain Coincidence
- **Chart type**: Heatmap (2D histogram), x-axis = wind speed bins, y-axis = rain rate bins.
- **Description**: Joint frequency distribution of wind speed and rainfall rate during rain events. Shows how often rain coincides with strong winds (the most problematic combination for naturally ventilated buildings).
- **Derived metrics**: Percentage of rain time with wind > threshold, percentage of windy time with rain.
- **Presentation**: Heatmap with colour intensity = frequency. Marginal distributions on axes.
- **Research value**: Wind + rain simultaneous occurrence is the critical condition that forces window closure. If most rain falls during calm periods, windows can have rain shelters and stay open. If rain and wind coincide, occupants must choose between ventilation and dryness.

#### 5.22 Solar-Wind Correlation
- **Chart type**: Scatter plot, x-axis = solar radiation, y-axis = wind speed, colour = hour of day.
- **Description**: Explores the relationship between solar heating and wind speed. In coastal tropical locations, solar heating drives thermal convection, which may correlate with afternoon sea breezes.
- **Derived metrics**: Correlation coefficient (daytime only), lag analysis (does wind peak follow solar peak?).
- **Presentation**: Interactive scatter. Optional time-lag cross-correlation plot.
- **Research value**: Understanding the solar-wind relationship helps predict ventilation potential from weather forecasts. If wind reliably follows solar heating, natural ventilation is self-regulating (more heat = more wind).

#### 5.23 Pre-Storm Signatures
- **Chart type**: Multi-panel time series aligned around rain event start times.
- **Description**: Composite plot showing the average behaviour of wind speed, wind direction variability, and solar radiation in the 2 hours before, during, and 2 hours after rain events. Created by aligning all detected rain events at t=0 (event start) and averaging.
- **Derived metrics**: Typical lead time between wind shift and rain onset, solar radiation drop before rain.
- **Presentation**: Three vertically stacked panels sharing a time axis (hours relative to event start).
- **Research value**: If there are reliable pre-storm signatures (e.g., wind picks up 30 minutes before rain), building operators can use them as cues to close windows proactively.

#### 5.24 Ventilation Window Analysis
- **Chart type**: Heatmap, x-axis = hour of day, y-axis = date, colour = ventilation condition.
- **Description**: For each hour of each day, classify the ventilation condition as: (a) Effective (wind above threshold, no rain), (b) Marginal (some wind but light rain, or calm), (c) Closed (heavy rain or driving rain above threshold). Presented as a calendar heatmap showing at a glance when natural ventilation is viable.
- **Derived metrics**: Overall ventilation window percentage, by hour, by day of week, by season.
- **Presentation**: Heatmap with green/yellow/red colour scheme. Summary statistics in sidebar.
- **Research value**: This is the synthesis chart. It combines all three weather variables into a single actionable metric: "can windows be open right now?" This directly serves the project's core research question about natural ventilation effectiveness.

---

## 6. Dashboard Layout

### 6.1 Overall Structure

Follows the existing project's layout:
- **Header bar**: ARC logo, title ("ARC Tanzania - Weather Station"), language toggle (EN/SW).
- **Sidebar** (300px, left, collapsible on mobile): Control panel with dropdowns, checkboxes, info tooltips, and stats panels.
- **Chart area**: Time bar (date range controls, chart type selector, download button) + full-width chart.

### 6.2 Chart Type Selector

A `<select>` dropdown in the time bar with the following options, grouped by module:

```
-- Wind --
Wind Rose
Wind Speed (Time Series)
Diurnal Wind Pattern
Wind Speed Distribution
Gust Factor
Calm Periods
Ventilation Availability

-- Solar --
Solar Radiation (Time Series)
Daily Insolation
Diurnal Solar Pattern
Solar Distribution
Clearness Index
Peak Solar Hours

-- Precipitation --
Cumulative Rainfall
Daily Rainfall
Rainfall Intensity
Diurnal Rainfall Pattern
Dry Spells
Rain Events

-- Combined --
Driving Rain Index
Wind-Rain Coincidence
Solar-Wind Correlation
Pre-Storm Signatures
Ventilation Windows
```

### 6.3 Sidebar Controls

The sidebar content changes based on the selected chart type (matching the existing project's pattern where line/histogram/comfort controls show/hide).

**Always visible:**
- **Date Range**: Start/end date pickers + hierarchical period selector (Year > Season > Month > Week > Day). Tanzanian seasons: Kiangazi (Jan-Feb), Masika (Mar-May), Kiangazi (Jun-Oct), Vuli (Nov-Dec).
- **Data freshness**: Footer showing when the Omnisense data was last updated, with stale-data warning if older than 2 days.

**Wind charts:**
- Wind speed unit toggle: km/h / m/s / knots.
- Calm threshold slider (for ventilation availability): 0.5 to 10 km/h, default 3.5 km/h (~1 m/s).
- Speed bin configuration (for wind rose): number of bins, max speed.
- Direction resolution: 8 or 16 compass points.

**Solar charts:**
- Latitude input (pre-filled: -7.065) for clear-sky calculations.
- Toggle: show/hide clear-sky reference envelope.

**Precipitation charts:**
- Intensity category thresholds (pre-filled with WMO defaults).
- Rain event gap tolerance (default 15 min).

**Cross-variable charts:**
- Building orientation input (degrees from North, default 0).
- Driving rain threshold.
- Ventilation wind speed threshold (shared with wind module).

**Stats panels** (context-dependent, shown below the controls):
- Summary statistics for the selected chart and date range.
- Styled consistently with the existing project's `#comfort-stats` and `#hist-stats-box` panels (light green/blue background, rounded border).

### 6.4 Info Tooltips

Every control, chart type label, and derived metric has an info tooltip using the existing project's pattern:
- Small circular "i" icon (`.info-i` class) next to the label.
- On hover, a fixed-position tooltip (`.info-tip-fixed` class) appears with explanatory text.
- **No em dashes in tooltip text.** Use commas, semicolons, or separate sentences instead.
- Tooltip text should be written for a building science audience: concise, technical, but accessible.

### 6.5 Time Bar

Matches existing project:
- Left: hierarchical period dropdown (auto-recommended based on data range).
- Centre: chart title.
- Right: date pickers + download PNG button.

### 6.6 Bilingual Support

English and Kiswahili, matching the existing project's `data-i18n` attribute pattern and `setLanguage()` function. Translation strings embedded in the JavaScript.

---

## 7. Technical Requirements

### 7.1 Conventions (carried over from existing project)

- **Timezone**: All timestamps in East African Time (EAT, UTC+3). Use `toEATString()` JS helper for Plotly.
- **No em dashes**: Use commas or semicolons in all text, tooltips, and labels.
- **Info tooltips**: Every feature and control must have a hover "i" icon with explanatory text.
- **Date range selection**: Hierarchical period picker with auto-recommendation.
- **PNG export**: Include source attribution ("ARC Tanzania Weather Station"), date range, and generation timestamp.
- **Zoom preservation**: User zoom state preserved across setting changes; resets on chart type switch.
- **Responsive**: Sidebar collapses to slide-out on mobile (< 680px).
- **Font**: Ubuntu (Google Fonts), matching existing project.
- **Generated output**: `index.html` is always generated by `build.py`. Never edit directly.
- **Commit messages**: Professional and specific. Describe what changed and why.

### 7.2 Dependencies

**Python (build time):**
- `pandas` - data processing
- `pytz` - timezone handling
- Standard library: `json`, `math`, `pathlib`, `glob`, `re`, `argparse`, `base64`, `struct`

**JavaScript (client-side):**
- Plotly.js 2.35.2+ (CDN)
- No other external libraries

### 7.3 Data Embedding

Following the existing project's pattern:
- `build.py` processes all data into a JSON structure.
- The JSON is embedded directly into `index.html` via string replacement (`__DATA__` placeholder in the HTML template).
- All chart rendering happens client-side in JavaScript using Plotly.

### 7.4 Build Command

```bash
python build.py          # Standard build: reads latest omnisense CSV, generates index.html
python build.py --csv path/to/file.csv   # Optional: specify a specific CSV file
```

No `--auto` mode needed (single data source).

---

## 8. Implementation Notes

### 8.1 Wind Rose in Plotly

Plotly supports polar bar charts via `Barpolar` trace type. Key considerations:
- Use `theta` for direction (degrees), `r` for frequency, and stack multiple traces for speed bins.
- Set `bargap=0` for a true wind rose appearance.
- Handle calm readings separately (they have no direction). Display calm percentage as a text annotation in the centre.
- The `angularaxis` should have 0 at North, going clockwise (`direction: "clockwise"`, `rotation: 90`).

### 8.2 Clear-Sky Model for Solar

For the clearness index (Kt) calculation, use a simple extraterrestrial radiation model:
1. Calculate solar declination for the day of year.
2. Calculate hour angle for the site longitude.
3. Compute extraterrestrial irradiance on a horizontal surface using the solar constant (1361 W/m2), corrected for Earth-Sun distance.
4. Integrate over the day to get daily extraterrestrial radiation (H0).
5. Kt = H_measured / H0.

This is a standard calculation (e.g., Duffie & Beckman, Chapter 2). No external API needed.

### 8.3 Precipitation Event Detection

Algorithm:
1. Identify all readings where `rate_percipitation_mm_h > 0`.
2. Group consecutive readings into events, allowing gaps of up to 15 minutes (3 readings at 5-min intervals).
3. For each event, record: start time, end time, duration, total rainfall (from cumulative column), peak rate, mean rate.
4. Merge the event's wind direction data to find prevailing direction during rain.

### 8.4 Cumulative Precipitation Reset Correction

The `total_percipitation_mm` column is a running total that occasionally resets. Correction algorithm:
1. Compute the difference between consecutive readings.
2. Where the difference is negative (reset detected), set the increment to 0 (or use the `rate_percipitation_mm_h` column to estimate the actual increment).
3. Compute a corrected cumulative sum from the non-negative increments.
4. One reset has been observed in the data: at 2026-02-21 03:04 the value drops from 64 to 18.

### 8.5 Peak Wind Spike Filtering

Some `peak_wind_kph` values are physically implausible (e.g., 431, 611, 803 km/h). These appear to be sensor glitches. Filter:
- Any `peak_wind_kph > 150` should be replaced with NaN.
- This threshold is conservative (Category 2 hurricane). Coastal Tanzania rarely exceeds 80 km/h even in severe storms.
- The filtering should be noted in the dashboard's data quality tooltip.

### 8.6 Driving Rain Index Calculation

Standard method (ISO 15927-3 simplified):
- For each 5-minute reading with both wind and rain: `DRI = v * r^(8/9) * cos(D - theta)`
  where `v` = wind speed (m/s), `r` = rain rate (mm/h), `D` = wind direction, `theta` = facade normal.
- Sum DRI over time for each facade orientation to get directional exposure.
- The building orientation control lets users set the actual facade normals.

### 8.7 Ventilation Window Classification

For each hour, classify based on:
- **Effective**: mean wind speed >= threshold AND no rain (rate = 0).
- **Marginal**: wind speed > 0 but < threshold, OR light rain (rate < 2.5 mm/h) with adequate wind.
- **Closed**: rain rate >= 2.5 mm/h, OR driving rain index above threshold.

These thresholds should be user-adjustable via sidebar controls.

### 8.8 Weibull Distribution Fit

For the wind speed histogram:
- Use the maximum likelihood method to fit Weibull shape (k) and scale (c) parameters.
- Exclude calm readings (speed = 0) from the fit, as Weibull does not model the zero spike.
- Report the calm percentage separately and the Weibull parameters for non-zero speeds.
- Overlay the fitted PDF on the histogram.

Implementation: the Weibull fit can be computed in Python during build (using `scipy.stats.weibull_min.fit` if scipy is available, or a simple iterative method with just numpy/pandas) and passed to the JS as parameters.

### 8.9 Module Integration Pattern

Each module in `modules/` should expose a single entry-point function:

```python
def process(df: pd.DataFrame) -> dict:
    """
    Args:
        df: Full weather station DataFrame (already parsed and cleaned).
    Returns:
        Dict with keys:
        - "charts": list of chart config dicts (each has "id", "title", "data", "layout")
        - "stats": dict of summary statistics
        - "sidebar_html": str of additional sidebar controls HTML (optional)
    """
```

`build.py` calls each module's `process()` function, collects all chart configs into the main JSON blob, and assembles the sidebar HTML from the module contributions.

---

## 9. Summary of Derived Metrics

| Metric | Module | Formula/Method | Research Use |
|---|---|---|---|
| Calm percentage | Wind | count(avg=0) / total_count * 100 | Ventilation reliability |
| Prevailing direction | Wind | Mode of 16-point compass bins | Window orientation |
| Gust factor | Wind | peak / avg (per reading, where avg > 0) | Turbulence, occupant comfort |
| Ventilation availability | Wind | Hours with speed >= threshold / total hours | Building performance KPI |
| Weibull k and c | Wind | MLE fit to non-zero speeds | Wind regime characterisation |
| Daily insolation | Solar | Integral of W/m2 over day, converted to kWh/m2 | Solar heat gain input |
| Clearness index (Kt) | Solar | Measured / extraterrestrial daily radiation | Sky condition classification |
| Peak solar hours | Solar | Daily insolation / 1 kW/m2 | Solar energy, heat gain proxy |
| Daily rainfall | Precip | End-of-day minus start-of-day cumulative (corrected) | Rainfall pattern |
| Rain event stats | Precip | Event detection algorithm (Section 8.3) | Storm characterisation |
| Dry spell duration | Precip | Consecutive readings with rate = 0 | Window-open potential |
| Driving rain index | Cross | v * r^(8/9) * cos(D - theta) | Facade moisture exposure |
| Ventilation window % | Cross | Classification algorithm (Section 8.7) | Core building performance metric |
