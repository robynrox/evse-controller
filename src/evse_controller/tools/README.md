# EVSE Controller Tools

This directory contains standalone utility scripts for the EVSE controller project.

## Available Tools

### Octopus Intelligent Fetcher
- **File**: `octopus_intelligent_fetcher.py`
- **Purpose**: Fetches information from the Octopus Intelligent tariff system using the GraphQL API
- **Dependencies**: Requires additional dependencies (see `octopus_intelligent_fetcher_requirements.txt`)
- **Documentation**: See `OCTOPUS_FETCHER_README.md` in the project root for detailed usage instructions

### InfluxDB Export Tool
- **File**: `export_influxdb.py`
- **Purpose**: Exports power logging data from InfluxDB to compressed CSV and JSON files
- **Dependencies**: Uses project dependencies (influxdb-client, pandas, etc.)
- **Usage**: `python src/evse_controller/tools/export_influxdb.py [start_date] [end_date]` (dates in YYYY-MM-DD format)

### Efficiency Analysis
- **File**: `analyse_efficiency.py` (pure analysis helpers in `efficiency_analysis.py`)
- **Purpose**: Segments recorded Wallbox AC/DC data into steady windows, computes charge
  and discharge efficiency and loss, fits `loss = a + b*P_dc^2` per direction, and
  produces a current-indexed model table for the energy planner
- **Dependencies**: Project dependencies only (`influxdb-client` for live queries; no
  pandas required)
- **Usage**:
  ```bash
  # On the production machine, using data/config/config.yaml
  PYTHONPATH=src python -m evse_controller.tools.analyse_efficiency --days 30

  # Or run the file directly from a checkout
  python src/evse_controller/tools/analyse_efficiency.py --days 30

  # Offline from an export (including .csv.gz)
  PYTHONPATH=src python -m evse_controller.tools.analyse_efficiency \
      --input-csv data/backup/influxdb/powerlog_20260901.csv.gz
  ```
  See `reports/efficiency/README.md` for the outputs and interpretation.

## Running the Tools

Each tool has different requirements and should be run independently of the main project.

For the Octopus Intelligent Fetcher, see the dedicated README for setup instructions, as it has additional dependencies that may conflict with the main project's dependencies.