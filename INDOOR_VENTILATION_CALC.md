# Indoor Ventilation Calculator: Technical Reference

## Purpose

The indoor ventilation calculator estimates likely indoor air speeds from the measured outdoor wind speed dataset, using the physical properties of a specific room and its windows. It overlays the resulting indoor speed distribution on the Wind Speed Categories chart, allowing direct comparison between the outdoor wind climate and the indoor air movement that occupants of a building can expect.

The primary application is assessing natural cross-ventilation potential in low-rise dwellings in tropical climates, where adequate indoor air speed is critical for thermal comfort and is frequently compromised by mosquito mesh on windows.

---

## Background: Why Indoor Air Speed Matters

In hot, humid tropical climates without mechanical cooling, indoor air movement is one of the few reliable routes to thermal comfort. Moving air increases the rate of convective and evaporative heat loss from the skin, lowering the perceived temperature even when the air itself is warm.

The relevant thresholds for occupant perception and comfort are approximately:

| Indoor air speed | Effect on occupant |
|---|---|
| < 0.1 m/s | Imperceptible; no comfort benefit |
| 0.1 to 0.2 m/s | Barely perceptible; marginally beneficial |
| 0.2 to 0.5 m/s | Noticeable; meaningful comfort benefit in warm conditions |
| 0.5 to 1.0 m/s | Effective; significant cooling effect; typical of open windows in moderate wind |
| > 1.0 m/s | Strong; may feel draughty for sedentary occupants |

These thresholds are drawn from ASHRAE Standard 55 (Thermal Environmental Conditions for Human Occupancy) and the work of Fanger and Christensen on draught ratings, and are widely used in tropical building design guidance.

At the ARC Tanzania site, the outdoor wind climate is exceptionally calm: the median outdoor speed is around 1.2 km/h and the 99th percentile is around 7.6 km/h. Even at the best case (no mesh, direct wind, exposed shielding), the mean indoor speed for a typical room rarely exceeds 0.05 to 0.08 m/s. This is below the perceptible threshold for most occupants, which is a significant finding for the site: natural cross-ventilation alone is insufficient for meaningful thermal comfort benefit on most days.

---

## The Physical Model: Cross-Ventilation

The calculator models simple cross-ventilation: outdoor wind creates a pressure difference between the windward and leeward faces of a building; air flows in through the inlet window, crosses the room, and exits through the outlet window.

The model has four steps:

1. Convert outdoor wind speed to a wind pressure difference across the building.
2. Convert that pressure difference to a volumetric airflow rate through the windows.
3. Convert that airflow rate to a mean indoor air speed.
4. Reduce by the mesh factor (if mosquito mesh is present).

Each step is described in full below, with the physical reasoning behind every parameter choice.

---

## The Calculation Chain

### Step 1: Wind pressure difference across the building

```
ΔP = ΔCp × ½ρv²
```

Where:

