#!/usr/bin/env python3
"""
extract_sensor_table.py
-----------------------
Walks an OpenSenseMap archive folder and extracts all unique sensor
(title, unit) combinations into a CSV file using parallel workers.

Usage:
    python extract_sensor_table.py --source /path/to/archive
    python extract_sensor_table.py --source /path/to/archive --output sensors.csv
    python extract_sensor_table.py --source /path/to/archive --all   # include duplicates
    python extract_sensor_table.py --source /path/to/archive --workers 16
"""

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_WORKERS = 32


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract sensor title + unit from OpenSenseMap archive JSON files"
    )
    parser.add_argument(
        "--source", required=True,
        help="Root folder of the OpenSenseMap archive"
    )
    parser.add_argument(
        "--output", default="sensors.csv",
        help="Path to write the CSV file (default: sensors.csv)"
    )
    parser.add_argument(
        "--all", dest="all_rows", action="store_true",
        help="Include all sensor rows (with duplicates). "
             "Default: only unique (title, unit) pairs."
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Number of parallel worker threads (default: {DEFAULT_WORKERS})"
    )
    return parser.parse_args()


def collect_json_files(root: Path) -> list[Path]:
    """Collect all station JSON file paths from the archive."""
    paths = []
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        for station_dir in sorted(date_dir.iterdir()):
            if not station_dir.is_dir():
                continue
            for f in station_dir.iterdir():
                if f.suffix == ".json":
                    paths.append(f)
    return paths


def process_json_file(json_path: Path) -> list[dict]:
    """Parse one station JSON and return its sensor rows."""
    rows = []
    try:
        data = json.loads(json_path.read_bytes().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(f"[WARN] Could not parse {json_path}: {e}", file=sys.stderr)
        return rows

    station_id = data.get("_id") or data.get("id", "")
    sensors = data.get("sensors") or data.get("Sensors") or []

    for sensor in sensors:
        title = sensor.get("title") or sensor.get("Title") or ""
        unit  = sensor.get("unit")  or sensor.get("Unit")  or ""
        se_id = sensor.get("_id")   or sensor.get("id", "")
        rows.append({
            "title":      title,
            "unit":       unit,
            "sensor_id":  se_id,
            "station_id": station_id,
        })

    return rows


def extract_sensors(root: Path, workers: int, unique_only: bool) -> list[dict]:
    """Scan archive in parallel and return sensor rows."""
    json_files = collect_json_files(root)
    total = len(json_files)
    print(f"[INFO] Found {total:,} station JSON files — processing with {workers} workers...")

    all_rows: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_json_file, p): p for p in json_files}
        for future in as_completed(futures):
            all_rows.extend(future.result())
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  [{done:>6,} / {total:,}] files processed", end="\r")

    print()  # newline after progress

    if unique_only:
        seen = set()
        unique_rows = []
        for row in all_rows:
            key = (row["title"], row["unit"])
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)
        return unique_rows

    return all_rows


def write_csv(rows: list[dict], path: str, unique_only: bool) -> None:
    fieldnames = ["title", "unit"] if unique_only else ["title", "unit", "sensor_id", "station_id"]
    out = Path(path)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"[INFO] Written {len(rows):,} row(s) to {out}")


def main() -> None:
    args = parse_args()
    root = Path(args.source)

    if not root.is_dir():
        print(f"[ERROR] Source folder not found: {root}", file=sys.stderr)
        sys.exit(1)

    unique_only = not args.all_rows
    print(f"[INFO] Scanning archive : {root}")
    print(f"[INFO] Workers          : {args.workers}")
    print(f"[INFO] Mode             : {'unique (title, unit) pairs' if unique_only else 'all sensor rows'}")
    print(f"[INFO] Output           : {args.output}")
    print()

    rows = extract_sensors(root, workers=args.workers, unique_only=unique_only)
    write_csv(rows, args.output, unique_only)

    print(f"\n[DONE] {len(rows):,} row(s) saved to {args.output}")


if __name__ == "__main__":
    main()
