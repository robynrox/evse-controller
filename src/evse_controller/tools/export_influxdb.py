#!/usr/bin/env python3

import os
import yaml
from datetime import datetime, timedelta
import influxdb_client
import pandas as pd
import sys
from pathlib import Path
import gzip
import shutil

def get_influxdb_client():
    """Initialize and return InfluxDB client from config"""
    # Load config
    with open('data/config/config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    # Get InfluxDB settings
    url = config['influxdb']['url']
    token = config['influxdb']['token']
    org = config['influxdb']['org']

    # Initialize client with longer timeout
    return influxdb_client.InfluxDBClient(
        url=url,
        token=token,
        org=org,
        timeout=30_000  # 30 seconds timeout
    )

def export_day(client, date, backup_dir):
    """Export data for a specific day to CSV and JSON with compression"""
    start_time = datetime.combine(date, datetime.min.time())
    end_time = datetime.combine(date, datetime.max.time())
    
    print(f"Querying data for {date.strftime('%Y-%m-%d')}")
    
    query_api = client.query_api()
    query = f'''from(bucket: "powerlog")
      |> range(start: {start_time.isoformat()}Z, stop: {end_time.isoformat()}Z)
      |> filter(fn: (r) => r["_measurement"] == "measurement")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")'''

    try:
        result = query_api.query_data_frame(query)
        if not isinstance(result, list):
            result = [result]
        
        if not result or (isinstance(result, list) and len(result) == 0):
            print(f"No data found for {date.strftime('%Y-%m-%d')}")
            return False
            
        df = pd.concat(result, ignore_index=True) if len(result) > 1 else result[0]
        
        if len(df) == 0:
            print(f"No data found for {date.strftime('%Y-%m-%d')}")
            return False
            
        date_str = date.strftime('%Y%m%d')
        csv_file = backup_dir / f"powerlog_{date_str}.csv.gz"
        json_file = backup_dir / f"powerlog_{date_str}.json.gz"
        
        # Save compressed CSV
        with gzip.open(csv_file, 'wt') as f:
            df.to_csv(f, index=False)
        
        # Save compressed JSON
        with gzip.open(json_file, 'wt') as f:
            df.to_json(f, orient='records', date_format='iso')
        
        print(f"Exported {len(df)} records for {date.strftime('%Y-%m-%d')}")
        print(f"Files created: {csv_file.name} and {json_file.name}")
        
        # Optional: Print compression ratio
        original_size = len(df.to_csv().encode('utf-8'))
        compressed_size = csv_file.stat().st_size
        ratio = original_size / compressed_size
        print(f"Compression ratio: {ratio:.2f}x (CSV)")
        
        return True
        
    except Exception as e:
        print(f"Error exporting data for {date.strftime('%Y-%m-%d')}: {e}")
        return False

def get_missing_dates(backup_dir, start_date, end_date):
    """Return list of dates that don't have backup files"""
    existing_backups = set()
    for file in backup_dir.glob("powerlog_*.csv.gz"):
        try:
            date_str = file.stem.split('_')[1]  # Now need to handle .csv.gz
            date_str = date_str.replace('.csv', '')  # Remove .csv from stem
            existing_backups.add(datetime.strptime(date_str, '%Y%m%d').date())
        except (IndexError, ValueError):
            continue
    
    all_dates = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]
    return [d for d in all_dates if d not in existing_backups]

def main():
    # Setup backup directory
    backup_dir = Path("data/backup/influxdb")
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Parse command line arguments
    if len(sys.argv) > 1:
        try:
            start_date = datetime.strptime(sys.argv[1], '%Y-%m-%d').date()
            end_date = datetime.strptime(sys.argv[2], '%Y-%m-%d').date() if len(sys.argv) > 2 else start_date
        except ValueError:
            print("Usage: export_influxdb.py [start_date] [end_date]")
            print("Dates should be in YYYY-MM-DD format")
            sys.exit(1)
    else:
        # Default to yesterday if no date specified
        end_date = datetime.now().date() - timedelta(days=1)
        start_date = end_date
    
    # Get list of dates needing backup
    dates_to_backup = get_missing_dates(backup_dir, start_date, end_date)
    
    if not dates_to_backup:
        print(f"No missing backups found between {start_date} and {end_date}")
        return
    
    # Initialize InfluxDB client
    client = get_influxdb_client()
    
    try:
        for date in dates_to_backup:
            export_day(client, date, backup_dir)
    finally:
        client.close()

if __name__ == "__main__":
    main()