- `ΔP` = wind-induced pressure difference between the windward (inlet) and leeward (outlet) faces of the building, in Pascals (Pa)
- `ΔCp` = dimensionless pressure coefficient difference (see table below)
- `ρ` = air density = **1.2 kg/m³**
- `v` = outdoor wind speed in m/s (converted from the station's km/h measurement by dividing by 3.6)

**Physical meaning:** Moving air has kinetic energy proportional to ½ρv². When wind hits a building, it decelerates and converts some of that kinetic energy into pressure on the windward face (positive pressure). On the leeward face, the flow separates and creates a suction zone (negative pressure). The net pressure difference ΔP drives air through any openings in the envelope.

**Why ρ = 1.2 kg/m³:** This is the standard value for air at sea level and around 20°C. For the ARC Tanzania site (approximately 700 m altitude, mean temperature around 26°C), the true density is closer to 1.13 kg/m³. The difference is small relative to other uncertainties in the model, so the standard value is retained for simplicity and comparability with published tables (which are calibrated to this value).

**Why ΔCp rather than Cp:** Individual face pressure coefficients Cp describe the pressure on one face relative to free-stream dynamic pressure. To drive cross-ventilation, what matters is the difference between the inlet face Cp and the outlet face Cp. A high positive Cp on the windward face and a high negative Cp on the leeward face both contribute to a large ΔCp and thus a large driving pressure. See the Cp table section below for specific values and sources.

---

### Step 2: Airflow rate through the window opening

```
Q = Cd × Ao × √(2ΔP/ρ)
```

Where:

- `Q` = volumetric airflow rate through the room, in m³/s
- `Cd` = discharge coefficient = **0.6**
- `Ao` = effective opening area (m²)

**Physical meaning:** This is derived from Bernoulli's equation applied to flow through an orifice. If air flows through a hole of area Ao under a pressure difference ΔP, the theoretical flow velocity is √(2ΔP/ρ). Multiplying by the area gives the theoretical flow rate. The discharge coefficient Cd accounts for the fact that real flow through a sharp-edged rectangular opening is less than the theoretical maximum.

**Why Cd = 0.6:** When air flows through a sharp-edged rectangular opening (a window frame), the flow contracts as it passes through (the "vena contracta" effect) and there are viscous losses at the edges. Measured values of Cd for rectangular openings in buildings consistently fall in the range 0.57 to 0.65. The value 0.6 is the standard in building ventilation engineering (used in EN 15242, AIVC TN44, and ASHRAE Fundamentals) and is appropriate for a window without any internal obstruction. The mosquito mesh, if present, is handled separately in Step 4 rather than by reducing Cd, because the mesh effect is a multiplier on the final indoor speed and is derived from empirical measurements rather than from orifice theory.

**Effective area Ao:** The current implementation uses `Ao = min(A_inlet, A_outlet)`. This is a conservative simplification: the smaller of the two openings is the primary bottleneck. The physically correct formula for two openings in series is `1/Aeff² = 1/Ainlet² + 1/Aoutlet²`, which gives a somewhat lower effective area when the two openings differ significantly in size. For example, with a 2 m² inlet and a 1.5 m² outlet, the min formula gives 1.5 m² while the series formula gives 1.17 m². The series correction is noted as a planned improvement.

---

### Step 3: Mean indoor air speed

```
v_indoor = Q / (√A_floor × h_room)
```

Where:

- `v_indoor` = mean indoor air speed through the room cross-section, in m/s
- `A_floor` = room floor area, in m²
- `h_room` = room ceiling height, in m

**Physical meaning:** The airflow rate Q (m³/s) passes through the cross-sectional area of the room perpendicular to the direction of flow. Dividing a flow rate (m³/s) by an area (m²) gives a speed (m/s). This is correct dimensional analysis.

**Why √A_floor × h_room for the cross-section:** The room is assumed to be square in plan (width = depth = √A_floor). The face of the room perpendicular to the dominant airflow direction then has dimensions √A_floor (width) by h_room (height), giving a cross-section area of √A_floor × h_room m².

For example, a 25 m² room is treated as 5 m × 5 m. With a ceiling height of 3.0 m, the cross-section area is 5 × 3.0 = 15 m².

**Interpretation:** This is a cross-section mean speed, not a local speed. In reality, air entering through a window does not fill the room uniformly. Speeds near the window are much higher; speeds in corners far from both windows may be near zero. The mean is a useful single-number summary for comparing sites and configurations, but occupant experience will vary strongly with position in the room.

---

### Step 4: Mesh reduction (if mosquito mesh is present)

Mosquito mesh substantially reduces airflow through openings. The reduction is applied as a multiplier to `v_indoor`:

| Bound | Multiplier | Interpretation |
|---|---|---|
| Optimistic (upper bound) | 0.50 | 50% of the unobstructed indoor speed; corresponds to the more permeable end of measured mesh performance |
| Conservative (lower bound) | 0.35 | 35% of the unobstructed indoor speed; 65% reduction; corresponds to the less permeable end |

**Why these values:** The multipliers are derived from field measurements reported in von Seidlein et al. (2012), who measured indoor air speeds with and without insecticide-treated mosquito mesh screens in Tanzanian housing. They found that mesh screens reduced indoor air velocity by 50 to 65% relative to the unscreened condition, corresponding to transmission factors of 35 to 50%. The midpoint (42.5%) is approximately the geometric mean.

The two-bound approach reflects genuine variability: loosely fitted mesh with a coarser weave approaches 50%; tightly fitted, fine-aperture mesh approaches 35%. Worn or damaged mesh may transmit more air than either bound.

If no mesh is present, a single line is shown with no reduction applied (multiplier = 1.0).

---

## Source of Cp Values: AIVC Technical Note 44

The pressure coefficient difference ΔCp is the most influential single parameter in the calculation. Values are taken from Tables 3.5 (i)-(iii) of:

> Orme, M., Liddament, M.W., and Wilson, A. (1998). *Numerical Data for Air Infiltration and Natural Ventilation Calculations.* Air Infiltration and Ventilation Centre, Technical Note AIVC 44. Coventry, UK. (Originally published 1994; reprinted and updated 1998.)

TN44 provides single-face wind pressure coefficients for low-rise buildings (up to 3 storeys) under three shielding conditions, derived from wind tunnel measurements by Wiren (1985) and Bowen (1976). These are among the most widely cited empirical Cp tables in building ventilation engineering.

The implementation uses a 3x3 lookup table of ΔCp values (three shielding conditions x three wind direction categories):

| Shielding | Direct (0°) | Diagonal (45°) | Side-on (90°) |
|---|---|---|---|
| **Exposed** (open countryside, no nearby obstructions) | 1.20 | 0.75 | 0.30 |
| **Suburban** (semi-sheltered; surrounding obstructions roughly half the building height) | 0.70 | 0.45 | 0.10 |
| **Urban** (sheltered; surrounding obstructions roughly equal to building height) | 0.45 | 0.35 | 0.05 |

**Derivation from TN44:**

*Direct (0° wind angle):* Windward face (face 1 at 0°) minus leeward face (face 3 at 0°).
- Exposed (Table 3.5 i): +0.70 minus (−0.50) = **1.20**
- Suburban (Table 3.5 ii): +0.40 minus (−0.30) = **0.70**
- Urban (Table 3.5 iii): +0.20 minus (−0.25) = **0.45**

*Diagonal (45° wind angle):* Windward face (face 1 at 45°) minus the adjacent side face (face 4 at 45°). With wind at 45°, the outlet is the side face most nearly leeward.
- Exposed: +0.35 minus (−0.40) = **0.75**
- Suburban: +0.10 minus (−0.35) = **0.45**
- Urban: +0.05 minus (−0.30) = **0.35**

*Side-on (90° to the inlet window):* Wind blows parallel to the inlet window plane. Pressure differences arise from the separation of flow around the building corners, creating a small but non-zero ΔP between the two side faces.
- Exposed: −0.20 minus (−0.50) = **0.30**
- Suburban: −0.20 minus (−0.30) = **0.10**
- Urban: −0.25 minus (−0.25) = approximately 0 (clamped to **0.05** to avoid numerical collapse)

The TN44 tables assume a 1:1 building length-to-width ratio. All Cp values are referenced to the wind speed at building height (not at the standard 10 m meteorological measurement height).

---

## Wind Speed Categories

The outdoor wind speed data is classified into categories for display on the chart. The calculator supports four category systems:

### Beaufort Scale

The Beaufort scale was originally developed for maritime observations by Sir Francis Beaufort in 1805 and extended to land use in the 20th century. It describes wind conditions by their observable effects, with each level corresponding to a range of wind speeds at 10 m height.

| Force | Description | km/h range | Typical land effect |
|---|---|---|---|
| 0 | Calm | 0 to 1 | Smoke rises vertically |
| 1 | Light air | 1 to 5 | Smoke drift shows direction; wind vanes unaffected |
| 2 | Light breeze | 6 to 11 | Wind felt on face; leaves rustle |
| 3 | Gentle breeze | 12 to 19 | Leaves and small twigs in constant motion |
| 4 | Moderate breeze | 20 to 28 | Raises dust; small branches move |
| 5 | Fresh breeze | 29 to 38 | Small trees sway; wavelets on inland water |
| 6 | Strong breeze | 39 to 49 | Large branches in motion; umbrellas difficult to use |
| 7 | Near gale | 50 to 61 | Whole trees in motion; walking against wind difficult |
| 8 | Gale | 62 to 74 | Breaks twigs off trees; impedes walking |
| 9 | Strong gale | 75 to 88 | Slight structural damage (chimney pots, slates) |
| 10 | Storm | 89 to 102 | Trees uprooted; considerable structural damage |
| 11 | Violent storm | 103 to 117 | Very rarely experienced; widespread damage |
| 12 | Hurricane | ≥ 118 | Devastation |

At the ARC Tanzania site, almost all observations fall in Beaufort 0 to 2 (calm to light breeze), with occasional Force 3 readings and very rare Force 4 or above.

### Lawson Criteria

The Lawson criteria (Lawson, 1978; revised by LDDC/BRE) assess pedestrian wind comfort for urban outdoor spaces. They are widely used in wind microclimate assessments for planning applications in the UK and internationally. Each criterion is named for the activity it describes, with a threshold frequency: wind conditions are "acceptable" if speeds above the threshold occur for less than the stated percentage of the time.

The Lawson L1 to L5 thresholds used in this chart are:

| Category | Description | Threshold |
|---|---|---|
| L1 Sitting | Outdoor seating comfort | 1.8 m/s (6.5 km/h) exceeded < 5% of time |
| L2 Standing | Outdoor standing comfort | 3.6 m/s (13 km/h) exceeded < 5% of time |
| L3 Walking | Pedestrian walking comfort | 5.3 m/s (19 km/h) exceeded < 5% of time |
| L4 Unpleasant | Uncomfortable; business disrupted | 7.6 m/s (27 km/h) exceeded < 2% of time |
| L5 Dangerous | Unsafe for pedestrians | 15.3 m/s (55 km/h) exceeded < 0.1% of time |

These criteria are for outdoor pedestrian comfort, not indoor ventilation, but they provide a useful reference frame for interpreting wind speed distributions relative to human activity.

### ARC Categories

The ARC (Action Research Centre) categories are a custom set of wind speed bands developed for this project, calibrated to the specific wind climate of the ARC Tanzania site. They are designed to spread the observed data distribution more evenly across the categories than the Beaufort scale does at this calm site, where Beaufort 0 and 1 together account for the majority of observations.

### Custom Thresholds

A custom category system allows the user to define their own threshold values in km/h, creating bands tailored to any specific analysis requirement.

---

## Interpreting the Overlay

The indoor ventilation overlay shows, for each outdoor wind speed category, the mean indoor air speed (in m/s) calculated from all the actual measured outdoor wind readings that fall within that category. A vertical line or pair of lines is drawn at the horizontal position corresponding to that mean indoor speed.

When mosquito mesh is enabled, two bounds are shown:
- A solid line for the optimistic bound (50% transmission)
- A dotted line for the conservative bound (35% transmission)
- A hatched green fill between the two bounds

When no mesh is selected, a single line is shown.

The secondary x-axis at the top of the chart shows indoor air speed in m/s. This axis is independent of the outdoor category axis; its scale is set automatically to accommodate the calculated indoor speeds.

**A note on the expected magnitudes:** At the ARC Tanzania site, with default settings (suburban shielding, direct wind, 2 m² inlet and outlet, 25 m² floor, 3.2 m ceiling, mosquito mesh), typical calculated indoor speeds are in the range 0.01 to 0.05 m/s. These values are well below the 0.1 m/s threshold for perceptible air movement. This is not a calculation error; it reflects the physical reality of a very calm outdoor wind climate combined with the substantial attenuation of mosquito mesh. The calculator correctly predicts that natural cross-ventilation at this site provides little or no thermal comfort benefit on most days.

---

## Worked Example

**Settings:** 2 m² inlet window, 1.5 m² outlet window, 25 m² floor area, 3.2 m ceiling height, suburban shielding, direct wind (0°), mosquito mesh present.

**Outdoor wind speed:** 10 km/h = 2.78 m/s

**Step 1:** ΔCp (suburban, direct) = 0.70. ΔP = 0.70 × 0.5 × 1.2 × 2.78² = 0.70 × 4.63 = **3.24 Pa**

**Step 2:** Ao = min(2.0, 1.5) = 1.5 m². Q = 0.6 × 1.5 × √(2 × 3.24 / 1.2) = 0.9 × √5.40 = 0.9 × 2.32 = **2.09 m³/s**

**Step 3:** Cross-section = √25 × 3.2 = 5 × 3.2 = 16 m². v_indoor = 2.09 / 16 = **0.131 m/s**

**Step 4 (mesh):**
- Optimistic: 0.131 × 0.50 = **0.065 m/s**
- Conservative: 0.131 × 0.35 = **0.046 m/s**

At 10 km/h outdoor wind (a moderate reading for this site), a room with these characteristics would experience roughly 0.046 to 0.065 m/s of mean indoor air speed through mesh -- barely at the lower edge of perceptibility.

---

## Known Limitations

**1. Single-zone, single-opening model.** The calculation assumes simple cross-ventilation: air enters through one opening and exits through another in steady state. Real buildings have multiple openings, corridors, internal partitions, and complex flow paths that this model cannot represent.

**2. Series opening correction not yet implemented.** The current implementation uses `Ao = min(A_inlet, A_outlet)` as the effective area. The correct formula for two openings in series is `1/Aeff² = 1/Ainlet² + 1/Aoutlet²`, which gives a lower effective area when the two openings differ in size. This will be added in a subsequent update and will generally reduce estimated airflow slightly.

**3. Mean wind speed, not instantaneous.** The outdoor wind speed data is a 5-minute average. Real ventilation is driven by fluctuating, gusty wind; instantaneous indoor air speed varies considerably around the calculated mean. The model gives a time-averaged estimate only.

**4. No wind profile correction.** The calculation uses the measured wind speed at the 10 m station height directly, without adjusting for the difference between 10 m and the actual building height. For a single-storey building, this overestimates the wind at eave height by a factor that depends on terrain roughness (typically 10 to 20%). TN44 Section 3.2.1 provides correction procedures that are not implemented here.

**5. No thermal buoyancy.** The model is wind-driven only. In practice, temperature differences between inside and outside also drive airflow through the stack effect. In the daytime tropical climate of the ARC site, wind tends to dominate; but at night or in calm conditions, stack ventilation may contribute meaningfully. Including the stack effect would require temperature data at every timestep.

**6. Cp values are for standard low-rise geometry.** The TN44 tables assume a rectangular building up to 3 storeys with a 1:1 plan ratio. Buildings with very different proportions, complex plan shapes, or significant roof overhangs will have different Cp distributions.

**7. Square floor plan assumed.** The formula treats the room as square in plan. A rectangular room with a narrow face toward the inlet window would have a smaller cross-section and higher actual mean speed; one with a wide face would have a larger cross-section and lower speed.

**8. Mosquito mesh reduction is approximate.** The 0.35 to 0.50 range is derived from field measurements in East African housing, but mesh type, condition, installation quality, and opening aspect ratio all affect actual performance. The bounds should be treated as indicative, not precise.

**9. Wind direction not modelled.** The selected ΔCp is applied uniformly to all outdoor wind speed readings regardless of actual wind direction at each timestep. In reality, the ventilation effectiveness varies with wind direction relative to the window. The "wind direction to window" setting is a fixed orientation assumption applied across the entire dataset.

**10. Calculated value is a cross-section mean.** The indoor air speed is the mean across the room cross-section perpendicular to flow. Local speeds near the window can be several times higher; local speeds in corners and behind partitions may be near zero. Occupant experience depends strongly on position within the room.

---

## References

Orme, M., Liddament, M.W., and Wilson, A. (1998). *Numerical Data for Air Infiltration and Natural Ventilation Calculations.* Air Infiltration and Ventilation Centre, Technical Note AIVC 44. Coventry, UK.

Von Seidlein, L., Ikonomidis, K., Mshamu, S., Nkya, T.E., Mukaka, M., Pell, C., Lindsay, S.W., Deen, J.L., Knols, B.G.J., and Killeen, G.F. (2012). Affordable interventions to protect against malaria in African cities: a cost-effectiveness analysis. *Malaria Journal*, 11, 17.

ASHRAE (2017). *ASHRAE Standard 55-2017: Thermal Environmental Conditions for Human Occupancy.* American Society of Heating, Refrigerating and Air-Conditioning Engineers, Atlanta.

Lawson, T.V. (1978). The wind content of the built environment. *Journal of Wind Engineering and Industrial Aerodynamics*, 3(2), 93-105.

Wiren, B.G. (1985). *A wind tunnel study of wind velocities in passages between and through buildings.* Proceedings of the 4th Colloquium on Industrial Aerodynamics, Aachen.

Fanger, P.O. and Christensen, N.K. (1986). Perception of draught in ventilated spaces. *Ergonomics*, 29(2), 215-235.
