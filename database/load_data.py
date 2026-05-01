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

# Lazy import for spatial lookup (only needed when admin_boundary.geojson exists)
try:
    import json as _json  # already imported above, alias for clarity
    from shapely.geometry import Point, shape
    _SHAPELY_AVAILABLE = True
except ImportError:
    _SHAPELY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATE_FOLDER_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DRY_RUN_DIR     = Path("dry_run_output")
STATION_ID_LEN  = 24
ADMIN_BOUNDARY_PATH = Path("data/admin_boundary.geojson")

# ---------------------------------------------------------------------------
# Admin boundary spatial index (loaded once at startup)
# ---------------------------------------------------------------------------
# Each entry: (shapely geometry, adm0_name, adm1_name)
_ADMIN_FEATURES: list[tuple] = []


def load_admin_boundaries(geojson_path: Path = ADMIN_BOUNDARY_PATH) -> None:
    """Load admin_boundary.geojson into an in-memory list for point-in-polygon lookups."""
    global _ADMIN_FEATURES
    if not _SHAPELY_AVAILABLE:
        print("[WARN] shapely not installed — country/region will not be populated.")
        print("       pip install shapely")
        return
    if not geojson_path.exists():
        print(f"[WARN] Admin boundary file not found: {geojson_path}")
        print("       country/region will be set to NULL.")
        return

    with open(geojson_path, encoding="utf-8") as f:
        fc = json.load(f)

    for feature in fc.get("features", []):
        props = feature.get("properties", {})
        geom  = shape(feature["geometry"])
        _ADMIN_FEATURES.append((geom, props.get("adm0_name"), props.get("adm1_name")))

    print(f"[INFO] Loaded {len(_ADMIN_FEATURES):,} admin boundary polygons from {geojson_path}")


