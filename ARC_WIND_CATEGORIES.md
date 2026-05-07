# ARC-calibrated Wind Speed Categories

## What it is

A site-specific wind speed classification system calibrated automatically from the ARC weather station's own data. Unlike Beaufort (calibrated against Atlantic sea state in 1805), Lawson (calibrated for pedestrians in UK urban canyons), or Davenport (temperate-climate wind engineering), the ARC-calibrated categories reflect what the wind actually does at Mkuranga.

## How it works

### Step 1: Separate calm

Wind readings at or below 0.1 m/s (0.36 km/h) are treated as calm. This threshold is fixed and non-negotiable: it corresponds to the sensor noise floor and is used consistently across all wind analysis in this dashboard. The **Calm** band is always 0-0.1 m/s.

### Step 2: Find the tail threshold

The 90th percentile (P90) of all non-calm speeds is computed. All readings above this value are grouped into a single open-ended **tail band** (e.g. "5.3+ km/h"). This prevents the sparse high-speed tail from distorting the width of the main bands.

### Step 3: Grid-search for the optimal number of equal-width bands

The algorithm tests N = 2 through 7 equal-width bands across the range from the calm threshold (0.1 m/s) to the tail threshold. For each candidate N, it counts how many readings fall in the least-populated band. It picks the **largest N** for which every band still contains at least 10% of the non-calm readings (minimum 100 readings). This delivers as much resolution as the data supports without creating nearly-empty bars.

### Step 4: Label with actual speed ranges

Bands are labelled by their speed ranges in the current display unit (e.g. "0.36-2.1 km/h"). Because the thresholds recalibrate whenever new data is loaded, fixed semantic names (Light, Moderate, etc.) would quickly become misleading.

### Peak gust mode

When Peak Gust is selected in the chart controls (without Average), the calibration uses peak gust speeds rather than 5-minute average speeds. Peak gust readings tend to be higher and more spread, so the band boundaries will differ.

## Example output (from current dataset)

| Band  | Range (km/h) | Range (m/s) |
|-------|-------------|-------------|
| Calm  | 0-0.36      | 0-0.1       |
| Band 2| 0.36-2.1    | 0.1-0.6     |
| Band 3| 2.1-3.9     | 0.6-1.1     |
| Band 4| 3.9-5.7     | 1.1-1.6     |
| Tail  | 5.7+        | 1.6+        |

*Exact boundaries update automatically each time a new dataset is loaded.*

## Show calibration view

Selecting "Show calibration" (visible when ARC-calibrated is chosen) replaces the bar chart with the raw speed distribution histogram, with the algorithm's decisions overlaid:

- **Gray shading**: the calm band (0-0.1 m/s)
- **Dotted dark line**: calm boundary at 0.1 m/s
- **Blue dashed lines**: equal-width band boundaries
- **Bold red dashed line**: P90 tail threshold

This makes it easy to see where the boundaries fall relative to the actual distribution of readings.

## Why this matters

Most published wind scales were designed for sailors navigating open oceans or engineers assessing rooftop wind loads in European cities. Neither context applies to a low-rise ecovillage on the Tanzanian coast where the dominant feature is a gentle diurnal sea breeze peaking at 4-6 km/h. Applying Beaufort to this site is like using a scale designed to measure earthquakes to classify vibrations from a passing bicycle: technically possible, but most of the interesting variation falls in the bottom two categories.

The ARC-calibrated system makes the full range of the site's actual wind behaviour visible by spreading readings evenly across all bands.

## Limitations

- Equal-width bands assume roughly uniform density across the non-calm range, which is only approximately true for a right-skewed Weibull-like wind distribution.
- The tail band is open-ended; rare extreme speeds are grouped with strong-but-common readings.
- With limited data, the P90 threshold and band widths may shift as more observations accumulate.
