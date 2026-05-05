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
          538da4d6a834155415765eae-Ctronix-2014-06-03.json   <- station + sensors
          538da4d6a834155415765eaf-2014-06-03.csv            <- sensor readings

Requirements:
    pip install psycopg2-binary python-dotenv tqdm requests beautifulsoup4

Usage:
    # Load everything from a local archive folder directly into the database
    python load_data.py --source /path/to/archive

    # Load from internet archive URL (directory listing must be browsable)
    python load_data.py --source https://archive.example.com/osem/

    # Filter by date range
    python load_data.py --source /path/to/archive --start 2014-06-01 --end 2014-06-30

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
STATION_ID_LEN      = 24
ADMIN_BOUNDARY_PATH = Path("data/admin_boundary.geojson")
WORKER_THREADS      = 8   # parallel station folders per date folder
PROGRESS_LOG        = Path("progress.log")  # resume checkpoint file

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

    print(f"[INFO] Loaded {len(_ADMIN_FEATURES):,} admin boundary polygons from {geojson_path}")


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
# Progress log -- resumable checkpoint tracking
# ---------------------------------------------------------------------------
def progress_log_path(source: str, start, end) -> Path:
    """Return a unique progress log path per source+date-range so different
    runs don't share the same log file."""
    import hashlib
    key = f"{source}|{start}|{end}"
    slug = hashlib.md5(key.encode()).hexdigest()[:8]
    start_s = str(start) if start else "begin"
    end_s   = str(end)   if end   else "end"
    return Path(f"progress_{start_s}_to_{end_s}_{slug}.log")


def load_completed(log_path: Path) -> set:
    """Return the set of 'date_folder/station_folder' keys already completed."""
    if not log_path.exists():
        return set()
    completed = set()
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                completed.add(line)
    print(f"[RESUME] Progress log found: {log_path}")
    print(f"[RESUME] Already completed : {len(completed):,} station-folders — these will be skipped.")
    return completed


