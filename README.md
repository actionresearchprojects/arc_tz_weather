# ARC Tanzania Weather Station Dashboard

A self-contained, static HTML dashboard for wind, solar radiation and precipitation
data from the Omnisense weather station at the ARC ecovillage near Mkuranga, Tanzania.

## What This Project Does

The ecovillage site has a weather station (Omnisense sensor `30B40014`) recording wind
speed, wind direction, solar radiation and rainfall at five-minute intervals. This
project turns that raw record into a browsable dashboard of charts covering the site's
wind, solar and rainfall climate, with a particular emphasis on what the data means for
naturally ventilated buildings.

It is a sibling of the temperature and humidity dashboard and shares its architecture:
a single build script processes the data and writes one self-contained `index.html`.

## Data Source

Everything on this dashboard comes from one sensor. There is no external weather data,
no reanalysis product and no forecast; where a figure is derived rather than measured,
the dashboard says so.

| Column | Unit | Description |
|---|---|---|
| `avg_wind_speed_kph` | km/h | Five-minute average wind speed |
| `peak_wind_kph` | km/h | Peak gust within the five-minute interval |
| `wind_direction` | degrees | Compass bearing, 0 = North |
| `solar_radiation` | W/m2 | Global horizontal irradiance |
| `total_percipitation_mm` | mm | Cumulative rainfall, with periodic resets |
| `rate_percipitation_mm_h` | mm/h | Instantaneous rainfall rate |
| `battery_voltage` | V | Sensor battery voltage |

## How It Works

### Build Process

`build.py` loads the CSV, passes the cleaned data through four analysis modules, and
writes a single `index.html` with all chart data embedded as JSON. The output needs
nothing at runtime except Plotly from a CDN.

```bash
python build.py                      # standard build
python build.py --csv path/to/file   # build from a specific CSV
```

`index.html` is generated output and is overwritten on every build. All changes to the
dashboard are made in `build.py`.

### Automation

A GitHub Action runs daily at 05:00 UTC (08:00 EAT), one hour after the temperature and
humidity project fetches the sensor CSV. It copies the latest CSV from that repository
by sparse checkout, rebuilds the dashboard, and if anything changed, commits and pushes,
then triggers a sync to the main site.

## Dashboard Features

The dashboard is organised into four modules.

**Wind.** Wind rose, speed time series, diurnal pattern, speed distribution, gust factor
analysis, calm period analysis and ventilation availability.

**Solar.** Radiation time series, daily insolation profile, diurnal pattern, distribution
histogram, clearness index and peak solar hours. The clearness index compares measured
radiation against clear-sky radiation computed from latitude and day of year, so no
external dataset is required.

**Precipitation.** Cumulative rainfall, daily rainfall, intensity distribution, diurnal
pattern, dry spell analysis and a rain event summary.

**Cross-variable.** Driving rain index, wind and rain coincidence, solar and wind
correlation, pre-storm signatures and ventilation window analysis. These combine two or
more channels to answer questions that no single channel can.

### Wind speed categories

Wind speeds can be classified on four scales: Beaufort, Lawson, Davenport, or a set of
ARC-calibrated categories derived from this station's own record. The standard scales
were each calibrated for a different purpose and place, so none of them describes a
humid tropical coastal site especially well. The ARC categories are computed from the
site's own distribution instead. See `ARC_WIND_CATEGORIES.md`.

### Indoor ventilation calculator

Outdoor wind speed is not what building occupants feel. The calculator estimates likely
indoor air speeds from the measured outdoor record given a room's dimensions, window
areas and mosquito mesh, and overlays the result on the wind speed categories chart.
Mesh in particular can reduce indoor air movement substantially, which matters directly
for thermal comfort in a naturally ventilated building. See `INDOOR_VENTILATION_CALC.md`.

## Data Quality

The station uses a cup anemometer with a reed switch counter, which produces two
characteristic artefacts: switch bounce on the peak gust channel, and isolated spikes on
the average channel. Three filters are applied, uniformly across every project that uses
this sensor:

- **Average spike filter**: values above three times the rolling median, ceiling 60 km/h.
- **Peak bounce filter**: peak to average ratio above 8, ceiling 100 km/h.
- **Physical impossibility filter**: where average exceeds peak, both channels are
  discarded rather than just the peak.

Flagged values become gaps rather than being replaced by estimates. Cumulative
precipitation resets are detected and corrected automatically. The reasoning behind each
filter is set out in `WIND_QC.md`.

## Project Structure

```
arc_tz_weather/
  build.py                    Orchestrator: loads data, calls modules, writes index.html
  modules/common.py           CSV parsing, time helpers, palettes, quality filters
  modules/wind.py             Wind analyses
  modules/solar.py            Solar analyses
  modules/precipitation.py    Rainfall analyses
  modules/cross_variable.py   Analyses combining two or more channels
  fetch_omnisense.py          Copied from arc_tz_temp_humid, not modified here
  index.html                  Generated output, never edited directly
  data/omnisense/             Sensor CSVs, copied from the sibling repository
```

## How Data Flows

```
Omnisense sensor 30B40014
    v
omnisense.com
    v   arc_tz_temp_humid daily workflow fetches the CSV
arc_tz_temp_humid/data/omnisense/
    v   arc_tz_weather daily workflow copies it across
arc_tz_weather/data/omnisense/
    v   build.py processes and generates
index.html
    v   push triggers a main site sync
actionresearchprojects.github.io
```

## Key Technical Details

- **Timezone**: all timestamps are East African Time (UTC+3). Charts use a helper that
  prevents the plotting library from converting to the viewer's local time.
- **Single source**: only the Omnisense CSV. No TinyTag, no Open-Meteo, no forecast data.
- **Self-contained output**: chart data is embedded in the HTML rather than fetched.

## Technologies Used

Python with pandas and numpy for processing; Plotly for charts; GitHub Actions for the
daily rebuild; GitHub Pages for hosting.

## Documentation

- `ARC_WIND_CATEGORIES.md`: how the site-calibrated wind categories are derived, and why
  the standard scales do not fit this site.
- `WIND_QC.md`: the anemometer artefacts and the filters that correct them.
- `INDOOR_VENTILATION_CALC.md`: how indoor air speed is estimated from outdoor wind.
- `dataflow.md`: the automated pipeline from sensor to published dashboard.
- `WEATHER_SPEC.md`: the full build specification.
- `CLAUDE.md`: project conventions.
