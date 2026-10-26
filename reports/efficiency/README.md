# Efficiency reports

`analyse_efficiency.py` writes its output here by default. The files are
generated, but the current best report is worth committing so that the planner
has a stable efficiency model to use and changes over time are reviewable.

## Generating a report

On the production machine (where `data/config/config.yaml` holds the InfluxDB
credentials):

```bash
PYTHONPATH=src .venv/bin/python -m evse_controller.tools.analyse_efficiency \
    --days 30 \
    --battery-capacity-kwh 59 \
    --output reports/efficiency
```

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

## Caveats

- The measurements come from the Wallbox side, not LeafSpy. Cell resistance and
  any vehicle auxiliary load are not visible at that boundary.
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
