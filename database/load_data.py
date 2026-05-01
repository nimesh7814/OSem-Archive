#!/usr/bin/env python3
"""
load_data.py
------------
Loads OpenSenseMap archive data into TimescaleDB running in Docker.

The script connects to the database using the values in your .env file.
As long as Docker is running and the container is healthy, the script will
connect to localhost on the port mapped in docker-compose.yml (POSTGRES_PORT).

Archive structure:
    <root>/
      2014-06-03/
        538da4d6a834155415765eae-Ctronix/
          538da4d6a834155415765eae-Ctronix-2014-06-03.json   ← station + sensors
          538da4d6a834155415765eaf-2014-06-03.csv            ← sensor readings

Requirements:
    pip install psycopg2-binary python-dotenv tqdm requests beautifulsoup4

Usage:
    # Load everything from a local archive folder
    python load_data.py --source /path/to/archive

    # Load from internet archive URL (directory listing must be browsable)
    python load_data.py --source https://archive.example.com/osem/

    # Filter by date range
    python load_data.py --source /path/to/archive --start 2014-06-01 --end 2014-06-30

    # Dry run — no DB writes, outputs preview CSVs in ./dry_run_output/
    python load_data.py --source /path/to/archive --dry-run

    # Dry run with date range
    python load_data.py --source /path/to/archive --dry-run --start 2014-06-01 --end 2014-06-30

    # Custom .env file
    python load_data.py --source /path/to/archive --env /path/to/.env
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import psycopg2
import psycopg2.extras
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATE_FOLDER_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DRY_RUN_DIR     = Path("dry_run_output")
STATION_ID_LEN  = 24


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load OpenSenseMap archive into TimescaleDB"
    )
    parser.add_argument(
        "--source", required=True,
        help="Local folder or base URL of the archive"
    )
    parser.add_argument(
        "--start", default=None,
        help="Start date (YYYY-MM-DD, inclusive). Default: all dates"
    )
    parser.add_argument(
        "--end", default=None,
        help="End date   (YYYY-MM-DD, inclusive). Default: all dates"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — no DB writes. Outputs tab-separated CSV files."
    )
    parser.add_argument(
        "--env", default=".env",
        help="Path to .env file (default: .env)"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# .env / DB connection
# ---------------------------------------------------------------------------
def load_env(env_path: str) -> None:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f"[INFO] Loaded env from '{env_path}'")
    else:
        print(f"[WARN] .env not found at '{env_path}', using shell environment")


def get_db_connection():
    # POSTGRES_HOST defaults to localhost — correct when Docker maps the port
    # to the host machine via docker-compose.yml (e.g. ports: "5435:5432")
    params = {
        "host":     os.getenv("POSTGRES_HOST", "localhost"),
        "port":     os.getenv("POSTGRES_PORT", "5432"),
        "dbname":   os.getenv("POSTGRES_DB"),
        "user":     os.getenv("POSTGRES_USER"),
        "password": os.getenv("POSTGRES_PASSWORD"),
    }
    missing = [k for k, v in params.items() if not v]
    if missing:
        print(f"[ERROR] Missing env vars: {', '.join(missing)}")
        sys.exit(1)
    try:
        conn = psycopg2.connect(**params)
        print(
            f"[INFO] Connected → {params['user']}@"
            f"{params['host']}:{params['port']}/{params['dbname']}"
        )
        return conn
    except psycopg2.OperationalError as e:
        print(f"[ERROR] DB connection failed:\n  {e}")
        print("[HINT]  Make sure Docker is running:  docker compose up -d")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Source abstraction — local or HTTP
# ---------------------------------------------------------------------------
def is_url(source: str) -> bool:
    return urlparse(source).scheme in ("http", "https")


def list_local_subdirs(folder: Path) -> list[str]:
    return sorted(
        d.name for d in folder.iterdir()
        if d.is_dir()
    )


def list_url_subdirs(base_url: str) -> list[str]:
    """Parse an Apache/Nginx directory listing for subdirectory hrefs."""
    resp = requests.get(base_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    dirs = []
    for a in soup.find_all("a", href=True):
        href = a["href"].rstrip("/")
        if href and not href.startswith(("?", "/")):
            dirs.append(href)
    return sorted(set(dirs))


def read_local_file(path: Path) -> Optional[bytes]:
    if path.exists():
        return path.read_bytes()
    return None


def read_url_file(url: str) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
        return None
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------
def parse_date(s: Optional[str]) -> Optional[date]:
    if s is None:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def in_date_range(folder_name: str, start: Optional[date], end: Optional[date]) -> bool:
    if not DATE_FOLDER_RE.match(folder_name):
        return False
    d = datetime.strptime(folder_name, "%Y-%m-%d").date()
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_station_json(raw: bytes) -> Optional[dict]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"  [WARN] Could not parse JSON: {e}")
        return None


def parse_sensor_csv(raw: bytes) -> list[dict]:
    """Return list of {createdAt, value} dicts."""
    rows = []
    try:
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rows.append(row)
    except Exception as e:
        print(f"  [WARN] Could not parse CSV: {e}")
    return rows


# ---------------------------------------------------------------------------
# DB upsert helpers
# ---------------------------------------------------------------------------
def upsert_station(cur, data: dict) -> Optional[int]:
    """Insert or get st_uuid for a station."""
    coords = data.get("loc", {}).get("geometry", {}).get("coordinates", [None, None])
    lon, lat = coords[0], coords[1]
    if lon is None or lat is None:
        print(f"  [WARN] Station {data.get('id')} has no geometry, skipping")
        return None

    cur.execute("""
        INSERT INTO stations (st_id, boxtype, exposure, model, geometry, region, country, init_date)
        VALUES (
            %s, %s, %s, %s,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326),
            NULL, NULL, NULL
        )
        ON CONFLICT (st_id) DO UPDATE
            SET boxtype  = EXCLUDED.boxtype,
                exposure = EXCLUDED.exposure,
                model    = EXCLUDED.model,
                geometry = EXCLUDED.geometry
        RETURNING st_uuid
    """, (
        data.get("id"),
        data.get("boxType"),
        data.get("exposure"),
        data.get("model"),
        lon, lat,
    ))
    row = cur.fetchone()
    return row[0] if row else None


def upsert_sensor(cur, sensor: dict, st_uuid: int) -> Optional[int]:
    """Insert or get se_uuid for a sensor."""
    cur.execute("""
        INSERT INTO sensors (se_id, st_uuid, title, unit, info, type, init_date)
        VALUES (%s, %s, %s, %s, %s, NULL, NULL)
        ON CONFLICT (se_id) DO UPDATE
            SET title = EXCLUDED.title,
                unit  = EXCLUDED.unit,
                info  = EXCLUDED.info
        RETURNING se_uuid
    """, (
        sensor.get("id"),
        st_uuid,
        sensor.get("title"),
        sensor.get("unit"),
        sensor.get("sensorType"),
    ))
    row = cur.fetchone()
    return row[0] if row else None


def insert_readings(cur, se_uuid: int, rows: list[dict]) -> int:
    """Bulk-insert readings, skip malformed rows. Returns count inserted."""
    records = []
    for row in rows:
        try:
            t = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
            v = float(row["value"])
            records.append((se_uuid, t, v))
        except (KeyError, ValueError):
            continue

    if not records:
        return 0

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO readings (se_uuid, time, value) VALUES %s
        ON CONFLICT (se_uuid, time) DO NOTHING
        """,
        records,
        page_size=500,
    )
    return len(records)


