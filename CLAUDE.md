# arc_tz_weather: Project Conventions

Weather station dashboard for the ARC ecovillage near Mkuranga, Tanzania. Self-contained, static HTML dashboard for wind, solar radiation, and precipitation data from the Omnisense weather station (sensor ID `30B40014`).

## Primary Files
- `build.py`: Orchestrator script. Loads CSV, calls modules, generates `index.html`. Contains full HTML/CSS/JS template.
- `modules/common.py`: Shared utilities (CSV parsing, time helpers, colour palettes, data quality filters).
- `modules/wind.py`: Wind rose, time series, diurnal, distribution, gust factor, calm periods, ventilation availability.
- `modules/solar.py`: Solar radiation time series, daily insolation, diurnal, distribution, clearness index, peak solar hours.
- `modules/precipitation.py`: Cumulative rainfall, daily rainfall, intensity distribution, diurnal, dry spells, rain events.
- `modules/cross_variable.py`: Driving rain index, wind-rain coincidence, solar-wind correlation, pre-storm signatures, ventilation windows.
- `fetch_omnisense.py`: Copy from arc_tz_temp_humid. Not modified by this project.
- `index.html`: **GENERATED OUTPUT. Never edit directly.**

## NEVER EDIT index.html DIRECTLY
`index.html` is completely overwritten every time `build.py` runs. ALL HTML, CSS, and JavaScript changes must be made in `build.py`.

## Critical Technical Rules
- **Timezone**: Everything MUST use East African Time (EAT, UTC+3).
- **Graphing**: Use `toEATString(ms)` helper in JS to prevent Plotly from converting to browser-local time.
- **No em dashes**: Use commas or semicolons in all text, tooltips, and labels.
- **Data source**: Only the Omnisense CSV (sensor 30B40014). No TinyTag, no Open-Meteo.
- **Spike filter**: peak_wind_kph > 150 replaced with NaN.
- **Precipitation resets**: Cumulative total resets detected and corrected automatically.
- **Commit messages**: Professional and specific. Describe what changed and why.

## Data Pipeline
1. Omnisense CSV fetched daily by arc_tz_temp_humid's workflow.
2. This project's GitHub Action copies the latest CSV from the sibling repo.
3. `build.py` processes data through modules and generates `index.html`.

## Build Command
```bash
python build.py                      # Standard build
python build.py --csv path/to/file   # Specific CSV
```
