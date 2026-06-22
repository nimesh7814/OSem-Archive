#!/usr/bin/env python3
"""
load_data.py
------------
Loads OpenSenseMap archive data into TimescaleDB running in Docker.

Schema (schema.sql):
  stations  – PK: st_id VARCHAR(24)
  sensors   – PK: se_id VARCHAR(24), FK → stations.st_id
  readings  – FK: se_id → sensors.se_id, st_id → stations.st_id  (TimescaleDB hypertable)

The script connects to the database using the values in your .env file.
As long as Docker is running and the container is healthy, the script will
connect to localhost on the port mapped in docker-compose.yml (POSTGRES_PORT).

Checkpoint / resume files (written next to load_data.py):
  data.resume  – one completed "date/station" key per line; delete to restart
  data.log     – JSONL log of every missing-data or missing-location event

Archive structure:
    <root>/
      2014-06-03/
        538da4d6a834155415765eae-Ctronix/
          538da4d6a834155415765eae-Ctronix-2014-06-03.json   <- station + sensors
          538da4d6a834155415765eaf-2014-06-03.csv            <- sensor readings

Requirements:
    pip install psycopg2-binary python-dotenv tqdm requests beautifulsoup4 shapely

Usage:
    # Load everything from a local archive folder directly into the database
    python load_data.py --source /path/to/archive

    # Load from internet archive URL (directory listing must be browsable)
    python load_data.py --source https://archive.example.com/osem/

    # Filter by date range  (resume-safe: same command picks up where it left off)
    python load_data.py --source /path/to/archive --start 2014-06-01 --end 2014-06-30

    # Kill and resume example:
    #   python load_data.py --source D:\\OSeM\\data --start 2014-06-04 --end 2025-05-06
    #   ^C   (kill at any point)
    #   python load_data.py --source D:\\OSeM\\data --start 2014-06-04 --end 2025-05-06
    #   → automatically resumes from the last completed station

    # Dry run -- no DB writes, outputs preview CSVs in ./dry_run_output/
    python load_data.py --source /path/to/archive --dry-run

    # Dry run with date range
    python load_data.py --source /path/to/archive --dry-run --start 2014-06-01 --end 2014-06-30

    # Generate a portable SQL dump (no DB required) -- great for experimentation
    # Output is written to ./sql/<start>_to_<end>.sql
    python load_data.py --source /path/to/archive --type sql
    python load_data.py --source "G:\\OSeM\\archive_data" --start 2014-06-03 --end 2024-12-31 --workers 1 --type sql

    # Split the SQL dump into N parts to avoid writing one huge file
    # Parts are written to ./sql/<start>_to_<end>_part1of3.sql etc.
    python load_data.py --source "G:\\OSeM\\archive_data" --start 2015-01-01 --end 2016-12-31 --type sql --part 3

    # Import a previously generated SQL dump into the database
    python load_data.py --import-sql sql/2015-01-01_to_2016-12-31.sql
    python load_data.py --import-sql sql/2015-01-01_to_2016-12-31.sql --env /path/to/.env

    # Import all parts from a split dump (pass the folder containing the part files)
    python load_data.py --import-sql sql/2015-01-01_to_2016-12-31_parts/
    python load_data.py --import-sql sql/2015-01-01_to_2016-12-31_parts/ --env /path/to/.env

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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Shared HTTP session -- reuses TCP connections across all requests
_SESSION = requests.Session()

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
DATE_FOLDER_RE      = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DRY_RUN_DIR         = Path("dry_run_output")
SQL_DUMP_DIR        = Path("sql")          # all .sql dumps live here
DOWNLOAD_DIR        = Path(".download")    # hybrid mode: staging folder for downloaded date folders
HYBRID_LOOKAHEAD    = 20                    # number of date folders to pre-fetch ahead of processing
SEPARATE_BATCH_SIZE = 100                  # stations per batch in --method separate hybrid mode
STATION_ID_LEN      = 24
# Default admin boundary – can be overridden with --admin-boundary CLI flag
ADMIN_BOUNDARY_PATH = Path(
    r"D:\Lectures\University of Munster\SoSem 2026\Study Project OpenSenseMap"
    r"\OSem-Archive\index\data\admin_boundary.geojson"
)
WORKER_THREADS      = 1   # 1 avoids deadlocks on chunk creation; increase after initial load
# Fixed checkpoint/log file names (written next to the script)
RESUME_FILE         = Path("data.resume")   # completed station keys → resumable on re-run
DATA_LOG_FILE       = Path("data.log")      # JSONL log of missing-data / missing-location events

# ---------------------------------------------------------------------------
# Sensor category lookup table  (loaded once from sensor_types.csv)
# ---------------------------------------------------------------------------
# Maps (title_lower, unit_lower) → category string.
# Built at startup by load_sensor_types_csv(); used by get_sensor_category().
_SENSOR_TYPE_CATEGORY: dict[tuple[str, str], str] = {}


def load_sensor_types_csv(csv_path: Optional[Path] = None) -> None:
    """Load sensor_types.csv into _SENSOR_TYPE_CATEGORY for fast (title, unit) lookups.

    The CSV must have columns 'title' and 'unit'.  A third column 'category' is
    optional; if absent the built-in classify_sensor() heuristic is used to derive
    the category so that every row in the file still gets an entry in the map.

    csv_path defaults to sensor_types.csv in the same folder as load_data.py.
    """
    global _SENSOR_TYPE_CATEGORY
    if csv_path is None:
        csv_path = Path(__file__).parent / "sensor_types.csv"
    if not csv_path.exists():
        print(f"[WARN] sensor_types.csv not found at {csv_path}; "
              "category will be derived by keyword heuristic only.")
        return

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("title") or "").strip()
            unit  = (row.get("unit")  or "").strip()
            if not title and not unit:
                continue
            # Use explicit category column if present, otherwise fall back to heuristic
            category = (row.get("category") or "").strip()
            if not category:
                category = classify_sensor(title, unit)
            _SENSOR_TYPE_CATEGORY[(title.lower(), unit.lower())] = category


def get_sensor_category(title: str, unit: str) -> str:
    """Return the category for a sensor, preferring the CSV lookup table.

    Falls back to classify_sensor() if the exact (title, unit) pair is not in
    the table (e.g. sensors from future archive dates not yet in the CSV).
    """
    key = ((title or "").strip().lower(), (unit or "").strip().lower())
    category = _SENSOR_TYPE_CATEGORY.get(key)
    if category is not None:
        return category
    return classify_sensor(title, unit)

# ---------------------------------------------------------------------------
# Admin boundary spatial index (loaded once at startup)
# ---------------------------------------------------------------------------
# Each entry: (shapely geometry, adm0_name, adm1_name)
_ADMIN_FEATURES: list[tuple] = []
_ADMIN_TREE = None  # shapely STRtree for fast point-in-polygon queries


def load_admin_boundaries(geojson_path: Path = ADMIN_BOUNDARY_PATH) -> None:
    """Load admin_boundary.geojson into an in-memory list + STRtree for fast point-in-polygon lookups."""
    global _ADMIN_FEATURES, _ADMIN_TREE
    if not _SHAPELY_AVAILABLE:
        print("[WARN] shapely not installed -- country/region will not be populated.")
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

    # Build a spatial index so lookup_admin is O(log n) instead of O(n)
    if _ADMIN_FEATURES:
        from shapely.strtree import STRtree
        _ADMIN_TREE = STRtree([geom for geom, _, _ in _ADMIN_FEATURES])

    pass  # admin boundaries loaded silently


def lookup_admin(lon: float, lat: float) -> tuple[Optional[str], Optional[str]]:
    """Return (country, region) for a point using an STRtree spatial index.

    Falls back to linear scan if the index wasn't built (e.g. empty boundary file).
    Returns (None, None) if the point falls outside all polygons or shapely is unavailable.
    """
    if not _ADMIN_FEATURES or not _SHAPELY_AVAILABLE:
        return None, None
    pt = Point(lon, lat)
    if _ADMIN_TREE is not None:
        # query() returns indices of candidate geometries whose bboxes intersect pt
        for idx in _ADMIN_TREE.query(pt):
            geom, country, region = _ADMIN_FEATURES[idx]
            if geom.contains(pt):
                return country, region
        return None, None
    # Fallback: linear scan (no index built)
    for geom, country, region in _ADMIN_FEATURES:
        if geom.contains(pt):
            return country, region
    return None, None


# ---------------------------------------------------------------------------
# Resume checkpoint (data.resume) + data log (data.log)
# ---------------------------------------------------------------------------
# data.resume  – plain-text checkpoint; one "date/station" key per line.
#                The same command re-run will skip already-completed stations.
#                Delete data.resume to restart from scratch.
#
# data.log     – JSONL event log; one JSON object per line for every station
#                that is skipped due to missing data or missing/bad location.
# ---------------------------------------------------------------------------

def load_last_completed_date(log_path):
    """Return the last successfully completed date (YYYY-MM-DD), or None."""
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8").strip()
    return text if text else None


def load_completed(log_path):
    """Kept for compat — resume is now date-based."""
    return set()


def mark_date_completed(log_path, date_folder):
    """Overwrite data.resume with the last successfully completed date."""
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(date_folder + "\n")


def mark_completed(log_path, date_folder, station_folder):
    """No-op — resume is now date-based."""
    pass


def init_resume_file(log_path, source, start, end):
    pass  # created/overwritten by mark_date_completed


def init_data_log(log_path: Path, source: str, start, end) -> None:
    """Write a header comment to data.log if the file does not yet exist.

    Every station skipped due to missing data or a bad/missing location is
    appended here as a single-line JSON object (JSONL format).
    """
    if not log_path.exists():
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# load_data.py  –  data event log (data.log)\n")
            f.write(f"# source : {source}\n")
            f.write(f"# range  : {start or 'beginning'} -> {end or 'end'}\n")
            f.write(f"# started: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"# Each non-comment line is a JSON object for a skipped station.\n")
            f.write(f"# event types: missing_location, bad_location, missing_data\n")



def mark_data_log_event(
    log_path: Path,
    event: str,
    date_folder: str,
    station_folder: str,
    station_folder_path: str,
    station_id: str,
    reason: str,
    station_data: dict,
) -> None:
    """Append one event to data.log (JSONL).

    event values:
      "missing_location" – station has no location information at all
      "bad_location"     – station has location info but coordinates are unusable
      "missing_data"     – station JSON or sensor CSV could not be read
    """
    location_snapshot = {
        "loc": station_data.get("loc"),
        "geometry": station_data.get("geometry"),
        "coordinates": station_data.get("coordinates"),
        "location": station_data.get("location"),
        "position": station_data.get("position"),
        "lat": station_data.get("lat"),
        "lon": station_data.get("lon"),
        "lng": station_data.get("lng"),
        "latitude": station_data.get("latitude"),
        "longitude": station_data.get("longitude"),
    }
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "date_folder": date_folder,
        "station_folder": station_folder,
        "station_folder_path": station_folder_path,
        "station_id": station_id,
        "reason": reason,
        "location": location_snapshot,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# Backward-compatible alias used internally
def mark_location_error(
    log_path: Path,
    date_folder: str,
    station_folder: str,
    station_folder_path: str,
    station_id: str,
    reason: str,
    station_data: dict,
) -> None:
    """Delegate to mark_data_log_event with the correct event type."""
    if "no location" in reason:
        event = "missing_location"
    else:
        event = "bad_location"
    mark_data_log_event(
        log_path, event,
        date_folder, station_folder, station_folder_path,
        station_id, reason, station_data,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load OpenSenseMap archive into TimescaleDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # -- Two mutually exclusive top-level modes ------------------------------
    # --source and --import-sql are mutually exclusive but neither is strictly
    # required: omitting --source defaults to the official OpenSenseMap archive.
    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument(
        "--source", default=None,
        help=(
            "Local folder or base URL of the archive to process. "
            "Defaults to https://archive.opensensemap.org/ when omitted."
        )
    )
    mode_group.add_argument(
        "--import-sql",
        dest="import_sql",
        metavar="SQL_FILE",
        help=(
            "Path to a previously generated .sql dump file to import into the database. "
            "Requires a working DB connection (.env). "
            "Example: --import-sql sql/2014-06-03_to_2023-12-31.sql"
        )
    )

    # -- Options that apply to --source mode ---------------------------------
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
        help="Preview only -- no DB writes. Outputs tab-separated CSV files."
    )
    parser.add_argument(
        "--type", dest="output_type", default="db", choices=["db", "sql"],
        help=(
            "Output mode: 'db' writes to TimescaleDB (default), "
            "'sql' generates a portable .sql dump in ./sql/ -- no DB connection required."
        )
    )
    parser.add_argument(
        "--workers", type=int, default=WORKER_THREADS,
        help=f"Parallel threads per date folder (default: {WORKER_THREADS}). "
             "Use 1 to disable parallelism."
    )
    parser.add_argument(
        "--part", type=int, default=None, metavar="N",
        help=(
            "Split the SQL dump into N roughly equal parts (only used with --type sql). "
            "Parts are written to a sub-folder ./sql/<name>_parts/ as "
            "<name>_part1ofN.sql, <name>_part2ofN.sql, ... "
            "Import them later with: --import-sql sql/<name>_parts/"
        )
    )

    parser.add_argument(
        "--way", dest="way", default=None, choices=["hybrid"],
        help=(
            "Download strategy. 'hybrid': pre-fetch date folders into .download/ "
            "ahead of processing (lookahead=%d) and delete each folder once "
            "ingested. Requires --source to be a remote URL." % HYBRID_LOOKAHEAD
        )
    )
    parser.add_argument(
        "--method", dest="method", default=None, choices=["separate"],
        help=(
            "Ingest strategy. 'separate': first insert ALL stations and sensors "
            "across every date folder, then do a second full pass to insert all "
            "readings. Ensures FK constraints are satisfied before any reading "
            "is written and allows the readings pass to run with parallelism."
        )
    )

    # -- Shared options ------------------------------------------------------
    parser.add_argument(
        "--env", default=".env",
        help="Path to .env file (default: .env)"
    )
    parser.add_argument(
        "--admin-boundary", dest="admin_boundary", default=None, metavar="PATH",
        help=(
            "Path to admin_boundary.geojson used for country/region lookup. "
            f"Defaults to the hard-coded Windows path in ADMIN_BOUNDARY_PATH constant."
        )
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# .env / DB connection
# ---------------------------------------------------------------------------
def load_env(env_path: str) -> None:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        pass  # env loaded silently
    else:
        print(f"[WARN] .env not found at '{env_path}', using shell environment")


def get_db_connection(verbose: bool = True):
    # POSTGRES_HOST defaults to localhost -- correct when Docker maps the port
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
        pass  # connection confirmed
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
# Source abstraction -- local or HTTP
# ---------------------------------------------------------------------------
def is_url(source: str) -> bool:
    return urlparse(source).scheme in ("http", "https")


def list_local_subdirs(folder: Path) -> list[str]:
    return sorted(
        d.name for d in folder.iterdir()
        if d.is_dir()
    )


def list_url_subdirs(base_url: str) -> list[str]:
    """Parse an Apache/Nginx directory listing for subdirectory hrefs.

    Filters out:
    - Absolute URLs (contain "://"), e.g. https://caddyserver.com injected by
      the server's navigation/footer HTML — these would become invalid path
      components on Windows and cause OSError [WinError 123].
    - Parent/query/fragment hrefs (starting with ?, /, ., #).
    - Any href containing path separators (\\) or colons (:) which are illegal
      on Windows file systems.
    """
    resp = _SESSION.get(base_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    dirs = []
    for a in soup.find_all("a", href=True):
        href = a["href"].rstrip("/")
        if href.startswith("./"):
            href = href[2:]
        # Skip empty, navigation hrefs, and absolute URLs
        if not href:
            continue
        if href.startswith(("?", "/", ".", "#")):
            continue
        # Reject anything that contains "://" (absolute URL) or a colon (Windows-illegal)
        if "://" in href or ":" in href:
            continue
        # Reject anything with backslashes or forward-slash (not a simple name)
        if "\\" in href or "/" in href:
            continue
        dirs.append(href)
    return sorted(set(dirs))


def read_local_file(path: Path) -> Optional[bytes]:
    if path.exists():
        return path.read_bytes()
    return None


def read_url_file(url: str) -> Optional[bytes]:
    try:
        resp = _SESSION.get(url, timeout=30)
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
        tqdm.write(f"  [WARN] Could not parse JSON: {e}")
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
        tqdm.write(f"  [WARN] Could not parse CSV: {e}")
    return rows


def _as_lon_lat(value) -> tuple[Optional[float], Optional[float]]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[0], value[1]
    return None, None


def station_location_info(station_data: dict) -> tuple[bool, Optional[float], Optional[float]]:
    """Return (has_any_location_info, lon, lat) for a station payload."""
    loc = station_data.get("loc")
    if isinstance(loc, dict):
        geometry = loc.get("geometry")
        if isinstance(geometry, dict):
            lon, lat = _as_lon_lat(geometry.get("coordinates"))
            if lon is not None and lat is not None:
                return True, lon, lat

        lon, lat = _as_lon_lat(loc.get("coordinates"))
        if lon is not None and lat is not None:
            return True, lon, lat

    for candidate in (
        station_data.get("geometry"),
        station_data.get("coordinates"),
        station_data.get("location"),
        station_data.get("position"),
    ):
        if isinstance(candidate, dict):
            if candidate.get("coordinates"):
                lon, lat = _as_lon_lat(candidate.get("coordinates"))
            elif candidate.get("lon") is not None and candidate.get("lat") is not None:
                lon, lat = candidate.get("lon"), candidate.get("lat")
            elif candidate.get("lng") is not None and candidate.get("lat") is not None:
                lon, lat = candidate.get("lng"), candidate.get("lat")
            elif candidate.get("longitude") is not None and candidate.get("latitude") is not None:
                lon, lat = candidate.get("longitude"), candidate.get("latitude")
            else:
                lon = lat = None
        else:
            lon, lat = _as_lon_lat(candidate)

        if lon is not None and lat is not None:
            return True, lon, lat

    has_any_location_info = any(
        value is not None and value != ""
        for value in (
            station_data.get("loc"),
            station_data.get("geometry"),
            station_data.get("coordinates"),
            station_data.get("location"),
            station_data.get("position"),
            station_data.get("lat"),
            station_data.get("lon"),
            station_data.get("lng"),
            station_data.get("latitude"),
            station_data.get("longitude"),
        )
    )
    return has_any_location_info, None, None


def get_existing_station_location(cur, st_id: str) -> tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """Return stored (lon, lat, country, region) for an existing station id."""
    cur.execute(
        """
        SELECT
            ST_X(geometry) AS lon,
            ST_Y(geometry) AS lat,
            country,
            region
        FROM stations
        WHERE st_id = %s
        """,
        (st_id,),
    )
    row = cur.fetchone()
    if not row:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


# ---------------------------------------------------------------------------
# DB upsert helpers
# ---------------------------------------------------------------------------
def upsert_station(cur, data: dict) -> tuple[Optional[str], bool, Optional[str], Optional[str]]:
    """Insert or update a station row.

    The schema uses st_id VARCHAR(24) as the primary key.
    Returns (st_id, is_new, country, skip_reason).
    """
    station_id = data.get("id")
    has_location_info, lon, lat = station_location_info(data)
    country = region = None
    skip_reason = None

    if lon is None or lat is None:
        existing_lon, existing_lat, existing_country, existing_region = get_existing_station_location(cur, station_id)
        if existing_lon is not None and existing_lat is not None:
            lon, lat = existing_lon, existing_lat
            country, region = existing_country, existing_region
        else:
            if has_location_info:
                skip_reason = "location info but no usable coordinates"
                print(f"  [WARN] Station {station_id} has location info but no usable coordinates, skipping")
            else:
                skip_reason = "no location information"
                print(f"  [WARN] Station {station_id} has no location information, skipping")
            return None, False, None, skip_reason

    if country is None or region is None:
        country, region = lookup_admin(lon, lat)

    cur.execute("""
        INSERT INTO stations (st_id, boxtype, exposure, model, geometry, region, country)
        VALUES (
            %s, %s, %s, %s,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326),
            %s, %s
        )
        ON CONFLICT (st_id) DO UPDATE
            SET boxtype  = EXCLUDED.boxtype,
                exposure = EXCLUDED.exposure,
                model    = EXCLUDED.model,
                geometry = EXCLUDED.geometry,
                country  = EXCLUDED.country,
                region   = EXCLUDED.region
        RETURNING st_id, (xmax = 0) AS is_new, country
    """, (
        station_id,
        data.get("boxType"),
        data.get("exposure"),
        data.get("model"),
        lon, lat,
        region,
        country,
    ))
    row = cur.fetchone()
    if not row:
        return None, False, None, None
    st_id_ret, is_new, stored_country = row
    return st_id_ret, is_new, stored_country, None


def classify_sensor(title: str, unit: str) -> str:
    """Return a canonical sensor category for a given (title, unit) pair.

    Rules are evaluated in order; the first match wins.
    Falls back to "Other" when nothing matches.
    """
    t = (title or "").lower()
    u = (unit  or "").lower()

    # ------------------------------------------------------------------ #
    # 1.  Temperature                                                      #
    # ------------------------------------------------------------------ #
    # Water-temperature titles are handled in the Water section (rule 13)
    if any(kw in t for kw in (
        "wassertemperatur", "water temperature", "wassertemp",
        "temperatur wasser",
    )):
        return "Water"

    if any(kw in t for kw in (
        "temperatur", "temperature", "temp", "wärme", "thermometer",
        "taupunkt", "dew point", "dewpoint", "heat index", "hitzeindex",
        "windchill", "kühlgrenz", "gefühlte temperatur",
    )) or u in ("°c", "c", "c°", "°c", "celsius", "celcius", "grad celsius",
                "degree celcius", "deg. c", "degree c", "centigrade",
                "k", "°f", "f", "˚c", "ºc", "*c", "oC"):
        # Exclude humidity sensors whose title happens to contain "temp"
        if not any(bad in t for bad in ("luftfeuchte", "humidity", "feuchte",
                                         "feuchtigkeit", "moisture")):
            return "Temperature"

    # ------------------------------------------------------------------ #
    # 2.  Humidity                                                         #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "luftfeuchte", "luftfeuchtigkeit", "feuchte", "feuchtigkeit",
        "humidity", "humedad", "humidité", "hygrométrie", "moisture",
        "wilgotność", "kosteus", "ilmankosteus", "umidità", "vochtigheid",
        "humidade", "páratartalom", "ilmankosteus",
    )) or u in ("%", "%rh", "% rh", "%rh", "rh", "rh%", "rel. h. %",
                "rel.h.%", "percent", "percent rh", "%rel", "% rel. f.",
                "relh", "rel. feuchte", "relative humidity",):
        if "boden" not in t and "soil" not in t:   # soil moisture handled separately
            return "Humidity"

    # ------------------------------------------------------------------ #
    # 3.  Air Pressure                                                     #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "luftdruck", "druck", "pressure", "pression", "pressione",
        "ciśnienie", "давление", "barometric", "barometer", "atm",
        "baro", "luchtdruk", "luftdruk", "lufdruck", "luftfdruck",
        "luftdruck", "pression", "pressão",
    )) or u in ("hpa", "pa", "mbar", "bar", "pascal", "millibar",
                "hectopascal", "kpa", "mmhg", "inhg"):
        return "Air Pressure"

    # ------------------------------------------------------------------ #
    # 9b. Gas (check BEFORE particulate matter so NO2 µg/m³ is Gas)       #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "no2", "stickoxid", "stickstoffdioxid", "nitrogen dioxide",
        "nh3", "ammoniak", "ammonia",
        "o3", "ozon", "ozone",
        " co ", "kohlenmonoxid", "carbon monoxide",
        "nox", "h2s", "ch4", "methan", "methane",
        "ethanol", "c2h5oh", "c3h8", "c4h10", "lpg",
    )) and not any(kw in t for kw in ("pm", "feinstaub", "dust", "particle")):
        return "Gas"

    # ------------------------------------------------------------------ #
    # 4.  Particulate Matter / Fine Dust                                   #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "feinstaub", "feinstaubkonzentration", "particulate", "particle",
        "partikel", "pm10", "pm2.5", "pm2,5", "pm 10", "pm 2.5",
        "pm1", "pm4", "staub", "dust", "pył", "pienhiukkaset",
        "polveri", "prахові", "частицы", "luftverschmutzung",
        "sds", "sps30", "sds011", "sps 30",
    )) or any(kw in u for kw in ("µg/m", "ug/m", "μg/m", "µg/m³",
                                   "pcs/", "particles")):
        return "Particulate Matter"

    # ------------------------------------------------------------------ #
    # 5.  UV Radiation                                                     #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "uv", "ultraviolet", "ultraviolett", "uv-intensität", "uv-index",
        "uv-strahlung", "uv-licht", "uva", "uvb", "uvc",
    )):
        return "UV Radiation"

    # ------------------------------------------------------------------ #
    # 6.  Illuminance / Light                                              #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "beleuchtungsstärke", "beleuchtung", "helligkeit", "licht",
        "illuminance", "illumination", "luminosity", "luminance",
        "lichtintensität", "lux", "lichtstärke", "lichtstaerke",
        "dämmerung", "sonnenstrahlung", "einstrahlung", "valaistusvoimakkuus",
        "valaistuksen", "valoisuus", "light intensity", "ambient light",
        "day light",
    )) or u in ("lux", "lx", "klx"):
        return "Illuminance"

    # ------------------------------------------------------------------ #
    # 7b. Signal Strength (check BEFORE noise so 'WiFi' isn't caught by dB)#
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "wifi", "wlan", "rssi", "signalstärke", "wi-fi",
        "wifistärke", "wifi-stärke", "wifi signal", "signalstärke",
        "signal strength",
    )) or u in ("dbm", "rssi"):
        return "Signal Strength"

    # ------------------------------------------------------------------ #
    # 7.  Noise / Sound                                                    #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "schall", "lärm", "lautstärke", "noise", "sound", "geräusch",
        "umgebungslautstärke", "lämpötila", "laerm", "laerm", "akustisch",
        "loudness", "lärmpegel", "geräuschpegel", "dnms",
    )) or any(kw in u for kw in ("db", "dba", "dbc", "dbz", "schallpegel",
                                    "dezibel", "pegel")):
        return "Noise"

    # ------------------------------------------------------------------ #
    # 8.  Precipitation / Rain                                             #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "niederschlag", "regen", "precipitation", "rain", "pluie",
        "pioggia", "opady", "regenrate", "regenintensität",
        "niederschlagsmenge", "regensensor", "regenindikator",
    )):
        return "Precipitation"

    # ------------------------------------------------------------------ #
    # 9.  Wind                                                             #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "wind", "windgeschwindigkeit", "windrichtung", "windstärke",
        "windböen", "böengeschwindigkeit", "windböe", "böe",
        "windspeed", "winddirection", "windchill",
    )):
        return "Wind"

    # ------------------------------------------------------------------ #
    # 10. CO2 / Air Quality Gases                                          #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "co2", "co₂", "c02", "kohlendioxid", "kohlenstoffdioxid",
        "carbon dioxide", "co2-konzentration",
        "eco2", "co2eq", "co2äquivalent",
    )):
        return "CO2"

    if any(kw in t for kw in (
        "voc", "tvoc", "volatile organic", "luftqualität", "luftgüte",
        "air quality", "air-quality", "airquality", "iaq",
        "indoor air quality", "innenraumluftqualität",
        "gas resistance", "luftwiderstand",
    )):
        return "Air Quality / VOC"

    # ------------------------------------------------------------------ #
    # 11. Soil                                                             #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "bodenfeuchte", "bodenfeuchtigkeit", "bodentemperatur",
        "boden", "soil", "ground temperature", "soil moisture",
        "soil temperature", "bodenfeucht",
    )):
        return "Soil"

    # ------------------------------------------------------------------ #
    # 12. Radiation / Radioactivity                                        #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "radioaktiv", "radioactiv", "radiation", "strahlung",
        "gammastrahlung", "gamma", "strahlenbelastung",
        "dosisleistung", "ortsdosisleistung", "ionizing",
    )) or any(kw in u for kw in ("sv/h", "cpm", "μr/h", "µr/h",
                                   "msv", "nsv", "usv")):
        return "Radiation"

    # ------------------------------------------------------------------ #
    # 13. Water                                                            #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "wassertemperatur", "water temperature", "wasserstand",
        "water level", "füllstand", "pegel",
        "leitfähigkeit", "conductivity", "leitwert",
        "trübung", "turbidity", "ph-wert", "ph wert",
        "wasserpegel",
    )):
        return "Water"

    # ------------------------------------------------------------------ #
    # 14. Power / Energy / Solar                                           #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "spannung", "voltage", "batterie", "battery", "akku", "akkusp",
        "solar", "leistung", "power", "energie", "energy",
        "strom", "current", "ladestrom", "solarstrom",
        "watt", "kwh", "pgn", "ugn", "pdc", "solarspannung",
        "eingangsspannung", "versorgungsspannung",
        "netz", "grid", "pv", "photovoltaic",
    )) or u in ("v", "mv", "a", "ma", "w", "kw", "kwh", "wh", "volt",
                "volts", "ampere"):
        return "Power / Energy"

    # ------------------------------------------------------------------ #
    # 15. GPS / Location                                                   #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "gps", "latitude", "longitude", "altitude", "breite", "länge",
        "breitengrad", "längengrad", "koordinaten", "höhe",
    )):
        return "GPS / Location"

    # ------------------------------------------------------------------ #
    # 16. People / Traffic Counting                                        #
    # ------------------------------------------------------------------ #
    if any(kw in t for kw in (
        "personen", "personenanzahl", "people", "besucher", "besucherzahl",
        "pax", "fahrzeug", "verkehr", "traffic", "cars", "fahrrad",
        "number of people", "attendance", "presence",
    )):
        return "Counting"

    return "Other"


def upsert_sensor(cur, sensor: dict, st_id: str) -> tuple[Optional[str], bool]:
    """Insert or update a sensor row.

    The schema uses se_id VARCHAR(24) as the primary key and st_id VARCHAR(24)
    as the FK to stations.  sensor_type is the raw sensorType from the JSON;
    category is derived from sensor_types.csv (with classify_sensor fallback).

    Returns (se_id, is_new) where is_new is True when freshly inserted.
    """
    title       = sensor.get("title", "")
    unit        = sensor.get("unit", "")
    sensor_type = sensor.get("sensorType") or None
    category    = get_sensor_category(title, unit)

    cur.execute("""
        INSERT INTO sensors (se_id, st_id, title, unit, sensor_type, category)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (se_id) DO UPDATE
            SET title       = EXCLUDED.title,
                unit        = EXCLUDED.unit,
                sensor_type = EXCLUDED.sensor_type,
                category    = EXCLUDED.category
        RETURNING se_id, (xmax = 0) AS is_new
    """, (
        sensor.get("id"),
        st_id,
        title or None,
        unit or None,
        sensor_type,
        category,
    ))
    row = cur.fetchone()
    if not row:
        return None, False
    se_id_ret, is_new = row
    return se_id_ret, is_new


def insert_readings(cur, se_id: str, st_id: str, rows: list[dict]) -> int:
    """Bulk-insert readings, skip malformed rows. Returns count inserted.

    Uses se_id and st_id as FKs — matching the schema natural primary keys.
    The unique index is (se_id, time); ON CONFLICT DO NOTHING deduplicates on
    re-runs (e.g. after a crash and resume).
    """
    records = []
    for row in rows:
        try:
            t = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
            v = float(row["value"])
            records.append((se_id, st_id, t, v))
        except (KeyError, ValueError):
            continue

    if not records:
        return 0

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO readings (se_id, st_id, time, value) VALUES %s
        ON CONFLICT (se_id, time) DO NOTHING
        """,
        records,
        page_size=2000,
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
        "stations": ["st_id", "boxtype", "model", "lon", "lat", "region", "country"],
        "sensors":  ["se_id", "st_id", "title", "unit", "sensor_type", "category"],
        "readings": ["se_id", "st_id", "time", "value"],
    }
    for name, rows in buffers.items():
        out_path = DRY_RUN_DIR / f"{name}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(headers[name])
            writer.writerows(rows)
        print(f"  [DRY-RUN] Written {len(rows):,} rows -> {out_path}")