# ---------------------------------------------------------------------------
# Dry-run CSV writers
# ---------------------------------------------------------------------------
def init_dry_run() -> dict:
    DRY_RUN_DIR.mkdir(exist_ok=True)
    buffers = {
        "stations": [],
        "sensors":  [],
        "readings": [],
    }
    return buffers


def flush_dry_run(buffers: dict) -> None:
    headers = {
        "stations": ["st_id", "boxtype", "model", "lon", "lat", "region", "country", "init_date"],
        "sensors":  ["se_id", "st_id", "title", "unit", "info", "type", "init_date"],
        "readings": ["se_id", "time", "value"],
    }
    for name, rows in buffers.items():
        out_path = DRY_RUN_DIR / f"{name}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(headers[name])
            writer.writerows(rows)
        print(f"  [DRY-RUN] Written {len(rows):,} rows → {out_path}")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
def process_station_folder(
    source: str,
    date_folder: str,
    station_folder: str,
    is_remote: bool,
    conn,
    dry_buffers: Optional[dict],
) -> dict:
    """Process one station folder. Returns counts."""
    counts = {"sensors": 0, "readings": 0, "skipped_csv": 0}

    # Locate the .json file
    if is_remote:
        base = urljoin(source.rstrip("/") + "/", f"{date_folder}/{station_folder}/")
        listing_raw = read_url_file(base)
        if not listing_raw:
            return counts
        soup = BeautifulSoup(listing_raw.decode("utf-8"), "html.parser")
        files = [a["href"] for a in soup.find_all("a", href=True)
                 if not a["href"].startswith(("?", "/", "."))]
    else:
        folder_path = Path(source) / date_folder / station_folder
        files = [f.name for f in folder_path.iterdir() if f.is_file()]

    # Find the station JSON file (first 24 chars = station id, ends with .json)
    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        print(f"  [WARN] No .json found in {station_folder}")
        return counts

    json_filename = json_files[0]
    if is_remote:
        raw_json = read_url_file(urljoin(base, json_filename))
    else:
        raw_json = read_local_file(folder_path / json_filename)

    if not raw_json:
        print(f"  [WARN] Could not read {json_filename}")
        return counts

    station_data = parse_station_json(raw_json)
    if not station_data:
        return counts

    coords = station_data.get("loc", {}).get("geometry", {}).get("coordinates", [None, None])
    lon, lat = coords[0], coords[1]

    # ── DB or dry-run: station ──────────────────────────────────────────────
    if dry_buffers is not None:
        st_id = station_data.get("id", "")
        dry_buffers["stations"].append([
            st_id,
            station_data.get("boxType"),
            station_data.get("model"),
            lon, lat,
            None, None, None,
        ])
    else:
        with conn.cursor() as cur:
            st_uuid = upsert_station(cur, station_data)
        conn.commit()
        if st_uuid is None:
            return counts

    # ── Sensors ────────────────────────────────────────────────────────────
    sensors = station_data.get("sensors", [])
    csv_files = {f[:STATION_ID_LEN]: f for f in files if f.endswith(".csv")}

    for sensor in sensors:
        se_id = sensor.get("id", "")

        if dry_buffers is not None:
            dry_buffers["sensors"].append([
                se_id,
                station_data.get("id"),
                sensor.get("title"),
                sensor.get("unit"),
                sensor.get("sensorType"),
                None, None,
            ])
        else:
            with conn.cursor() as cur:
                se_uuid = upsert_sensor(cur, sensor, st_uuid)
            conn.commit()
            if se_uuid is None:
                continue

        # ── Readings CSV ───────────────────────────────────────────────────
        csv_filename = csv_files.get(se_id)
        if not csv_filename:
            counts["skipped_csv"] += 1
            continue

        if is_remote:
            raw_csv = read_url_file(urljoin(base, csv_filename))
        else:
            raw_csv = read_local_file(folder_path / csv_filename)

        if not raw_csv:
            counts["skipped_csv"] += 1
            continue

        rows = parse_sensor_csv(raw_csv)

        if dry_buffers is not None:
            for row in rows:
                dry_buffers["readings"].append([se_id, row.get("createdAt"), row.get("value")])
            counts["readings"] += len(rows)
        else:
            with conn.cursor() as cur:
                n = insert_readings(cur, se_uuid, rows)
            conn.commit()
            counts["readings"] += n

        counts["sensors"] += 1

    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args    = parse_args()
    source  = args.source
    dry_run = args.dry_run
    start   = parse_date(args.start)
    end     = parse_date(args.end)

    load_env(args.env)

    is_remote = is_url(source)
    conn      = None
    dry_buffers = None

    if dry_run:
        print("[DRY-RUN] No data will be written to the database.")
        if start or end:
            print(f"[DRY-RUN] Date range: {start or 'beginning'} → {end or 'end'}")
        dry_buffers = init_dry_run()
    else:
        conn = get_db_connection()

    # ── Discover date folders ───────────────────────────────────────────────
    print(f"\n[INFO] Scanning source: {source}")
    if is_remote:
        all_date_folders = list_url_subdirs(source)
    else:
        all_date_folders = list_local_subdirs(Path(source))

    date_folders = [
        d for d in all_date_folders
        if in_date_range(d, start, end)
    ]

    if not date_folders:
        print("[WARN] No date folders found matching the given range.")
        sys.exit(0)

    print(f"[INFO] Date folders to process: {len(date_folders)}")
    if start or end:
        print(f"       Range: {start or 'beginning'} → {end or 'end'}")

    # ── Totals ──────────────────────────────────────────────────────────────
    total = {"stations": 0, "sensors": 0, "readings": 0, "skipped_csv": 0}

    for date_folder in tqdm(date_folders, desc="Date folders", unit="day"):
        # Discover station folders
        if is_remote:
            date_url = urljoin(source.rstrip("/") + "/", date_folder + "/")
            station_folders = list_url_subdirs(date_url)
        else:
            station_folders = list_local_subdirs(Path(source) / date_folder)

        for station_folder in tqdm(
            station_folders,
            desc=f"  {date_folder}",
            unit="station",
            leave=False,
        ):
            counts = process_station_folder(
                source, date_folder, station_folder,
                is_remote, conn, dry_buffers,
            )
            total["stations"]    += 1
            total["sensors"]     += counts["sensors"]
            total["readings"]    += counts["readings"]
            total["skipped_csv"] += counts["skipped_csv"]

    # ── Dry-run output ──────────────────────────────────────────────────────
    if dry_run:
        print("\n[DRY-RUN] Writing preview CSVs...")
        flush_dry_run(dry_buffers)

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print("  Load complete")
    print("=" * 50)
    print(f"  Stations processed : {total['stations']:>10,}")
    print(f"  Sensors  processed : {total['sensors']:>10,}")
    print(f"  Readings inserted  : {total['readings']:>10,}")
    print(f"  CSVs not found     : {total['skipped_csv']:>10,}")
    print("=" * 50)

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