def mark_completed(log_path: Path, date_folder: str, station_folder: str) -> None:
    """Append a completed key to the progress log (one line per station-folder)."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{date_folder}/{station_folder}\n")


def init_progress_log(log_path: Path, source: str, start, end) -> None:
    """Write a header comment if the log doesn't exist yet."""
    if not log_path.exists():
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# load_data.py progress log\n")
            f.write(f"# source : {source}\n")
            f.write(f"# range  : {start or 'beginning'} -> {end or 'end'}\n")
            f.write(f"# started: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"# Each line below is a completed date_folder/station_folder.\n")
            f.write(f"# Delete this file to restart from scratch.\n")


def error_log_path(source: str, start, end) -> Path:
    """Return the companion error log path for the same source/date-range."""
    base = progress_log_path(source, start, end)
    return base.with_name(base.stem + "_errors.log")


def init_error_log(log_path: Path, source: str, start, end) -> None:
    """Write a header comment if the error log doesn't exist yet."""
    if not log_path.exists():
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# load_data.py station location error log\n")
            f.write(f"# source : {source}\n")
            f.write(f"# range  : {start or 'beginning'} -> {end or 'end'}\n")
            f.write(f"# started: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"# Each line below contains a skipped station and its location payload.\n")
            f.write(f"# Delete this file to restart the log from scratch.\n")


def mark_location_error(
    log_path: Path,
    date_folder: str,
    station_folder: str,
    station_folder_path: str,
    station_id: str,
    reason: str,
    station_data: dict,
) -> None:
    """Append one station-location failure entry to the error log."""
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
        "date_folder": date_folder,
        "station_folder": station_folder,
        "station_folder_path": station_folder_path,
        "station_id": station_id,
        "reason": reason,
        "location": location_snapshot,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load OpenSenseMap archive into TimescaleDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # -- Two mutually exclusive top-level modes ------------------------------
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--source",
        help="Local folder or base URL of the archive to process"
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

    # -- Shared options ------------------------------------------------------
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
        if verbose:
            print(
                f"[INFO] Connected -> {params['user']}@"
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
    """Parse an Apache/Nginx directory listing for subdirectory hrefs."""
    resp = _SESSION.get(base_url, timeout=30)
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
def upsert_station(cur, data: dict) -> tuple[Optional[int], bool, Optional[str], Optional[str]]:
    """Insert or get st_uuid for a station.

    Returns (st_uuid, is_new, country) where is_new=True when freshly inserted.
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
        station_id,
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
    return st_uuid, is_new, stored_country, None


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


def upsert_sensor(cur, sensor: dict, st_uuid: int, init_date=None) -> tuple[Optional[int], bool]:
    """Insert or get se_uuid for a sensor.

    init_date should be the earliest timestamp from the readings CSV for this
    sensor. Written only on first insert; subsequent runs leave it untouched so
    the earliest date is always preserved.

    Returns (se_uuid, is_new) where is_new is True when the row was freshly
    inserted (xmax = 0 means INSERT path, not UPDATE path).
    """
    sensor_type = classify_sensor(sensor.get("title", ""), sensor.get("unit", ""))

    cur.execute("""
        INSERT INTO sensors (se_id, st_uuid, title, unit, info, type, init_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (se_id) DO UPDATE
            SET title     = EXCLUDED.title,
                unit      = EXCLUDED.unit,
                info      = EXCLUDED.info,
                type      = EXCLUDED.type,
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
        sensor_type,          # classified category replaces raw sensorType
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
        print(f"  [DRY-RUN] Written {len(rows):,} rows -> {out_path}")


# ---------------------------------------------------------------------------
# SQL dump mode  (--type sql)
# ---------------------------------------------------------------------------

# DDL used to create the tables in the dump (mirrors TimescaleDB schema,
# but without the hypertable call so the file is also importable into plain
# PostgreSQL for experimentation).
_SQL_SCHEMA = """\
-- ============================================================
--  OpenSenseMap archive dump
--  Generated by load_data.py --type sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS stations (
    st_uuid   SERIAL PRIMARY KEY,
    st_id     TEXT UNIQUE NOT NULL,
    boxtype   TEXT,
    exposure  TEXT,
    model     TEXT,
    geometry  GEOMETRY(Point, 4326),
    region    TEXT,
    country   TEXT,
    init_date TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sensors (
    se_uuid   SERIAL PRIMARY KEY,
    se_id     TEXT UNIQUE NOT NULL,
    st_uuid   INTEGER REFERENCES stations(st_uuid),
    title     TEXT,
    unit      TEXT,
    info      TEXT,
    type      TEXT,
    init_date TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS readings (
    se_uuid  INTEGER REFERENCES sensors(se_uuid),
    time     TIMESTAMPTZ NOT NULL,
    value    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (se_uuid, time)
);

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
    station_id_map: dict,
    sensor_id_map: dict,
    include_schema: bool = True,
) -> None:
    """Write a single SQL file containing stations, sensors, and readings.

    station_id_map / sensor_id_map must already be populated (by the caller)
    before this function is called so that cross-table references are correct
    even when the data is split across multiple files.
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
                st_id, boxtype, model, lon, lat, region, country, init_date = row
                idx = station_id_map[st_id]
                geom = (
                    f"ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)"
                    if lon is not None and lat is not None
                    else "NULL"
                )
                f.write(
                    f"INSERT INTO stations (st_uuid, st_id, boxtype, model, geometry, region, country, init_date) "
                    f"VALUES ({idx}, {_sql_literal(st_id)}, {_sql_literal(boxtype)}, {_sql_literal(model)}, "
                    f"{geom}, {_sql_literal(region)}, {_sql_literal(country)}, "
                    f"{_sql_literal(str(init_date) if init_date else None)}) "
                    f"ON CONFLICT (st_id) DO NOTHING;\n"
                )
            f.write("COMMIT;\n\n")

        # -- Sensors ---------------------------------------------------------
        if sensor_rows:
            f.write("-- sensors\n")
            f.write("BEGIN;\n")
            for row in sensor_rows:
                se_id, st_id, title, unit, info, stype, init_date = row
                idx      = sensor_id_map[se_id]
                st_uuid  = station_id_map.get(st_id, "NULL")
                f.write(
                    f"INSERT INTO sensors (se_uuid, se_id, st_uuid, title, unit, info, type, init_date) "
                    f"VALUES ({idx}, {_sql_literal(se_id)}, {_sql_literal(st_uuid)}, {_sql_literal(title)}, "
                    f"{_sql_literal(unit)}, {_sql_literal(info)}, {_sql_literal(stype)}, "
                    f"{_sql_literal(str(init_date) if init_date else None)}) "
                    f"ON CONFLICT (se_id) DO NOTHING;\n"
                )
            f.write("COMMIT;\n\n")

        # -- Readings -- batched transactions of 5 000 rows ------------------
        if reading_rows:
            f.write("-- readings\n")
            for batch_start in range(0, len(reading_rows), BATCH):
                f.write("BEGIN;\n")
                for row in reading_rows[batch_start : batch_start + BATCH]:
                    se_id, time_str, value = row
                    se_uuid = sensor_id_map.get(se_id, "NULL")
                    f.write(
                        f"INSERT INTO readings (se_uuid, time, value) "
                        f"VALUES ({_sql_literal(se_uuid)}, {_sql_literal(time_str)}, {_sql_literal(value)}) "
                        f"ON CONFLICT (se_uuid, time) DO NOTHING;\n"
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
    station_rows = buffers["stations"]   # [st_id, boxtype, model, lon, lat, region, country, init_date]
    sensor_rows  = buffers["sensors"]    # [se_id, st_id, title, unit, info, type, init_date]
    reading_rows = buffers["readings"]   # [se_id, time, value]

    # Build global id maps (1-based serials) -- must be consistent across all parts
    station_id_map: dict[str, int] = {row[0]: idx for idx, row in enumerate(station_rows, start=1)}
    sensor_id_map:  dict[str, int] = {row[0]: idx for idx, row in enumerate(sensor_rows,  start=1)}

    # ------------------------------------------------------------------ single file
    if not num_parts or num_parts <= 1:
        out_path = SQL_DUMP_DIR / f"{base_name}.sql"
        _write_sql_file(
            out_path,
            station_rows, sensor_rows, reading_rows,
            station_id_map, sensor_id_map,
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
            station_id_map, sensor_id_map,
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
        files = [a["href"] for a in soup.find_all("a", href=True)
                 if not a["href"].startswith(("?", "/", "."))]
    else:
        folder_path = Path(source) / date_folder / station_folder
        station_folder_path = str(folder_path.resolve())
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

    # Inject the archive folder date as the station's init_date (first-seen date)
    folder_date = datetime.strptime(date_folder, "%Y-%m-%d").date()
    station_data["_init_date"] = folder_date

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
            folder_date,
        ])
    else:
        # Open one cursor for the entire station (station + all sensors + all readings)
        # and commit once at the end -- vastly fewer round-trips vs. per-sensor commits.
        with conn.cursor() as cur:
            st_uuid, is_new_station, station_country, skip_reason = upsert_station(cur, station_data)
            if st_uuid is None:
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

                # -- Readings CSV -- parse early to extract earliest timestamp -
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

                # Derive sensor init_date from the earliest timestamp in the CSV
                sensor_init_date = None
                for row in rows:
                    try:
                        t = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
                        if sensor_init_date is None or t < sensor_init_date:
                            sensor_init_date = t
                    except (KeyError, ValueError):
                        continue

                se_uuid, is_new_sensor = upsert_sensor(cur, sensor, st_uuid, init_date=sensor_init_date)
                if se_uuid is None:
                    continue
                if is_new_sensor and sensor_init_date is not None:
                    year = sensor_init_date.year
                    counts["new_sensors_by_year"][year] = counts["new_sensors_by_year"].get(year, 0) + 1

                if rows:
                    n = insert_readings(cur, se_uuid, rows)
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

        sensor_init_date = None
        for row in rows:
            try:
                t = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
                if sensor_init_date is None or t < sensor_init_date:
                    sensor_init_date = t
            except (KeyError, ValueError):
                continue

        active_buf["sensors"].append([
            se_id,
            station_data.get("id"),
            sensor.get("title"),
            sensor.get("unit"),
            sensor.get("sensorType"),
            classify_sensor(sensor.get("title", ""), sensor.get("unit", "")),
            sensor_init_date,
        ])

        if rows:
            for row in rows:
                active_buf["readings"].append([se_id, row.get("createdAt"), row.get("value")])
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
    source   = args.source
    dry_run  = args.dry_run
    sql_mode = (args.output_type == "sql")
    start    = parse_date(args.start)
    end      = parse_date(args.end)
    workers  = args.workers
    num_parts = args.part if sql_mode else None

    load_env(args.env)
    load_admin_boundaries()  # load once into _ADMIN_FEATURES

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

    print(f"[INFO] Date folders to process: {len(date_folders)}")
    if start or end:
        print(f"       Range: {start or 'beginning'} -> {end or 'end'}")

    # -- Progress log (resume support) ---------------------------------------
    log_path = progress_log_path(source, start, end)
    init_progress_log(log_path, source, start, end)
    completed = load_completed(log_path)
    location_error_path = error_log_path(source, start, end)
    init_error_log(location_error_path, source, start, end)

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

    for date_folder in tqdm(date_folders, desc="Date folders", unit="day"):
        # Discover station folders
        if is_remote:
            date_url = urljoin(source.rstrip("/") + "/", date_folder + "/")
            station_folders = list_url_subdirs(date_url)
        else:
            station_folders = list_local_subdirs(Path(source) / date_folder)

        if workers == 1:
            # Single-threaded path -- preserves original behaviour exactly
            for station_folder in tqdm(
                station_folders,
                desc=f"  {date_folder}",
                unit="station",
                leave=False,
            ):
                key = f"{date_folder}/{station_folder}"
                if key in completed:
                    continue  # already done in a previous run -- skip
                counts = process_station_folder(
                    source, date_folder, station_folder,
                    is_remote, conn, dry_buffers, sql_buffers,
                    location_error_path,
                )
                _merge_counts(counts)
                mark_completed(log_path, date_folder, station_folder)
                completed.add(key)
        else:
            # Parallel path -- each station folder processed in its own thread.
            # DB connections are not thread-safe, so each worker gets its own
            # connection (or None for dry-run/sql). Buffers are protected by a
            # lock since multiple threads append to shared lists.
            import threading
            buf_lock = threading.Lock() if (dry_buffers is not None or sql_buffers is not None) else None
            log_lock = threading.Lock()  # protect progress log writes

            # Filter out already-completed station folders before dispatching
            pending_stations = [
                sf for sf in station_folders
                if f"{date_folder}/{sf}" not in completed
            ]
            skipped_count = len(station_folders) - len(pending_stations)
            if skipped_count:
                tqdm.write(f"  [RESUME] {date_folder}: skipping {skipped_count} already-completed stations")

            def _worker(station_folder: str) -> dict:
                if dry_buffers is not None or sql_buffers is not None:
                    # Thread-local buffer; merged into shared buffer under lock
                    local_buf = {"stations": [], "sensors": [], "readings": []}
                    local_dry = local_buf if dry_buffers is not None else None
                    local_sql = local_buf if sql_buffers is not None else None
                    c = process_station_folder(
                        source, date_folder, station_folder,
                        is_remote, None, local_dry, local_sql,
                        location_error_path,
                        log_lock,
                    )
                    shared_buf = dry_buffers if dry_buffers is not None else sql_buffers
                    with buf_lock:
                        for key in local_buf:
                            shared_buf[key].extend(local_buf[key])
                    with log_lock:
                        mark_completed(log_path, date_folder, station_folder)
                    return c
                else:
                    # Each thread uses its own DB connection (silent -- no log spam)
                    thread_conn = get_db_connection(verbose=False)
                    try:
                        result = process_station_folder(
                            source, date_folder, station_folder,
                            is_remote, thread_conn, None, None,
                            location_error_path,
                            log_lock,
                        )
                        with log_lock:
                            mark_completed(log_path, date_folder, station_folder)
                        return result
                    finally:
                        thread_conn.close()

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_worker, sf): sf
                    for sf in pending_stations
                }
                pbar = tqdm(
                    total=len(pending_stations),
                    desc=f"  {date_folder}",
                    unit="station",
                    leave=False,
                )
                for future in as_completed(futures):
                    try:
                        counts = future.result()
                        _merge_counts(counts)
                    except Exception as exc:
                        sf = futures[future]
                        print(f"\n  [ERROR] {sf}: {exc}")
                    finally:
                        pbar.update(1)
                pbar.close()

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
    print("\n" + "=" * 50)
    print("  Load complete")
    print("=" * 50)
    print(f"  Stations added     : {total['stations']:>10,}")
    print(f"  Sensors added      : {total['sensors']:>10,}")
    print(f"  Readings added     : {total['readings']:>10,}")
    print(f"  CSVs not found     : {total['skipped_csv']:>10,}")
    print(f"  No location       : {total['skipped_no_location']:>10,}")
    print(f"  Bad location      : {total['skipped_bad_location']:>10,}")
    print(f"  Logged errors     : {location_error_path}")
    print("=" * 50)

    if conn:
        conn.close()

    print_sensor_year_chart(total["new_sensors_by_year"])
    print_station_country_chart(total["new_stations_by_country"])

    print(f"\n[RESUME] Progress log : {log_path}")
    print(f"[RESUME] To restart from scratch, delete that file and re-run.")


if __name__ == "__main__":
    main()
