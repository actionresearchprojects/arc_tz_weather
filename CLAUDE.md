# omnisense_w_p_s

Omnisense weather station dashboard (wind, pressure, solar) — static HTML build.

## Primary files
- `build.py` — run to regenerate `index.html` from data
- `index.html` — output served by GitHub Pages

## Instructions for Claude
Whenever changes are made to `build.py` or `index.html`, append a brief entry to the Changelog below. Each entry heading must include the date and time to the second in CST (Taiwan/China Standard Time, UTC+8) — always run `date` first to get the real time, e.g. `### 2026-02-27 14:32:05 CST`.

## To update data
1. Add/replace data files in `data/`
2. Run: `python build.py`
3. `git add index.html && git commit -m "update data" && git push`

## Changelog

### 2026-02-27 13:33:53 CST
- Created `build.py`: reads `omnisense_*.csv` from `data/` (ignoring `data/legacy/`), filters to sensor `30B40014` (Sun/Wind/Rain weather station gateway), parses wind/solar/rain columns. Dashboard HTML output pending spec.
