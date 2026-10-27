# Efficiency reports

`analyse_efficiency.py` writes its output here by default. The files are
generated, but the current best report is worth committing so that the planner
has a stable efficiency model to use and changes over time are reviewable.

The committed JSON and CSVs were generated before schema version 2, so they do
not yet contain the `calibration` section or the corrected model columns.
`REVIEW.md` records the calibration derived from them. Re-run the tool on the
production machine to regenerate the files with the new columns.

## Generating a report

On the production machine (where `data/config/config.yaml` holds the InfluxDB
credentials):

```bash
PYTHONPATH=src .venv/bin/python -m evse_controller.tools.analyse_efficiency \
    --days 30 \
    --battery-capacity-kwh 59 \
    --output reports/efficiency
```

The report includes a `calibration` section and a corrected model table. The
vehicle active load is calibrated automatically; use `--active-load-kw` only
to override it (for example to check a different vehicle).

The configuration can also be supplied without touching the production file:

```bash
PYTHONPATH=src .venv/bin/python -m evse_controller.tools.analyse_efficiency \
    --config /path/to/config.yaml --days 30
```

or through environment variables / CLI flags:

```bash
INFLUXDB_URL=http://influx:8086 \
INFLUXDB_TOKEN=... \
INFLUXDB_ORG=... \
INFLUXDB_BUCKET=powerlog \
PYTHONPATH=src .venv/bin/python -m evse_controller.tools.analyse_efficiency --days 7
```

If no configuration can be found, `--print-config-template` prints the YAML
section the tool expects. The token is never written to the report.

For an offline run, export a day first and analyse the CSV (`.csv.gz` works):

```bash
python src/evse_controller/tools/export_influxdb.py 2026-09-01
PYTHONPATH=src .venv/bin/python -m evse_controller.tools.analyse_efficiency \
    --input-csv data/backup/influxdb/powerlog_20260901.csv.gz
```

## Output files

| File | Contents |
| --- | --- |
| `efficiency_analysis.json` | Complete machine-readable report: settings, summary, bins, fits, round-trip and model table |
| `windows.csv` | One row per accepted steady window (60 s by default), useful for plotting and spot checks |
| `by_current.csv` | Efficiency and loss binned by commanded current, per direction |
| `by_soc.csv` | Same, binned by reported SoC |
| `model_table.csv` | Flat current → charge/discharge efficiency, loss and round-trip table intended for the DP planner |
| `calibration_by_current.csv` | Calibration segments binned by current: implied dashboard-SoC capacity, DC power, SoC delta and duration |

## What the report means

- Efficiency is measured at the Wallbox boundary: AC input/output versus the
  charger's DC output. It includes the charger's own overhead but excludes
  cable, contactor and cell internal resistance.
- Windows are only used when the setpoint and direction are steady and the AC
  power is stable. Idle/standby data is summarised separately.
- `loss_fit.<direction>.power` is a weighted least-squares fit of
  `loss_kw = intercept + slope * P_dc_kw^2`. The slope's t-statistic indicates
  whether a power-dependent (I²R-like) term is significant or whether a fixed
  overhead plus a small constant is adequate.
- `soc_based` is a separate cross-check that uses the reported SoC change and
  an assumed usable capacity. It is intentionally not mixed with the AC↔DC
  result because 1% SoC quantisation and BMS taper/balancing make it much
  noisier.
- `calibration` estimates the **effective dashboard-SoC capacity** and the
  **vehicle active load** from long steady charge and discharge segments. It is
  an effective slope for planning, not a pack-capacity or degradation
  measurement: the Leaf dashboard SoC is nonlinear and keeps roughly 2–3 kWh in
  reserve at the bottom.

## Calibration

The AC↔DC efficiency in `by_current` and the raw model table excludes the
vehicle's own active load. Long segments let the tool solve for both the
effective capacity and that load:

```text
charge:    E_dc = C * |dSoC|/100 + L * t
discharge: E_dc = C * |dSoC|/100 - L * t
```

where `C` is kWh per 100% of reported dashboard SoC and `L` is the vehicle-side
load in kW. The default thresholds are conservative:

| Option | Default | Meaning |
| --- | ---: | --- |
| `--calibration-min-soc-delta` | 10 | Minimum dashboard SoC change per segment |
| `--calibration-min-duration-minutes` | 30 | Minimum segment duration |
| `--calibration-taper-soc` | 90 | Truncate charge segments here; `0` disables |
| `--active-load-kw` | calibrated | Override the vehicle load used for corrections |

The 90% taper cutoff matters because the dashboard SoC stalls near the top of
charge while DC energy still flows. Without it, a charge segment that includes
the taper looks like it stored far more energy per SoC point than it did.

`calibration.joint_fit` is an independent no-intercept least-squares fit of the
same equations. `calibration.effective_capacity_kwh` uses the median implied
capacity in the highest current bin that has enough segments in both
directions, which is less sensitive to the top-of-charge taper.

The corrected model table adds:

| Column | Meaning |
| --- | --- |
| `charge_stored_efficiency` | `eta_acdc - L / P_ac` (AC → pack) |
| `discharge_stored_efficiency` | `eta_acdc * P_dc / (P_dc + L)` (pack → AC) |
| `stored_round_trip_efficiency` | Product of the two corrected values |
| `price_ratio_required` | `1 / stored_round_trip_efficiency` |

Natural-data calibration lands around 45–47 kWh and 0.07–0.13 kW because the
long discharge sessions reach lower SoC, where the dashboard slope differs.
The direct 4 h/3 h experiment is the cleaner 46.5 kWh / 0.125 kW figure and is
the value used by `battery_efficiency.py`.

Use the corrected columns, not the raw AC↔DC columns, when deciding whether a
storage/export cycle is worth it. A blanket round-trip constant cannot
represent both a 16 A→16 A cycle (~0.84) and a 3 A→3 A cycle (~0.45).

The static model in `battery_efficiency.py` uses the same 3–16 A table. The
test Wallbox is derated to 16 A, so that is the top *measured* point, not a
universal limit. Above 16 A the model holds efficiency at the last measured
value and scales AC power linearly with current (`P ∝ I`), which gives ~7.07 kW
export at 32 A. The default wallbox maximum currents remain 32 A (non-derated
capability); a derated site must configure its real limit. The
`max_export_power_kw` setting remains a separate upper bound.

## Caveats

- The measurements come from the Wallbox side, not LeafSpy. Cell resistance and
  any vehicle auxiliary load are not visible at that boundary. The
  `calibration` section estimates the vehicle load, but only from long steady
  segments; short sessions and sub-10% SoC changes are too noisy.
- **The earlier manual LeafSpy table is a lower bound, not a converter
  measurement.** The car had to be powered up to read LeafSpy, so pack-side
  current can include the 12 V DC-DC converter, coolant pumps, fans and BMS
  electronics. That load is charged to the pack but does not store energy, so
  it depresses AC↔pack efficiency, most severely at low current where it is a
  large fraction of the total. The Wallbox AC↔DC result measures a different
  boundary (charger output) and will generally be higher. Compare
  `soc_based.<direction>[].ac_dc_efficiency` with `.efficiency` to see the gap,
  but do not attribute all of it to auxiliaries: SoC quantisation, BMS taper
  and usable-capacity error also contribute.
- The existing manual data was taken at ~94% SoC and ~15°C ambient. Use
  `--battery-capacity-kwh` and several months of data to see whether the model
  is stable across SoC and season.
- SoC is quantised and may be reported as `-1` when unknown; unknown SoC rows
  still contribute to AC↔DC efficiency but not to the SoC cross-check.