# ---------------------------------------------------------------------------
# SQL dump mode  (--type sql)
# ---------------------------------------------------------------------------

# DDL used to create the tables in the dump (mirrors TimescaleDB schema,
# but without the hypertable call so the file is also importable into plain
# PostgreSQL for experimentation).
_SQL_SCHEMA = """-- ============================================================
--  OpenSenseMap archive dump
--  Generated by load_data.py --type sql
--  Schema mirrors schema.sql: st_id/se_id are the natural VARCHAR(24) PKs.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS stations (
    st_id        VARCHAR(24) NOT NULL,
    boxtype      TEXT,
    exposure     TEXT,
    model        TEXT,
    geometry     GEOMETRY(Point, 4326) NOT NULL,
    region       TEXT,
    country      TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_stations PRIMARY KEY (st_id),
    CONSTRAINT uq_stations_id UNIQUE (st_id)
);

CREATE TABLE IF NOT EXISTS sensors (
    se_id        VARCHAR(24) NOT NULL,
    st_id        VARCHAR(24) NOT NULL,
    title        TEXT,
    unit         TEXT,
    sensor_type  TEXT,
    category     TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_sensors PRIMARY KEY (se_id),
    CONSTRAINT uq_sensors UNIQUE (se_id),
    CONSTRAINT fk_sensors_st_id
        FOREIGN KEY (st_id) REFERENCES stations (st_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS readings (
    se_id        VARCHAR(24) NOT NULL,
    st_id        VARCHAR(24) NOT NULL,
    time         TIMESTAMPTZ NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_readings_se_id
        FOREIGN KEY (se_id) REFERENCES sensors (se_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_readings_st_id
        FOREIGN KEY (st_id) REFERENCES stations (st_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_readings ON readings (se_id, time);

"""