def lookup_admin(lon: float, lat: float) -> tuple[Optional[str], Optional[str]]:
    """Return (country, region) for a point using ST_Within logic.

    Iterates admin boundary polygons and returns the first match.
    Returns (None, None) if the point falls outside all polygons or
    shapely is unavailable.
    """
    if not _ADMIN_FEATURES or not _SHAPELY_AVAILABLE:
        return None, None
    pt = Point(lon, lat)
    for geom, country, region in _ADMIN_FEATURES:
        if geom.contains(pt):
            return country, region
    return None, None


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
# Database size helper
# ---------------------------------------------------------------------------
def get_db_size_mb(conn) -> float:
    """Return the current total size of the database in MB."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        size_bytes = cur.fetchone()[0]
    return size_bytes / (1024 * 1024)


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
def upsert_station(cur, data: dict) -> tuple[Optional[int], bool, Optional[str]]:
    """Insert or get st_uuid for a station.

    Returns (st_uuid, is_new, country) where is_new=True when freshly inserted.
    """
    coords = data.get("loc", {}).get("geometry", {}).get("coordinates", [None, None])
    lon, lat = coords[0], coords[1]
    if lon is None or lat is None:
        print(f"  [WARN] Station {data.get('id')} has no geometry, skipping")
        return None

    country, region = lookup_admin(lon, lat)

    cur.execute("""
        INSERT INTO stations (st_id, boxtype, exposure, model, geometry, region, country, init_date)
        VALUES (
            %s, %s, %s, %s,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326),
            %s, %s, %s
        )
        ON CONFLICT (st_id) DO UPDATE
            SET boxtype   = EXCLUDED.boxtype,
                exposure  = EXCLUDED.exposure,
                model     = EXCLUDED.model,
                geometry  = EXCLUDED.geometry,
                country   = EXCLUDED.country,
                region    = EXCLUDED.region,
                init_date = CASE
                    WHEN stations.init_date IS NULL THEN EXCLUDED.init_date
                    WHEN EXCLUDED.init_date < stations.init_date THEN EXCLUDED.init_date
                    ELSE stations.init_date
                END
        RETURNING st_uuid, (xmax = 0) AS is_new, country
    """, (
        data.get("id"),
        data.get("boxType"),
        data.get("exposure"),
        data.get("model"),
        lon, lat,
        region,
        country,
        data.get("_init_date"),  # injected by caller from the date_folder
    ))
    row = cur.fetchone()
    if not row:
        return None, False, None
    st_uuid, is_new, stored_country = row
    return st_uuid, is_new, stored_country


def upsert_sensor(cur, sensor: dict, st_uuid: int, init_date=None) -> tuple[Optional[int], bool]:
    """Insert or get se_uuid for a sensor.

    init_date should be the earliest timestamp from the readings CSV for this
    sensor. Written only on first insert; subsequent runs leave it untouched so
    the earliest date is always preserved.

    Returns (se_uuid, is_new) where is_new is True when the row was freshly
    inserted (xmax = 0 means INSERT path, not UPDATE path).
    """
    cur.execute("""
        INSERT INTO sensors (se_id, st_uuid, title, unit, info, type, init_date)
        VALUES (%s, %s, %s, %s, %s, NULL, %s)
        ON CONFLICT (se_id) DO UPDATE
            SET title     = EXCLUDED.title,
                unit      = EXCLUDED.unit,
                info      = EXCLUDED.info,
                init_date = CASE
                    WHEN sensors.init_date IS NULL THEN EXCLUDED.init_date
                    WHEN EXCLUDED.init_date < sensors.init_date THEN EXCLUDED.init_date
                    ELSE sensors.init_date
                END
        RETURNING se_uuid, (xmax = 0) AS is_new, init_date
    """, (
        sensor.get("id"),
        st_uuid,
        sensor.get("title"),
        sensor.get("unit"),
        sensor.get("sensorType"),
        init_date,
    ))
    row = cur.fetchone()
    if not row:
        return None, False
    se_uuid, is_new, stored_init_date = row
    return se_uuid, is_new


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
    counts = {"sensors": 0, "readings": 0, "skipped_csv": 0, "new_sensors_by_year": {}, "new_stations_by_country": {}}

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

    # Inject the archive folder date as the station's init_date (first-seen date)
    folder_date = datetime.strptime(date_folder, "%Y-%m-%d").date()
    station_data["_init_date"] = folder_date

    # ── DB or dry-run: station ──────────────────────────────────────────────
    if dry_buffers is not None:
        st_id = station_data.get("id", "")
        country, region = lookup_admin(lon, lat) if (lon is not None and lat is not None) else (None, None)
        dry_buffers["stations"].append([
            st_id,
            station_data.get("boxType"),
            station_data.get("model"),
            lon, lat,
            region,
            country,
            folder_date,
        ])
    else:
        with conn.cursor() as cur:
            st_uuid, is_new_station, station_country = upsert_station(cur, station_data)
        conn.commit()
        if st_uuid is None:
            return counts
        if is_new_station:
            key = station_country or "Unknown"
            counts["new_stations_by_country"][key] = counts["new_stations_by_country"].get(key, 0) + 1

    # ── Sensors ────────────────────────────────────────────────────────────
    sensors = station_data.get("sensors", [])
    csv_files = {f[:STATION_ID_LEN]: f for f in files if f.endswith(".csv")}

    for sensor in sensors:
        se_id = sensor.get("id", "")

        # ── Readings CSV — parse early to extract earliest timestamp ─────────
        csv_filename = csv_files.get(se_id)
        if not csv_filename:
            counts["skipped_csv"] += 1
            # Still upsert the sensor (no readings yet for this period)
            rows = []
        else:
            if is_remote:
                raw_csv = read_url_file(urljoin(base, csv_filename))
            else:
                raw_csv = read_local_file(folder_path / csv_filename)

            if not raw_csv:
                counts["skipped_csv"] += 1
                rows = []
            else:
                rows = parse_sensor_csv(raw_csv)

        # Derive sensor init_date from the earliest timestamp in the CSV
        sensor_init_date = None
        for row in rows:
            try:
                t = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
                if sensor_init_date is None or t < sensor_init_date:
                    sensor_init_date = t
            except (KeyError, ValueError):
                continue

        if dry_buffers is not None:
            dry_buffers["sensors"].append([
                se_id,
                station_data.get("id"),
                sensor.get("title"),
                sensor.get("unit"),
                sensor.get("sensorType"),
                None,
                sensor_init_date,
            ])
        else:
            with conn.cursor() as cur:
                se_uuid, is_new_sensor = upsert_sensor(cur, sensor, st_uuid, init_date=sensor_init_date)
            conn.commit()
            if se_uuid is None:
                continue
            if is_new_sensor and sensor_init_date is not None:
                year = sensor_init_date.year
                counts["new_sensors_by_year"][year] = counts["new_sensors_by_year"].get(year, 0) + 1

        if not rows:
            continue

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
# Sensor-by-year progress chart
# ---------------------------------------------------------------------------
def print_sensor_year_chart(by_year: dict) -> None:
    """Print a bar chart + table of new sensors grouped by init_date year."""
    if not by_year:
        print("\n[INFO] No new sensors were added this run (all already existed).")
        return

    years      = sorted(by_year)
    counts_    = [by_year[y] for y in years]
    max_count  = max(counts_)
    bar_width  = 40          # max bar length in characters
    col_w_year = 6
    col_w_cnt  = 10
    col_w_bar  = bar_width + 2

    sep = "+" + "-" * col_w_year + "+" + "-" * col_w_cnt + "+" + "-" * col_w_bar + "+"

    print("\n  New sensors added — by init_date year")
    print(sep)
    print(f"| {'Year':<{col_w_year-1}}| {'Count':>{col_w_cnt-1}} | {'Bar':<{col_w_bar-1}}|")
    print(sep)

    for year, count in zip(years, counts_):
        filled = round(count / max_count * bar_width) if max_count else 0
        bar    = "█" * filled + "░" * (bar_width - filled)
        print(f"| {year:<{col_w_year-1}}| {count:>{col_w_cnt-1},} | {bar} |")

    print(sep)
    print(f"| {'TOTAL':<{col_w_year-1}}| {sum(counts_):>{col_w_cnt-1},} | {'':<{col_w_bar-1}}|")
    print(sep)


# ---------------------------------------------------------------------------
# Station-by-country progress chart
# ---------------------------------------------------------------------------
def print_station_country_chart(by_country: dict) -> None:
    """Print a bar chart + table of new stations grouped by country."""
    if not by_country:
        print("\n[INFO] No new stations were added this run (all already existed).")
        return

    # Sort by count descending, then alphabetically for ties
    sorted_items = sorted(by_country.items(), key=lambda x: (-x[1], x[0]))
    countries    = [c for c, _ in sorted_items]
    counts_      = [n for _, n in sorted_items]
    max_count    = max(counts_)
    bar_width    = 40
    col_w_ctry   = max(16, max(len(c) for c in countries) + 2)
    col_w_cnt    = 10
    col_w_bar    = bar_width + 2

    sep = "+" + "-" * col_w_ctry + "+" + "-" * col_w_cnt + "+" + "-" * col_w_bar + "+"

    print("\n  New stations added — by country")
    print(sep)
    print(f"| {'Country':<{col_w_ctry-1}}| {'Count':>{col_w_cnt-1}} | {'Bar':<{col_w_bar-1}}|")
    print(sep)

    for country, count in zip(countries, counts_):
        filled = round(count / max_count * bar_width) if max_count else 0
        bar    = "█" * filled + "░" * (bar_width - filled)
        print(f"| {country:<{col_w_ctry-1}}| {count:>{col_w_cnt-1},} | {bar} |")

    print(sep)
    print(f"| {'TOTAL':<{col_w_ctry-1}}| {sum(counts_):>{col_w_cnt-1},} | {'':<{col_w_bar-1}}|")
    print(sep)


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
    load_admin_boundaries()  # load once into _ADMIN_FEATURES

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
        db_size_before_mb = get_db_size_mb(conn)
        print(f"[INFO] Database size before load: {db_size_before_mb:.2f} MB")

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
    total = {"stations": 0, "sensors": 0, "readings": 0, "skipped_csv": 0, "new_sensors_by_year": {}, "new_stations_by_country": {}}

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
            for yr, cnt in counts["new_sensors_by_year"].items():
                total["new_sensors_by_year"][yr] = total["new_sensors_by_year"].get(yr, 0) + cnt
            for ctry, cnt in counts["new_stations_by_country"].items():
                total["new_stations_by_country"][ctry] = total["new_stations_by_country"].get(ctry, 0) + cnt

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
        db_size_after_mb = get_db_size_mb(conn)
        db_size_added_mb = db_size_after_mb - db_size_before_mb
        print(f"\n  Database size before : {db_size_before_mb:>10.2f} MB")
        print(f"  Database size after  : {db_size_after_mb:>10.2f} MB")
        print(f"  Data added           : {db_size_added_mb:>+10.2f} MB")
        print("=" * 50)

    print_sensor_year_chart(total["new_sensors_by_year"])
    print_station_country_chart(total["new_stations_by_country"])

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