def _sql_literal(v) -> str:
    """Escape a Python value to a safe SQL literal string."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    # Dates / datetimes -> cast as text
    return "'" + str(v).replace("'", "''") + "'"


def init_sql_dump() -> dict:
    """Create the sql/ output folder and return an empty buffer dict."""
    SQL_DUMP_DIR.mkdir(exist_ok=True)
    return {"stations": [], "sensors": [], "readings": []}


def _build_base_name(start, end) -> str:
    """Return the stem used for SQL dump filenames (without .sql extension)."""
    if start and end:
        return f"{start}_to_{end}"
    elif start:
        return f"{start}_to_end"
    elif end:
        return f"start_to_{end}"
    else:
        return "osem_full"


def _write_sql_file(
    out_path: Path,
    station_rows: list,
    sensor_rows: list,
    reading_rows: list,
    include_schema: bool = True,
) -> None:
    """Write a single SQL file containing stations, sensors, and readings.

    Stations are keyed by st_id (natural VARCHAR primary key); sensors reference
    stations via st_id directly; readings reference sensors via se_id and st_id.
    """
    BATCH = 5_000

    with open(out_path, "w", encoding="utf-8") as f:
        if include_schema:
            f.write(_SQL_SCHEMA)

        # -- Stations --------------------------------------------------------
        if station_rows:
            f.write("-- stations\n")
            f.write("BEGIN;\n")
            for row in station_rows:
                st_id, boxtype, model, lon, lat, region, country = row
                geom = (
                    f"ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)"
                    if lon is not None and lat is not None
                    else "NULL"
                )
                f.write(
                    f"INSERT INTO stations (st_id, boxtype, model, geometry, region, country) "
                    f"VALUES ({_sql_literal(st_id)}, {_sql_literal(boxtype)}, {_sql_literal(model)}, "
                    f"{geom}, {_sql_literal(region)}, {_sql_literal(country)}) "
                    f"ON CONFLICT (st_id) DO NOTHING;\n"
                )
            f.write("COMMIT;\n\n")

        # -- Sensors ---------------------------------------------------------
        # FK st_id resolved at import time (it's the natural PK).
        if sensor_rows:
            f.write("-- sensors\n")
            f.write("BEGIN;\n")
            for row in sensor_rows:
                se_id, st_id, title, unit, sensor_type, category = row
                f.write(
                    f"INSERT INTO sensors (se_id, st_id, title, unit, sensor_type, category) "
                    f"VALUES ({_sql_literal(se_id)}, {_sql_literal(st_id)}, {_sql_literal(title)}, "
                    f"{_sql_literal(unit)}, {_sql_literal(sensor_type)}, {_sql_literal(category)}) "
                    f"ON CONFLICT (se_id) DO NOTHING;\n"
                )
            f.write("COMMIT;\n\n")

        # -- Readings -- batched transactions of 5 000 rows ------------------
        if reading_rows:
            f.write("-- readings\n")
            for batch_start in range(0, len(reading_rows), BATCH):
                f.write("BEGIN;\n")
                for row in reading_rows[batch_start : batch_start + BATCH]:
                    se_id, st_id, time_str, value = row
                    f.write(
                        f"INSERT INTO readings (se_id, st_id, time, value) "
                        f"VALUES ({_sql_literal(se_id)}, {_sql_literal(st_id)}, "
                        f"{_sql_literal(time_str)}, {_sql_literal(value)}) "
                        f"ON CONFLICT (se_id, time) DO NOTHING;\n"
                    )
                f.write("COMMIT;\n\n")



def flush_sql_dump(buffers: dict, start, end, num_parts: Optional[int] = None) -> Path:
    """Write the SQL dump to disk.

    When num_parts is None (default) a single .sql file is written to sql/.
    When num_parts >= 2 the data is split into that many roughly equal parts:
      - All stations and sensors are written to part 1 (they must precede readings).
      - Readings are divided evenly across all parts.
      - Parts are placed in sql/<base_name>_parts/<base_name>_part1ofN.sql etc.
      - The schema DDL is only included in part 1.

    Returns the path of the single file written, or the parts sub-folder.
    """
    base_name    = _build_base_name(start, end)
    station_rows = buffers["stations"]   # [st_id, boxtype, model, lon, lat, region, country]
    sensor_rows  = buffers["sensors"]    # [se_id, st_id, title, unit, sensor_type, category]
    reading_rows = buffers["readings"]   # [se_id, st_id, time, value]

    # ------------------------------------------------------------------ single file
    if not num_parts or num_parts <= 1:
        out_path = SQL_DUMP_DIR / f"{base_name}.sql"
        _write_sql_file(
            out_path,
            station_rows, sensor_rows, reading_rows,
            include_schema=True,
        )
        print(
            f"  [SQL] Written {len(station_rows):,} stations, {len(sensor_rows):,} sensors, "
            f"{len(reading_rows):,} readings -> {out_path}"
        )
        return out_path

    # ------------------------------------------------------------------ split into N parts
    parts_dir = SQL_DUMP_DIR / f"{base_name}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    # Divide reading rows into num_parts slices (last slice may be slightly smaller)
    total_readings   = len(reading_rows)
    chunk_size       = max(1, (total_readings + num_parts - 1) // num_parts)
    reading_chunks   = [
        reading_rows[i : i + chunk_size]
        for i in range(0, max(total_readings, 1), chunk_size)
    ]
    # Ensure we always produce exactly num_parts files (some chunks may be empty)
    while len(reading_chunks) < num_parts:
        reading_chunks.append([])
    reading_chunks = reading_chunks[:num_parts]

    written_paths: list[Path] = []
    for part_idx, r_chunk in enumerate(reading_chunks, start=1):
        part_path = parts_dir / f"{base_name}_part{part_idx}of{num_parts}.sql"

        # Stations + sensors only in part 1; subsequent parts contain readings only
        s_rows = station_rows if part_idx == 1 else []
        se_rows = sensor_rows if part_idx == 1 else []

        _write_sql_file(
            part_path,
            s_rows, se_rows, r_chunk,
            include_schema=(part_idx == 1),
        )

        r_count = len(r_chunk)
        print(
            f"  [SQL] Part {part_idx}/{num_parts}: "
            + (f"{len(s_rows):,} stations, {len(se_rows):,} sensors, " if part_idx == 1 else "")
            + f"{r_count:,} readings -> {part_path}"
        )
        written_paths.append(part_path)

    print(
        f"  [SQL] Total: {len(station_rows):,} stations, {len(sensor_rows):,} sensors, "
        f"{total_readings:,} readings split across {num_parts} parts in {parts_dir}/"
    )
    print(f"  [SQL] To import all parts run:")
    print(f"         python load_data.py --import-sql {parts_dir}/")
    return parts_dir


# ---------------------------------------------------------------------------
# SQL import mode  (--import-sql)
# ---------------------------------------------------------------------------
def _import_single_sql_file(sql_path: Path, conn) -> tuple[int, int]:
    """Stream and execute one .sql file. Returns (statements_executed, rows_affected)."""
    file_size = sql_path.stat().st_size
    print(f"[IMPORT] File : {sql_path}  ({file_size / (1024 * 1024):.1f} MB)")

    statement_buffer: list[str] = []
    total_statements   = 0
    total_rows_affected = 0

    with conn.cursor() as cur:
        with (
            open(sql_path, "r", encoding="utf-8") as fh,
            tqdm(
                total=file_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"  {sql_path.name}",
            ) as pbar,
        ):
            for line in fh:
                pbar.update(len(line.encode("utf-8")))
                stripped = line.strip()

                # Skip blank lines and pure comment lines
                if not stripped or stripped.startswith("--"):
                    continue

                statement_buffer.append(line)

                # A statement ends when the stripped line ends with ";"
                if stripped.endswith(";"):
                    stmt = "".join(statement_buffer).strip()
                    statement_buffer = []
                    if not stmt:
                        continue
                    try:
                        cur.execute(stmt)
                        total_statements += 1
                        if cur.rowcount and cur.rowcount > 0:
                            total_rows_affected += cur.rowcount
                    except psycopg2.Error as e:
                        conn.rollback()
                        print(f"\n[ERROR] Statement failed:\n  {stmt[:300]}\n  {e}")
                        print("[HINT]  Rolling back and aborting import.")
                        conn.close()
                        sys.exit(1)

    conn.commit()
    return total_statements, total_rows_affected


def import_sql_file(sql_path: Path, env_path: str) -> None:
    """Import a .sql dump (or a folder of split parts) into the database.

    Accepts two forms:
      - A single .sql file   -> imported directly.
      - A directory          -> all .sql files inside (sorted) are imported in order.
        This matches the folder written by flush_sql_dump when --part N is used.

    The file(s) are streamed line-by-line so even multi-GB dumps won't exhaust memory.
    """
    load_env(env_path)
    conn = get_db_connection()

    # -- Resolve the list of files to import ---------------------------------
    if sql_path.is_dir():
        sql_files = sorted(sql_path.glob("*.sql"))
        if not sql_files:
            print(f"[ERROR] No .sql files found in directory: {sql_path}")
            sys.exit(1)
        print(f"[IMPORT] Directory : {sql_path}  ({len(sql_files)} part(s) found)")
    elif sql_path.is_file():
        sql_files = [sql_path]
    else:
        print(f"[ERROR] Path not found: {sql_path}")
        sys.exit(1)

    print("[IMPORT] Executing SQL statements...")

    total_statements    = 0
    total_rows_affected = 0

    for i, part_path in enumerate(sql_files, start=1):
        if len(sql_files) > 1:
            print(f"\n[IMPORT] Part {i}/{len(sql_files)}: {part_path.name}")
        stmts, rows = _import_single_sql_file(part_path, conn)
        total_statements    += stmts
        total_rows_affected += rows

    print("\n" + "=" * 50)
    print("  Import complete")
    print("=" * 50)
    print(f"  Files imported      : {len(sql_files):>10,}")
    print(f"  Statements executed : {total_statements:>10,}")
    print(f"  Rows affected       : {total_rows_affected:>10,}")
    print("=" * 50)

    conn.close()



# ---------------------------------------------------------------------------
# Hybrid mode helpers  (--hybrid)
# ---------------------------------------------------------------------------
def _download_date_folder(source: str, date_folder: str, dest_root: Path) -> Path:
    """Download all files for one date folder from a remote source into
    dest_root/<date_folder>/ and return the local path.

    Each station subfolder and its files are fetched:
      <source>/<date_folder>/<station>/  ->  dest_root/<date_folder>/<station>/
    """
    date_dest = dest_root / date_folder
    date_dest.mkdir(parents=True, exist_ok=True)

    date_url      = urljoin(source.rstrip("/") + "/", date_folder + "/")
    station_names = list_url_subdirs(date_url)

    for station_name in station_names:
        st_dest = date_dest / station_name
        st_dest.mkdir(parents=True, exist_ok=True)

        st_url   = urljoin(date_url.rstrip("/") + "/", station_name + "/")
        listing  = read_url_file(st_url)
        if not listing:
            continue

        soup  = BeautifulSoup(listing.decode("utf-8"), "html.parser")
        files = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("./"):
                href = href[2:]
            if href and not href.startswith(("?", "/", "..", "#")):
                files.append(href)

        for fname in files:
            if not (fname.endswith(".json") or fname.endswith(".csv")):
                continue
            dest_file = st_dest / fname
            if dest_file.exists():
                continue   # already fetched (e.g. partial re-run)
            data = read_url_file(urljoin(st_url.rstrip("/") + "/", fname))
            if data:
                dest_file.write_bytes(data)

    return date_dest


def _download_station_json_files(
    source: str, date_folder: str, station_names: list, dest_root: Path
) -> None:
    """Download only the .json files for a list of station folders.

    Used in --method separate hybrid mode so the metadata pass can start as
    soon as the first batch of JSONs arrives, without waiting for all CSVs.
    """
    date_url  = urljoin(source.rstrip("/") + "/", date_folder + "/")
    date_dest = dest_root / date_folder

    for station_name in station_names:
        st_dest = date_dest / station_name
        st_dest.mkdir(parents=True, exist_ok=True)

        st_url  = urljoin(date_url.rstrip("/") + "/", station_name + "/")
        listing = read_url_file(st_url)
        if not listing:
            continue

        soup = BeautifulSoup(listing.decode("utf-8"), "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("./"):
                href = href[2:]
            if not href or href.startswith(("?", "/", "..", "#")):
                continue
            if "://" in href or ":" in href or "\\" in href or "/" in href:
                continue
            if not href.endswith(".json"):
                continue
            dest_file = st_dest / href
            if dest_file.exists():
                continue
            data = read_url_file(urljoin(st_url.rstrip("/") + "/", href))
            if data:
                dest_file.write_bytes(data)


def _download_station_csv_files(
    source: str, date_folder: str, station_names: list, dest_root: Path
) -> None:
    """Download only the .csv files for a list of station folders.

    Called after the JSON/metadata batch has been inserted into the DB so that
    readings can be ingested immediately, then the batch deleted to free disk.
    """
    date_url  = urljoin(source.rstrip("/") + "/", date_folder + "/")
    date_dest = dest_root / date_folder

    for station_name in station_names:
        st_dest = date_dest / station_name
        st_dest.mkdir(parents=True, exist_ok=True)

        st_url  = urljoin(date_url.rstrip("/") + "/", station_name + "/")
        listing = read_url_file(st_url)
        if not listing:
            continue

        soup = BeautifulSoup(listing.decode("utf-8"), "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("./"):
                href = href[2:]
            if not href or href.startswith(("?", "/", "..", "#")):
                continue
            if "://" in href or ":" in href or "\\" in href or "/" in href:
                continue
            if not href.endswith(".csv"):
                continue
            dest_file = st_dest / href
            if dest_file.exists():
                continue
            data = read_url_file(urljoin(st_url.rstrip("/") + "/", href))
            if data:
                dest_file.write_bytes(data)


def _delete_station_batch(date_dest: Path, station_names: list) -> None:
    """Delete a batch of station subdirectories from a date folder on disk."""
    import shutil
    for station_name in station_names:
        st_path = date_dest / station_name
        try:
            shutil.rmtree(st_path)
        except Exception as exc:
            tqdm.write(f"  [WARN] Could not delete {st_path}: {exc}")


def _delete_date_folder(date_dest: Path) -> None:
    """Recursively delete a downloaded date folder after successful processing."""
    import shutil
    try:
        shutil.rmtree(date_dest)
    except Exception as exc:
        tqdm.write(f"  [WARN] Could not delete {date_dest}: {exc}")


def run_hybrid(
    source: str,
    date_folders: list,
    conn,
    dry_buffers,
    sql_buffers,
    workers: int,
    log_path: Path,
    location_error_path: Path,
    total: dict,
    run_start_time: float,
    separate: bool = False,
) -> None:
    """--way hybrid main loop.

    Background threads pre-fetch the next HYBRID_LOOKAHEAD date folders from
    the remote source into .download/<date>/ while the current day is being
    processed.  All DB work is done against the *local* copy so there are no
    per-file HTTP round-trips during ingestion.

    With --method separate the metadata (station+sensor) pass runs first for
    every day; the readings pass follows after all metadata is committed.
    """
    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor as _TPE, Future

    # ── ANSI colour helpers ─────────────────────────────────────────────────
    R  = "\033[0m"          # reset
    BOLD   = "\033[1m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    DIM    = "\033[2m"

    def clr(text, *codes): return "".join(codes) + str(text) + R

    # ── setup ───────────────────────────────────────────────────────────────
    dest_root  = DOWNLOAD_DIR
    dest_root.mkdir(parents=True, exist_ok=True)

    day_total  = len(date_folders)
    lookahead  = HYBRID_LOOKAHEAD

    # Populated during metadata pass when separate=True so readings pass
    # knows which folders+stations to revisit.
    _meta_done: list = []   # [(date_folder, local_date_path, station_folders)]

    # ── download helpers ────────────────────────────────────────────────────
    fetch_executor   = _TPE(max_workers=lookahead, thread_name_prefix="dl")
    pending_fetches: dict[str, Future] = {}
    dl_status: dict[str, str] = {}   # date_folder -> "queued"|"downloading"|"ready"

    def _submit_fetch(df: str) -> None:
        if df not in pending_fetches:
            dl_status[df] = "downloading"
            pending_fetches[df] = fetch_executor.submit(
                _download_date_folder, source, df, dest_root
            )

    for df in date_folders[:lookahead]:
        if not separate:
            _submit_fetch(df)

    # ── merge helper ────────────────────────────────────────────────────────
    def _merge(counts: dict) -> None:
        total["stations"]    += 1
        total["sensors"]     += counts["sensors"]
        total["readings"]    += counts["readings"]
        total["skipped_csv"] += counts["skipped_csv"]
        total["skipped_no_location"]  += counts["skipped_no_location"]
        total["skipped_bad_location"] += counts["skipped_bad_location"]
        for yr, cnt in counts["new_sensors_by_year"].items():
            total["new_sensors_by_year"][yr] = total["new_sensors_by_year"].get(yr, 0) + cnt
        for ctry, cnt in counts["new_stations_by_country"].items():
            total["new_stations_by_country"][ctry] = (
                total["new_stations_by_country"].get(ctry, 0) + cnt
            )

    # ── status line ─────────────────────────────────────────────────────────
    def _status(phase: str, date_folder: str, day_idx: int,
                st_done: int, st_total: int, skipped: int) -> None:
        elapsed = _time.monotonic() - run_start_time
        h, rem  = divmod(int(elapsed), 3600)
        m, s    = divmod(rem, 60)

        phase_col = {
            "FULL":  GREEN,
            "META":  CYAN,
            "RDGS":  MAGENTA,
        }.get(phase, CYAN)

        tqdm.write(
            clr(f" ✔ [{h:02d}:{m:02d}:{s:02d}] ", BOLD, GREEN) +
            clr(f"[{phase}]", BOLD, phase_col) +
            clr(f"  Day {day_idx:>4}/{day_total}", BOLD) +
            clr(f"  {date_folder}", CYAN) +
            clr(f"  stations {st_done:>4}/{st_total:<4}", BLUE) +
            clr(f"  skipped {skipped:>3}", YELLOW if skipped else DIM) +
            clr("  ▶ ", DIM) +
            clr(f"st:{total['stations']:,}", GREEN) +
            clr("  ", DIM) +
            clr(f"se:{total['sensors']:,}", CYAN) +
            clr("  ", DIM) +
            clr(f"rd:{total['readings']:,}", MAGENTA)
        )

    def _warn(msg: str) -> None:
        tqdm.write(clr(f"  ⚠  {msg}", YELLOW))

    def _err(msg: str) -> None:
        tqdm.write(clr(f"  ✖  {msg}", RED, BOLD))

    def _info(msg: str) -> None:
        tqdm.write(clr(f"  ●  {msg}", CYAN))

    def _ok(msg: str) -> None:
        tqdm.write(clr(f"  ✔  {msg}", GREEN))

    # ── header ───────────────────────────────────────────────────────────────
    mode_label = "hybrid + separate" if separate else "hybrid"
    tqdm.write("")
    tqdm.write(clr(f"  ╔══ {mode_label.upper()} MODE ", BOLD, CYAN) +
               clr(f"{'═' * max(0, 54 - len(mode_label))}╗", BOLD, CYAN))
    tqdm.write(clr(f"  ║  Source   : {source}", CYAN))
    tqdm.write(clr(f"  ║  Staging  : {dest_root}/", CYAN))
    tqdm.write(clr(f"  ║  Lookahead: {lookahead} day(s)  │  Workers: {workers}", CYAN))
    tqdm.write(clr(f"  ║  Days     : {day_total}", CYAN))
    if separate:
        tqdm.write(clr(f"  ║  Strategy : Pass 1 → stations+sensors  │  Pass 2 → readings", CYAN))
    tqdm.write(clr(f"  ╚{'═' * 58}╝", BOLD, CYAN))
    tqdm.write("")

    # ═══════════════════════════════════════════════════════════════════════
    # PASS 1  (or only pass when separate=False)
    # ═══════════════════════════════════════════════════════════════════════
    pass1_label = "Pass 1/2 — stations+sensors" if separate else "Ingesting"
    date_pbar = tqdm(
        date_folders,
        desc=clr(f" ⬇ {pass1_label}", BOLD, CYAN),
        unit="day",
        position=0,
        dynamic_ncols=True,
        colour="cyan",
    )

    for day_idx, date_folder in enumerate(date_pbar, start=1):
        # Queue next lookahead download (non-separate mode only)
        if not separate:
            future_idx = day_idx - 1 + lookahead
            if future_idx < day_total:
                _submit_fetch(date_folders[future_idx])

        # Show what is currently being fetched in the background
        fetching = [df for df, st in dl_status.items()
                    if st == "downloading" and df != date_folder]
        if fetching:
            date_pbar.set_description(
                clr(f" ⬇ {pass1_label}", BOLD, CYAN) +
                clr(f"  [prefetching: {', '.join(fetching[:2])}]", DIM)
            )

        # Wait for this day's download (non-separate mode only —
        # separate mode fetches per-batch inside its own block below)
        if not separate:
            _info(f"Waiting for local copy: {clr(date_folder, BOLD)}")
            local_date_path = pending_fetches.pop(date_folder).result()
            dl_status[date_folder] = "ready"
            _ok(f"Downloaded → {clr(str(local_date_path), BOLD)}  "
                f"(reading from local disk)")

        # ── IMPORTANT: always use local path for DB ingestion ──────────────
        local_source = str(dest_root)   # process_station_folder joins dest_root/date/station
        if not separate:
            station_folders = list_local_subdirs(local_date_path)
            day_total_st    = len(station_folders)
        else:
            station_folders = []   # separate mode sets day_total_st itself
            day_total_st    = 0

        day_done     = 0
        skipped      = 0
        log_lock     = threading.Lock()
        counts_lock  = threading.Lock()

        if separate:
            # ── batched metadata + readings pass (SEPARATE_BATCH_SIZE at a time) ──
            #
            # For each batch of up to SEPARATE_BATCH_SIZE station folders:
            #   1. Download .json files only  → insert stations+sensors (META)
            #   2. Download .csv files only   → insert readings         (RDGS)
            #   3. Delete that batch from disk to free space
            #   4. Move to the next batch
            #
            # This keeps at most SEPARATE_BATCH_SIZE stations' worth of files
            # on disk at any one time, rather than the entire date folder.

            # Discover station names from the remote listing (no full download yet)
            date_url_remote = urljoin(source.rstrip("/") + "/", date_folder + "/")
            all_station_names = list_url_subdirs(date_url_remote)
            day_total_st = len(all_station_names)
            local_date_path = dest_root / date_folder
            local_date_path.mkdir(parents=True, exist_ok=True)
            local_source = str(dest_root)

            st_pbar = tqdm(
                total=day_total_st,
                desc=clr(f"   ├─ {date_folder} [SEPARATE]", CYAN),
                unit="stn", position=1, leave=False, dynamic_ncols=True,
                colour="cyan",
            )

            def _meta_w(sf: str) -> dict:
                tconn = get_db_connection(verbose=False)
                try:
                    return process_station_folder_metadata(
                        local_source, date_folder, sf, False,
                        tconn, location_error_path, log_lock,
                    )
                finally:
                    tconn.close()

            def _rdg_w(sf: str) -> dict:
                tconn = get_db_connection(verbose=False)
                try:
                    return process_station_folder_readings(
                        local_source, date_folder, sf, False, tconn,
                    )
                finally:
                    tconn.close()

            # Slice into batches of SEPARATE_BATCH_SIZE
            batch_size = SEPARATE_BATCH_SIZE
            for batch_start in range(0, day_total_st, batch_size):
                batch = all_station_names[batch_start: batch_start + batch_size]
                batch_num = batch_start // batch_size + 1
                batch_total = (day_total_st + batch_size - 1) // batch_size

                _info(
                    f"{date_folder}  batch {batch_num}/{batch_total}  "
                    f"({len(batch)} stations)  — downloading JSON…"
                )

                # Step 1: download .json files for this batch
                _download_station_json_files(source, date_folder, batch, dest_root)

                # Step 2: insert metadata (stations + sensors)
                if workers == 1:
                    for sf in batch:
                        c = _meta_w(sf)
                        if c["station_ok"]:
                            _merge(c); day_done += 1
                        skipped += c["skipped_no_location"] + c["skipped_bad_location"]
                        st_pbar.set_postfix(
                            batch=clr(f"{batch_num}/{batch_total}", DIM),
                            done=clr(day_done, GREEN),
                            se=clr(f"{total['sensors']:,}", CYAN),
                            skip=clr(skipped, YELLOW) if skipped else skipped,
                            refresh=True,
                        )
                        st_pbar.update(1)
                else:
                    with _TPE(max_workers=workers) as ex:
                        futs = {ex.submit(_meta_w, sf): sf for sf in batch}
                        for fut in as_completed(futs):
                            try:
                                c = fut.result()
                                with counts_lock:
                                    if c["station_ok"]:
                                        _merge(c); day_done += 1
                                    skipped += c["skipped_no_location"] + c["skipped_bad_location"]
                            except Exception as exc:
                                _err(f"{futs[fut]}: {exc}")
                            finally:
                                st_pbar.set_postfix(
                                    batch=clr(f"{batch_num}/{batch_total}", DIM),
                                    done=clr(day_done, GREEN),
                                    se=clr(f"{total['sensors']:,}", CYAN),
                                    skip=clr(skipped, YELLOW) if skipped else skipped,
                                    refresh=True,
                                )
                                st_pbar.update(1)

                _info(
                    f"{date_folder}  batch {batch_num}/{batch_total}  "
                    f"— downloading CSV…"
                )

                # Step 3: download .csv files for this batch
                _download_station_csv_files(source, date_folder, batch, dest_root)

                # Step 4: insert readings
                rd_done = 0
                if workers == 1:
                    for sf in batch:
                        c = _rdg_w(sf)
                        total["readings"]    += c["readings"]
                        total["skipped_csv"] += c["skipped_csv"]
                        rd_done += 1
                else:
                    cl2 = threading.Lock()
                    with _TPE(max_workers=workers) as ex:
                        futs2 = {ex.submit(_rdg_w, sf): sf for sf in batch}
                        for fut in as_completed(futs2):
                            try:
                                c = fut.result()
                                with cl2:
                                    total["readings"]    += c["readings"]
                                    total["skipped_csv"] += c["skipped_csv"]
                                    rd_done += 1
                            except Exception as exc:
                                _err(f"{futs2[fut]}: {exc}")

                # Step 5: delete this batch from disk
                _delete_station_batch(local_date_path, batch)
                _ok(
                    f"Batch {batch_num}/{batch_total} complete  "
                    f"({rd_done} readings stations)  — local files deleted"
                )

            st_pbar.close()

            # Clean up the (now-empty) date folder itself
            _delete_date_folder(local_date_path)

            _status("META", date_folder, day_idx, day_done, day_total_st, skipped)
            mark_date_completed(log_path, date_folder)
            date_pbar.set_postfix(
                st=clr(f"{total['stations']:,}", GREEN),
                se=clr(f"{total['sensors']:,}", CYAN),
                rd=clr(f"{total['readings']:,}", MAGENTA),
                skip=clr(skipped, YELLOW) if skipped else skipped,
                refresh=True,
            )

        else:
            # ── full ingest pass ───────────────────────────────────────────
            def _full_w(sf: str) -> dict:
                tconn = get_db_connection(verbose=False)
                try:
                    return process_station_folder(
                        local_source, date_folder, sf, False,
                        tconn, None, None, location_error_path, log_lock,
                    )
                finally:
                    tconn.close()

            st_pbar = tqdm(
                total=day_total_st,
                desc=clr(f"   ├─ {date_folder} [INGEST]", GREEN),
                unit="stn", position=1, leave=False, dynamic_ncols=True,
                colour="green",
            )
            if workers == 1:
                for sf in station_folders:
                    c = _full_w(sf)
                    _merge(c); day_done += 1
                    skipped += c["skipped_no_location"] + c["skipped_bad_location"]
                    st_pbar.set_postfix(
                        done=clr(day_done, GREEN),
                        rd=clr(f"{total['readings']:,}", MAGENTA),
                        skip=clr(skipped, YELLOW) if skipped else skipped,
                        refresh=True,
                    )
                    st_pbar.update(1)
            else:
                with _TPE(max_workers=workers) as ex:
                    futs = {ex.submit(_full_w, sf): sf for sf in station_folders}
                    for fut in as_completed(futs):
                        try:
                            c = fut.result()
                            with counts_lock:
                                _merge(c); day_done += 1
                                skipped += c["skipped_no_location"] + c["skipped_bad_location"]
                        except Exception as exc:
                            _err(f"{futs[fut]}: {exc}")
                        finally:
                            st_pbar.set_postfix(
                                done=clr(day_done, GREEN),
                                rd=clr(f"{total['readings']:,}", MAGENTA),
                                skip=clr(skipped, YELLOW) if skipped else skipped,
                                refresh=True,
                            )
                            st_pbar.update(1)
            st_pbar.close()

            _status("FULL", date_folder, day_idx, day_done, day_total_st, skipped)
            mark_date_completed(log_path, date_folder)
            _delete_date_folder(local_date_path)
            _ok(f"Deleted local copy: {clr(str(local_date_path), DIM)}")
            date_pbar.set_postfix(
                st=clr(f"{total['stations']:,}", GREEN),
                se=clr(f"{total['sensors']:,}", CYAN),
                rd=clr(f"{total['readings']:,}", MAGENTA),
                skip=clr(skipped, YELLOW) if skipped else skipped,
                refresh=True,
            )

    date_pbar.close()
    fetch_executor.shutdown(wait=False)

    if not separate:
        return

    # In batched separate mode all readings are inserted inline (per batch),
    # so _meta_done is empty and there is nothing more to do.
    tqdm.write("")
    tqdm.write(clr(
        f"  ✔  Separate mode complete — "
        f"{total['readings']:,} readings inserted  "
        f"({total['skipped_csv']:,} CSVs missing)", BOLD, GREEN
    ))



# ---------------------------------------------------------------------------
# Separate method helpers  (--method separate)
# ---------------------------------------------------------------------------

def process_station_folder_metadata(
    source: str,
    date_folder: str,
    station_folder: str,
    is_remote: bool,
    conn,
    location_error_log_path: Optional[Path] = None,
    location_error_lock=None,
) -> dict:
    """Pass 1 of --method separate: insert/upsert station + sensors only.

    Returns counts dict with 'sensors', 'skipped_*' keys and a 'station_id'
    entry so pass 2 can skip stations that were rejected here.
    """
    counts = {
        "sensors": 0,
        "readings": 0,
        "skipped_csv": 0,
        "skipped_no_location": 0,
        "skipped_bad_location": 0,
        "new_sensors_by_year": {},
        "new_stations_by_country": {},
        "station_ok": False,   # True when the station was accepted
    }

    # -- Locate files (same logic as process_station_folder) -----------------
    if is_remote:
        base = urljoin(source.rstrip("/") + "/", f"{date_folder}/{station_folder}/")
        station_folder_path = base
        listing_raw = read_url_file(base)
        if not listing_raw:
            return counts
        soup  = BeautifulSoup(listing_raw.decode("utf-8"), "html.parser")
        raw_files = [a["href"] for a in soup.find_all("a", href=True)]
        files = []
        for f in raw_files:
            if f.startswith("./"):
                f = f[2:]
            if f and not f.startswith(("?", "/", "..")):
                files.append(f)
    else:
        folder_path = Path(source) / date_folder / station_folder
        station_folder_path = str(folder_path.resolve())
        files = [f.name for f in folder_path.iterdir() if f.is_file()]

    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        return counts

    json_filename = json_files[0]
    if is_remote:
        raw_json = read_url_file(urljoin(base, json_filename))
    else:
        raw_json = read_local_file(folder_path / json_filename)

    if not raw_json:
        return counts

    station_data = parse_station_json(raw_json)
    if not station_data:
        return counts

    station_id = station_data.get("id", "")
    has_location_info, lon, lat = station_location_info(station_data)

    if lon is None or lat is None:
        skip_reason = "location info but no usable coordinates" if has_location_info else "no location information"
        if location_error_log_path is not None:
            def _log():
                mark_location_error(
                    location_error_log_path, date_folder, station_folder,
                    station_folder_path, station_id, skip_reason, station_data,
                )
            if location_error_lock:
                with location_error_lock:
                    _log()
            else:
                _log()
        if has_location_info:
            counts["skipped_bad_location"] += 1
        else:
            counts["skipped_no_location"] += 1
        return counts

    with conn.cursor() as cur:
        st_id_ret, is_new_station, station_country, skip_reason = upsert_station(cur, station_data)
        if st_id_ret is None:
            conn.rollback()
            if skip_reason == "no location information":
                counts["skipped_no_location"] += 1
            elif skip_reason == "location info but no usable coordinates":
                counts["skipped_bad_location"] += 1
            return counts

        if is_new_station:
            key = station_country or "Unknown"
            counts["new_stations_by_country"][key] = counts["new_stations_by_country"].get(key, 0) + 1

        for sensor in station_data.get("sensors", []):
            se_id_ret, is_new_sensor = upsert_sensor(cur, sensor, st_id_ret)
            if se_id_ret is None:
                continue
            counts["sensors"] += 1

        conn.commit()

    counts["station_ok"] = True
    return counts


def process_station_folder_readings(
    source: str,
    date_folder: str,
    station_folder: str,
    is_remote: bool,
    conn,
) -> dict:
    """Pass 2 of --method separate: insert readings only (station+sensors already exist).

    Also handles sensors whose CSV files exist in the archive but whose se_id was
    not listed in the station JSON for this date (e.g. sensors added later and
    back-populated, or sensors removed from the station metadata but whose
    historical CSVs are still present).  For such orphan CSVs the se_id is
    derived from the filename prefix; if the sensor row is missing from the DB
    it is auto-inserted as a minimal stub so the FK constraint is satisfied.
    """
    counts = {
        "sensors": 0,
        "readings": 0,
        "skipped_csv": 0,
        "skipped_no_location": 0,
        "skipped_bad_location": 0,
        "new_sensors_by_year": {},
        "new_stations_by_country": {},
    }

    if is_remote:
        base = urljoin(source.rstrip("/") + "/", f"{date_folder}/{station_folder}/")
        listing_raw = read_url_file(base)
        if not listing_raw:
            return counts
        soup  = BeautifulSoup(listing_raw.decode("utf-8"), "html.parser")
        raw_files = [a["href"] for a in soup.find_all("a", href=True)]
        files = []
        for f in raw_files:
            if f.startswith("./"):
                f = f[2:]
            if f and not f.startswith(("?", "/", "..")):
                files.append(f)
    else:
        folder_path = Path(source) / date_folder / station_folder
        if not folder_path.exists():
            return counts
        files = [f.name for f in folder_path.iterdir() if f.is_file()]

    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        return counts

    json_filename = json_files[0]
    raw_json = (
        read_url_file(urljoin(base, json_filename))
        if is_remote
        else read_local_file(folder_path / json_filename)
    )
    if not raw_json:
        return counts

    station_data = parse_station_json(raw_json)
    if not station_data:
        return counts

    # st_id is the 24-char hex prefix of the station folder name
    st_id = station_data.get("id", station_folder[:STATION_ID_LEN])

    # Build a map of se_id -> sensor dict from the JSON listing
    sensors_by_id: dict[str, dict] = {
        s.get("id", ""): s
        for s in station_data.get("sensors", [])
        if s.get("id")
    }

    # All CSV files on disk/remote, keyed by the se_id prefix in their filename
    csv_files_map: dict[str, str] = {
        f[:STATION_ID_LEN]: f for f in files if f.endswith(".csv")
    }

    # Union: sensors from JSON + any extra CSV files not in the JSON
    all_se_ids = set(sensors_by_id) | set(csv_files_map)

    with conn.cursor() as cur:
        # Check whether the parent station was accepted in the metadata pass.
        # Stations with no usable location are intentionally excluded from the
        # stations table; trying to insert sensors or readings for them would
        # just produce FK violations.  Skip silently.
        cur.execute("SELECT st_id FROM stations WHERE st_id = %s", (st_id,))
        row = cur.fetchone()
        if row is None:
            return counts
        st_id_db = row[0]

        for se_id in all_se_ids:
            csv_filename = csv_files_map.get(se_id)
            if not csv_filename:
                # sensor listed in JSON but no CSV on disk — nothing to insert
                counts["skipped_csv"] += 1
                continue

            raw_csv = (
                read_url_file(urljoin(base, csv_filename))
                if is_remote
                else read_local_file(folder_path / csv_filename)
            )
            if not raw_csv:
                counts["skipped_csv"] += 1
                continue

            rows = parse_sensor_csv(raw_csv)
            if not rows:
                continue

            # Ensure the sensor row exists before inserting readings.
            # Sensors listed in the JSON were upserted in the metadata pass, but
            # CSV files can reference sensor IDs that were added to the station
            # *after* the date snapshot — those IDs are absent from the sensors
            # table and would cause an FK violation.  We do a lightweight
            # INSERT … ON CONFLICT DO NOTHING so the FK is always satisfied.
            sensor_meta = sensors_by_id.get(se_id, {})
            sensor_type = sensor_meta.get("sensorType") or None
            title       = sensor_meta.get("title") or sensor_meta.get("sensorType") or None
            unit        = sensor_meta.get("unit") or None
            category    = get_sensor_category(title or "", unit or "") if (title or unit) else None
            cur.execute(
                """
                INSERT INTO sensors (se_id, st_id, title, unit, sensor_type, category)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (se_id) DO NOTHING
                RETURNING se_id
                """,
                (
                    se_id,
                    st_id_db,
                    title,
                    unit,
                    sensor_type,
                    category,
                ),
            )
            inserted = cur.fetchone()
            if not inserted:
                # Sensor already existed — verify it's there
                cur.execute("SELECT se_id FROM sensors WHERE se_id = %s", (se_id,))
                se_row = cur.fetchone()
                if se_row is None:
                    continue

            try:
                cur.execute("SAVEPOINT rdg_insert")
                n = insert_readings(cur, se_id, st_id_db, rows)
                cur.execute("RELEASE SAVEPOINT rdg_insert")
                counts["readings"] += n
            except psycopg2.Error as exc:
                # Roll back only this sensor's work; keep the transaction alive
                # for the remaining sensors in this station.
                cur.execute("ROLLBACK TO SAVEPOINT rdg_insert")
                cur.execute("RELEASE SAVEPOINT rdg_insert")
                tqdm.write(
                    f"  [WARN] Skipping readings for {se_id} in {station_folder}: {exc}"
                )
                continue

        conn.commit()

    return counts


def run_separate_passes(
    source: str,
    date_folders: list,
    is_remote: bool,
    conn,
    workers: int,
    log_path: Path,
    location_error_path: Path,
    total: dict,
    run_start_time: float,
) -> None:
    """Two-pass ingest for --method separate.

    Pass 1: walk every date folder, upsert all stations + sensors.
    Pass 2: walk every date folder again, insert all readings.
    Both passes respect --workers parallelism.
    """
    import threading
    import time as _time

    day_total = len(date_folders)

    def _merge(counts: dict) -> None:
        total["stations"]    += 1
        total["sensors"]     += counts["sensors"]
        total["readings"]    += counts["readings"]
        total["skipped_csv"] += counts["skipped_csv"]
        total["skipped_no_location"]  += counts["skipped_no_location"]
        total["skipped_bad_location"] += counts["skipped_bad_location"]
        for yr, cnt in counts["new_sensors_by_year"].items():
            total["new_sensors_by_year"][yr] = total["new_sensors_by_year"].get(yr, 0) + cnt
        for ctry, cnt in counts["new_stations_by_country"].items():
            total["new_stations_by_country"][ctry] = total["new_stations_by_country"].get(ctry, 0) + cnt

    # -----------------------------------------------------------------------
    # PASS 1 — stations + sensors
    # -----------------------------------------------------------------------
    print("\n[SEPARATE] Pass 1/2 — inserting stations and sensors...")

    pass1_pbar = tqdm(date_folders, desc="Pass 1 (meta)", unit="day", position=0, dynamic_ncols=True)

    for day_idx, date_folder in enumerate(pass1_pbar, start=1):
        pass1_pbar.set_description(f"Pass1  [{date_folder}]")

        if is_remote:
            station_folders = list_url_subdirs(
                urljoin(source.rstrip("/") + "/", date_folder + "/")
            )
        else:
            station_folders = list_local_subdirs(Path(source) / date_folder)

        counts_lock  = threading.Lock()
        log_lock     = threading.Lock()
        day_done     = 0
        day_total_st = len(station_folders)

        def _meta_worker(sf: str) -> dict:
            tconn = get_db_connection(verbose=False)
            try:
                return process_station_folder_metadata(
                    source, date_folder, sf, is_remote,
                    tconn, location_error_path, log_lock,
                )
            finally:
                tconn.close()

        if workers == 1:
            st_pbar = tqdm(station_folders, desc=f"  {date_folder}", unit="stn",
                           position=1, leave=False, dynamic_ncols=True)
            for sf in st_pbar:
                c = _meta_worker(sf)
                if c["station_ok"]:
                    with counts_lock:
                        _merge(c)
                        day_done += 1
                st_pbar.set_postfix(done=day_done, se=total["sensors"], refresh=True)
            st_pbar.close()
        else:
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
            st_pbar = tqdm(total=day_total_st, desc=f"  {date_folder}", unit="stn",
                           position=1, leave=False, dynamic_ncols=True)
            with _TPE(max_workers=workers) as ex:
                futs = {ex.submit(_meta_worker, sf): sf for sf in station_folders}
                for fut in _ac(futs):
                    try:
                        c = fut.result()
                        if c["station_ok"]:
                            with counts_lock:
                                _merge(c)
                                day_done += 1
                    except Exception as exc:
                        tqdm.write(f"  [ERROR] {futs[fut]}: {exc}")
                    finally:
                        st_pbar.update(1)
                        st_pbar.set_postfix(done=day_done, se=total["sensors"], refresh=True)
            st_pbar.close()

        pass1_pbar.set_postfix(st=f"{total['stations']:,}", se=f"{total['sensors']:,}", refresh=True)

    pass1_pbar.close()
    print(f"[SEPARATE] Pass 1 complete — {total['stations']:,} stations, {total['sensors']:,} sensors.")

    # -----------------------------------------------------------------------
    # PASS 2 — readings
    # -----------------------------------------------------------------------
    print("\n[SEPARATE] Pass 2/2 — inserting readings...")

    # Reset total readings counter (stations/sensors already counted in pass 1)
    total["readings"]    = 0
    total["skipped_csv"] = 0

    pass2_pbar = tqdm(date_folders, desc="Pass 2 (rdgs)", unit="day", position=0, dynamic_ncols=True)

    for day_idx, date_folder in enumerate(pass2_pbar, start=1):
        pass2_pbar.set_description(f"Pass2  [{date_folder}]")

        if is_remote:
            station_folders = list_url_subdirs(
                urljoin(source.rstrip("/") + "/", date_folder + "/")
            )
        else:
            station_folders = list_local_subdirs(Path(source) / date_folder)

        rd_lock  = threading.Lock()
        day_done = 0

        def _rdg_worker(sf: str) -> dict:
            tconn = get_db_connection(verbose=False)
            try:
                return process_station_folder_readings(
                    source, date_folder, sf, is_remote, tconn,
                )
            finally:
                tconn.close()

        if workers == 1:
            st_pbar = tqdm(station_folders, desc=f"  {date_folder}", unit="stn",
                           position=1, leave=False, dynamic_ncols=True)
            for sf in st_pbar:
                c = _rdg_worker(sf)
                with rd_lock:
                    total["readings"]    += c["readings"]
                    total["skipped_csv"] += c["skipped_csv"]
                    day_done += 1
                st_pbar.set_postfix(done=day_done, rd=total["readings"], refresh=True)
            st_pbar.close()
        else:
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
            st_pbar = tqdm(total=len(station_folders), desc=f"  {date_folder}", unit="stn",
                           position=1, leave=False, dynamic_ncols=True)
            with _TPE(max_workers=workers) as ex:
                futs = {ex.submit(_rdg_worker, sf): sf for sf in station_folders}
                for fut in _ac(futs):
                    try:
                        c = fut.result()
                        with rd_lock:
                            total["readings"]    += c["readings"]
                            total["skipped_csv"] += c["skipped_csv"]
                            day_done += 1
                    except Exception as exc:
                        tqdm.write(f"  [ERROR] {futs[fut]}: {exc}")
                    finally:
                        st_pbar.update(1)
                        st_pbar.set_postfix(done=day_done, rd=total["readings"], refresh=True)
            st_pbar.close()

        mark_date_completed(log_path, date_folder)
        pass2_pbar.set_postfix(rd=f"{total['readings']:,}", refresh=True)

    pass2_pbar.close()
    print(f"[SEPARATE] Pass 2 complete — {total['readings']:,} readings inserted.")

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
    sql_buffers: Optional[dict] = None,
    location_error_log_path: Optional[Path] = None,
    location_error_lock=None,
) -> dict:
    """Process one station folder. Returns counts.

    One of conn / dry_buffers / sql_buffers must be provided:
      - conn        -> write directly to TimescaleDB
      - dry_buffers -> collect rows for tab-separated CSV preview
      - sql_buffers -> collect rows for portable SQL dump (--type sql)
    """
    counts = {
        "sensors": 0,
        "readings": 0,
        "skipped_csv": 0,
        "skipped_no_location": 0,
        "skipped_bad_location": 0,
        "new_sensors_by_year": {},
        "new_stations_by_country": {},
    }

    # Locate the .json file
    if is_remote:
        base = urljoin(source.rstrip("/") + "/", f"{date_folder}/{station_folder}/")
        station_folder_path = base
        listing_raw = read_url_file(base)
        if not listing_raw:
            return counts
        soup = BeautifulSoup(listing_raw.decode("utf-8"), "html.parser")
        raw_files = [a["href"] for a in soup.find_all("a", href=True)]
        files = []
        for f in raw_files:
            if f.startswith("./"):
                f = f[2:]
            if f and not f.startswith(("?", "/", "..")):
                files.append(f)
    else:
        folder_path = Path(source) / date_folder / station_folder
        station_folder_path = str(folder_path.resolve())
        files = [f.name for f in folder_path.iterdir() if f.is_file()]

    # Find the station JSON file (first 24 chars = station id, ends with .json)
    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        return counts

    json_filename = json_files[0]
    if is_remote:
        raw_json = read_url_file(urljoin(base, json_filename))
    else:
        raw_json = read_local_file(folder_path / json_filename)

    if not raw_json:
        tqdm.write(f"  [WARN] Could not read {json_filename}")
        return counts

    station_data = parse_station_json(raw_json)
    if not station_data:
        return counts

    station_id = station_data.get("id", "")

    has_location_info, lon, lat = station_location_info(station_data)

    if lon is None or lat is None:
        skip_reason = "location info but no usable coordinates" if has_location_info else "no location information"
        if location_error_log_path is not None:
            if location_error_lock is not None:
                with location_error_lock:
                    mark_location_error(
                        location_error_log_path,
                        date_folder,
                        station_folder,
                        station_folder_path,
                        station_id,
                        skip_reason,
                        station_data,
                    )
            else:
                mark_location_error(
                    location_error_log_path,
                    date_folder,
                    station_folder,
                    station_folder_path,
                    station_id,
                    skip_reason,
                    station_data,
                )
        if has_location_info:
            counts["skipped_bad_location"] += 1
        else:
            counts["skipped_no_location"] += 1
        return counts

    # -- DB or buffer: station -----------------------------------------------
    if dry_buffers is not None or sql_buffers is not None:
        active_buf = dry_buffers if dry_buffers is not None else sql_buffers
        country, region = lookup_admin(lon, lat)
        active_buf["stations"].append([
            station_id,
            station_data.get("boxType"),
            station_data.get("model"),
            lon, lat,
            region,
            country,
        ])
    else:
        # Open one cursor for the entire station (station + all sensors + all readings)
        # and commit once at the end -- vastly fewer round-trips vs. per-sensor commits.
        with conn.cursor() as cur:
            st_id_ret, is_new_station, station_country, skip_reason = upsert_station(cur, station_data)
            if st_id_ret is None:
                if skip_reason and location_error_log_path is not None:
                    if location_error_lock is not None:
                        with location_error_lock:
                            mark_location_error(
                                location_error_log_path,
                                date_folder,
                                station_folder,
                                station_folder_path,
                                station_id,
                                skip_reason,
                                station_data,
                            )
                    else:
                        mark_location_error(
                            location_error_log_path,
                            date_folder,
                            station_folder,
                            station_folder_path,
                            station_id,
                            skip_reason,
                            station_data,
                        )
                if skip_reason == "no location information":
                    counts["skipped_no_location"] += 1
                elif skip_reason == "location info but no usable coordinates":
                    counts["skipped_bad_location"] += 1
                conn.rollback()
                return counts
            if is_new_station:
                key = station_country or "Unknown"
                counts["new_stations_by_country"][key] = counts["new_stations_by_country"].get(key, 0) + 1

            # -- Sensors -----------------------------------------------------
            sensors = station_data.get("sensors", [])
            csv_files = {f[:STATION_ID_LEN]: f for f in files if f.endswith(".csv")}

            for sensor in sensors:
                se_id = sensor.get("id", "")

                csv_filename = csv_files.get(se_id)
                if not csv_filename:
                    counts["skipped_csv"] += 1
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

                se_id_ret, is_new_sensor = upsert_sensor(cur, sensor, st_id_ret)
                if se_id_ret is None:
                    continue

                if rows:
                    n = insert_readings(cur, se_id_ret, st_id_ret, rows)
                    counts["readings"] += n

                counts["sensors"] += 1

        conn.commit()  # single commit covers the whole station
        return counts

    # -- buffer path (dry-run / sql): sensors + readings ---------------------
    active_buf = dry_buffers if dry_buffers is not None else sql_buffers
    sensors   = station_data.get("sensors", [])
    csv_files = {f[:STATION_ID_LEN]: f for f in files if f.endswith(".csv")}

    for sensor in sensors:
        se_id = sensor.get("id", "")

        csv_filename = csv_files.get(se_id)
        if not csv_filename:
            counts["skipped_csv"] += 1
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

        active_buf["sensors"].append([
            se_id,
            station_data.get("id"),
            sensor.get("title"),
            sensor.get("unit"),
            sensor.get("sensorType") or None,
            get_sensor_category(sensor.get("title", ""), sensor.get("unit", "")),
        ])

        if rows:
            for row in rows:
                active_buf["readings"].append([se_id, station_data.get("id"), row.get("createdAt"), row.get("value")])
            counts["readings"] += len(rows)

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

    print("\n  New sensors added -- by init_date year")
    print(sep)
    print(f"| {'Year':<{col_w_year-1}}| {'Count':>{col_w_cnt-1}} | {'Bar':<{col_w_bar-1}}|")
    print(sep)

    for year, count in zip(years, counts_):
        filled = round(count / max_count * bar_width) if max_count else 0
        bar    = "X" * filled + "." * (bar_width - filled)
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

    print("\n  New stations added -- by country")
    print(sep)
    print(f"| {'Country':<{col_w_ctry-1}}| {'Count':>{col_w_cnt-1}} | {'Bar':<{col_w_bar-1}}|")
    print(sep)

    for country, count in zip(countries, counts_):
        filled = round(count / max_count * bar_width) if max_count else 0
        bar    = "X" * filled + "." * (bar_width - filled)
        print(f"| {country:<{col_w_ctry-1}}| {count:>{col_w_cnt-1},} | {bar} |")

    print(sep)
    print(f"| {'TOTAL':<{col_w_ctry-1}}| {sum(counts_):>{col_w_cnt-1},} | {'':<{col_w_bar-1}}|")
    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()

    # -- Import mode -- completely separate path, no archive scanning needed -
    if args.import_sql:
        import_sql_file(Path(args.import_sql), args.env)
        return

    # -- Archive processing mode ---------------------------------------------
    OSEM_DEFAULT_URL = "https://archive.opensensemap.org/"
    source   = args.source or OSEM_DEFAULT_URL

    dry_run   = args.dry_run
    sql_mode  = (args.output_type == "sql")
    way       = args.way        # None | "hybrid"
    method    = args.method     # None | "separate"
    hybrid    = (way == "hybrid")
    separate  = (method == "separate")
    start     = parse_date(args.start)
    end       = parse_date(args.end)
    workers   = args.workers
    num_parts = args.part if sql_mode else None

    if hybrid and not is_url(source):
        print("[ERROR] --way hybrid requires a remote --source URL "
              f"(got: '{source}'). Omit --source to use the default archive URL.")
        sys.exit(1)
    if hybrid and (dry_run or sql_mode):
        print("[ERROR] --way hybrid cannot be combined with --dry-run or --type sql.")
        sys.exit(1)
    if separate and (dry_run or sql_mode):
        print("[ERROR] --method separate cannot be combined with --dry-run or --type sql.")
        sys.exit(1)

    load_env(args.env)
    # Resolve admin boundary path: CLI flag > constant default
    admin_path = Path(args.admin_boundary) if args.admin_boundary else ADMIN_BOUNDARY_PATH
    load_admin_boundaries(admin_path)  # load once into _ADMIN_FEATURES
    load_sensor_types_csv()            # load sensor_types.csv for category lookup

    is_remote   = is_url(source)
    conn        = None
    dry_buffers = None
    sql_buffers = None

    if sql_mode:
        print("[SQL] No data will be written to the database.")
        print(f"[SQL] SQL dump will be written to the '{SQL_DUMP_DIR}/' folder.")
        if num_parts and num_parts > 1:
            print(f"[SQL] Output will be split into {num_parts} parts.")
        if start or end:
            print(f"[SQL] Date range: {start or 'beginning'} -> {end or 'end'}")
        sql_buffers = init_sql_dump()
    elif dry_run:
        print("[DRY-RUN] No data will be written to the database.")
        if start or end:
            print(f"[DRY-RUN] Date range: {start or 'beginning'} -> {end or 'end'}")
        dry_buffers = init_dry_run()
    else:
        conn = get_db_connection()

    # -- Discover date folders -----------------------------------------------
    print(f"\n[INFO] Scanning source: {source}")
    print(f"[INFO] Parallel workers per date folder: {workers}")
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

    log_path = RESUME_FILE
    last_done = load_last_completed_date(log_path)
    if last_done:
        date_folders = [d for d in date_folders if d > last_done]
    location_error_path = DATA_LOG_FILE
    init_data_log(location_error_path, source, start, end)

    resume_msg = f"resuming after {last_done}" if last_done else "fresh start"
    print(f"[INFO] {len(date_folders)} days to process  ({start or 'beginning'} -> {end or 'today'})  |  {resume_msg}")

    # ── ANSI helpers (same palette as run_hybrid) ───────────────────────────
    _R = "\033[0m"
    _BOLD = "\033[1m"; _CYAN = "\033[96m"; _GREEN = "\033[92m"
    _YELLOW = "\033[93m"; _MAGENTA = "\033[95m"; _DIM = "\033[2m"
    def _c(t, *codes): return "".join(codes) + str(t) + _R

    if hybrid:
        print(_c(f"  [HYBRID] Staging folder : {DOWNLOAD_DIR}/", _CYAN))
        print(_c(f"  [HYBRID] Lookahead      : {HYBRID_LOOKAHEAD} day(s)  │  Workers: {workers}", _CYAN))
    if separate:
        print(_c("  [SEPARATE] Strategy: Pass 1 → stations+sensors  │  Pass 2 → readings", _MAGENTA))

    # -- Totals --------------------------------------------------------------
    total = {
        "stations": 0,
        "sensors": 0,
        "readings": 0,
        "skipped_csv": 0,
        "skipped_no_location": 0,
        "skipped_bad_location": 0,
        "new_sensors_by_year": {},
        "new_stations_by_country": {},
    }

    import threading
    import time as _time

    run_start_time = _time.monotonic()

    # -- Hybrid mode: hand off to dedicated runner ---------------------------
    if hybrid:
        run_hybrid(
            source=source,
            date_folders=date_folders,
            conn=conn,
            dry_buffers=dry_buffers,
            sql_buffers=sql_buffers,
            workers=workers,
            log_path=log_path,
            location_error_path=location_error_path,
            total=total,
            run_start_time=run_start_time,
            separate=separate,
        )
        # Summary (hybrid path)
        sep = _c("  " + "═" * 58, _BOLD, _CYAN)
        mode_tag = "hybrid + separate" if separate else "hybrid"
        print("")
        print(sep)
        print(_c(f"  ✔  Load complete  [{mode_tag}]", _BOLD, _GREEN))
        print(sep)
        print(_c(f"  Stations added     : {total['stations']:>10,}", _GREEN))
        print(_c(f"  Sensors added      : {total['sensors']:>10,}", _CYAN))
        print(_c(f"  Readings added     : {total['readings']:>10,}", _MAGENTA))
        print(_c(f"  CSVs not found     : {total['skipped_csv']:>10,}", _YELLOW if total['skipped_csv'] else _DIM))
        print(_c(f"  No location        : {total['skipped_no_location']:>10,}", _YELLOW if total['skipped_no_location'] else _DIM))
        print(_c(f"  Bad location       : {total['skipped_bad_location']:>10,}", _YELLOW if total['skipped_bad_location'] else _DIM))
        print(sep)
        if conn:
            conn.close()
        print_sensor_year_chart(total["new_sensors_by_year"])
        print_station_country_chart(total["new_stations_by_country"])
        return

    # -- Separate method (non-hybrid): two-pass ingest -----------------------
    if separate:
        run_separate_passes(
            source=source,
            date_folders=date_folders,
            is_remote=is_remote,
            conn=conn,
            workers=workers,
            log_path=log_path,
            location_error_path=location_error_path,
            total=total,
            run_start_time=run_start_time,
        )
        print("\n" + "=" * 60)
        print("  Load complete  [separate method]")
        print("=" * 60)
        print(f"  Stations added     : {total['stations']:>10,}")
        print(f"  Sensors added      : {total['sensors']:>10,}")
        print(f"  Readings added     : {total['readings']:>10,}")
        print(f"  CSVs not found     : {total['skipped_csv']:>10,}")
        print(f"  No location        : {total['skipped_no_location']:>10,}")
        print(f"  Bad location       : {total['skipped_bad_location']:>10,}")
        print("=" * 60)
        if conn:
            conn.close()
        print_sensor_year_chart(total["new_sensors_by_year"])
        print_station_country_chart(total["new_stations_by_country"])
        return

    # -- live status line printed after each station -------------------------
    def _print_status(date_folder: str, day_idx: int, day_total: int,
                      st_done: int, st_total: int, skipped: int) -> None:
        """Print a compact one-line status update using tqdm.write so it
        doesn't collide with the progress bars."""
        elapsed   = _time.monotonic() - run_start_time
        h, rem    = divmod(int(elapsed), 3600)
        m, s      = divmod(rem, 60)
        elapsed_s = f"{h:02d}:{m:02d}:{s:02d}"

        tqdm.write(
            f"  [{elapsed_s}] "
            f"Day {day_idx:>4}/{day_total}  {date_folder}  |  "
            f"stations {st_done:>4}/{st_total:<4}  "
            f"skipped {skipped:>3}  |  "
            f"total → st:{total['stations']:,}  "
            f"se:{total['sensors']:,}  "
            f"rd:{total['readings']:,}"
        )

    def _merge_counts(counts: dict) -> None:
        """Merge per-station counts into the running total (called from the main thread)."""
        total["stations"]    += 1
        total["sensors"]     += counts["sensors"]
        total["readings"]    += counts["readings"]
        total["skipped_csv"] += counts["skipped_csv"]
        total["skipped_no_location"] += counts["skipped_no_location"]
        total["skipped_bad_location"] += counts["skipped_bad_location"]
        for yr, cnt in counts["new_sensors_by_year"].items():
            total["new_sensors_by_year"][yr] = total["new_sensors_by_year"].get(yr, 0) + cnt
        for ctry, cnt in counts["new_stations_by_country"].items():
            total["new_stations_by_country"][ctry] = total["new_stations_by_country"].get(ctry, 0) + cnt

    # outer bar: one tick per date folder
    date_pbar = tqdm(
        date_folders,
        desc="Overall",
        unit="day",
        position=0,
        dynamic_ncols=True,
    )

    for day_idx, date_folder in enumerate(date_pbar, start=1):
        date_pbar.set_description(f"Overall  [{date_folder}]")

        # Discover station folders
        if is_remote:
            date_url = urljoin(source.rstrip("/") + "/", date_folder + "/")
            station_folders = list_url_subdirs(date_url)
        else:
            station_folders = list_local_subdirs(Path(source) / date_folder)

        pending_stations = station_folders
        day_done  = 0
        day_total = len(station_folders)

        if workers == 1:
            # ----------------------------------------------------------------
            # Single-threaded path
            # ----------------------------------------------------------------
            station_pbar = tqdm(
                pending_stations,
                desc=f"  {date_folder}",
                unit="stn",
                position=1,
                leave=False,
                dynamic_ncols=True,
            )
            for station_folder in station_pbar:
                station_pbar.set_postfix_str(station_folder[:30], refresh=True)
                counts = process_station_folder(
                    source, date_folder, station_folder,
                    is_remote, conn, dry_buffers, sql_buffers,
                    location_error_path,
                )
                _merge_counts(counts)
                day_done += 1
                station_pbar.set_postfix(
                    done=day_done, total=day_total,
                    rd=total["readings"],
                    refresh=True,
                )
            station_pbar.close()

        else:
            # ----------------------------------------------------------------
            # Parallel path
            # ----------------------------------------------------------------
            buf_lock = threading.Lock() if (dry_buffers is not None or sql_buffers is not None) else None
            log_lock = threading.Lock()
            counts_lock = threading.Lock()

            def _worker(station_folder: str) -> dict:
                if dry_buffers is not None or sql_buffers is not None:
                    local_buf  = {"stations": [], "sensors": [], "readings": []}
                    local_dry  = local_buf if dry_buffers is not None else None
                    local_sql  = local_buf if sql_buffers is not None else None
                    c = process_station_folder(
                        source, date_folder, station_folder,
                        is_remote, None, local_dry, local_sql,
                        location_error_path, log_lock,
                    )
                    shared_buf = dry_buffers if dry_buffers is not None else sql_buffers
                    with buf_lock:
                        for k in local_buf:
                            shared_buf[k].extend(local_buf[k])
                    return c
                else:
                    thread_conn = get_db_connection(verbose=False)
                    try:
                        result = process_station_folder(
                            source, date_folder, station_folder,
                            is_remote, thread_conn, None, None,
                            location_error_path, log_lock,
                        )
                        return result
                    finally:
                        thread_conn.close()

            station_pbar = tqdm(
                total=len(pending_stations),
                desc=f"  {date_folder}",
                unit="stn",
                position=1,
                leave=False,
                dynamic_ncols=True,
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_worker, sf): sf for sf in pending_stations}
                for future in as_completed(futures):
                    try:
                        c = future.result()
                        with counts_lock:
                            _merge_counts(c)
                            day_done += 1
                    except Exception as exc:
                        sf = futures[future]
                        tqdm.write(f"\n  [ERROR] {sf}: {exc}")
                    finally:
                        station_pbar.set_postfix(
                            done=day_done, total=day_total,
                            rd=total["readings"],
                            refresh=True,
                        )
                        station_pbar.update(1)
            station_pbar.close()

        mark_date_completed(log_path, date_folder)
        date_pbar.set_postfix(
            st=f"{total['stations']:,}",
            se=f"{total['sensors']:,}",
            rd=f"{total['readings']:,}",
            refresh=True,
        )

    date_pbar.close()

    # -- SQL dump output -----------------------------------------------------
    if sql_mode:
        print("\n[SQL] Writing .sql dump file(s)...")
        out_path = flush_sql_dump(sql_buffers, start, end, num_parts=num_parts)
        if not (num_parts and num_parts > 1):
            print(f"[SQL] To import later run:")
            print(f"       python load_data.py --import-sql {out_path}")

    # -- Dry-run output ------------------------------------------------------
    elif dry_run:
        print("\n[DRY-RUN] Writing preview CSVs...")
        flush_dry_run(dry_buffers)

    # -- Summary -------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  Load complete")
    print("=" * 60)
    print(f"  Stations added     : {total['stations']:>10,}")
    print(f"  Sensors added      : {total['sensors']:>10,}")
    print(f"  Readings added     : {total['readings']:>10,}")
    print(f"  CSVs not found     : {total['skipped_csv']:>10,}")
    print(f"  No location        : {total['skipped_no_location']:>10,}")
    print(f"  Bad location       : {total['skipped_bad_location']:>10,}")
    print("=" * 60)
    print(f"  Resume checkpoint  : {log_path}")
    print(f"  Data event log     : {location_error_path}")
    print("=" * 60)
    print(f"  Re-run the same command to continue from the last checkpoint.")
    print(f"  Delete {log_path} to restart from scratch.")
    print("=" * 60)

    if conn:
        conn.close()

    print_sensor_year_chart(total["new_sensors_by_year"])
    print_station_country_chart(total["new_stations_by_country"])


if __name__ == "__main__":
    main()
