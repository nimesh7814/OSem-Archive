"""
indexer.py
==========
OpenSenseMap Archive Indexer
-----------------------------
Crawls https://archive.opensensemap.org/ day by day and populates the
osem_index_db database with:

  - stations        : one row per unique senseBox
  - sensors         : one row per unique sensor, linked to its station
  - station_dates   : which dates a station was active + folder URL
  - sensor_files    : the per-sensor CSV URL for every (sensor, date) pair
  - index_log       : one summary row per indexed date (used for resume)

Features
--------
  --from / --to     date range (inclusive); --start / --end are accepted aliases
  --date            single date shortcut
  --last N          last N days
                    Crash recovery is automatic — re-running the same command
                    always continues from the last successfully indexed day.
                    The .resume sidecar file (next to the log) is checked first,
                    then the index_log table as a fallback.
  --skip-done       skip individual dates already marked 'success'
  --dry             dry-run: fetch & parse, print what would be written,
                    no DB writes at all
  --log FILE        write full log to FILE (default: indexer.log)
                    A companion FILE.resume tracks the last successful date
                    for automatic crash recovery.
  -v / --verbose    DEBUG level logging

Usage
-----
    # Date range  (--from/--to  or  --start/--end — both work)
    python indexer.py --from 2024-01-01 --to 2024-01-31
    python indexer.py --start 2024-01-01 --end 2024-01-31

    # Resume after crash — just re-run the exact same command
    python indexer.py --start 2024-01-01 --end 2024-01-31

    # Dry run — verify connectivity & parsing without touching the DB
    python indexer.py --from 2024-01-01 --to 2024-01-03 --dry

    # Last 7 days, skip already-done dates
    python indexer.py --last 7 --skip-done

    # Single date with verbose output
    python indexer.py --date 2024-06-15 -v

Dependencies
------------
    pip install requests psycopg2-binary shapely tqdm python-dotenv

Environment (.env or shell)
---------------------------
    POSTGRES_HOST      default: localhost
    POSTGRES_PORT      default: 5436
    POSTGRES_DB        default: osem_index_db
    POSTGRES_USER      default: osem_index
    POSTGRES_PASSWORD  (required)
    GEOJSON_PATH       path to admin_boundary.geojson
                       default: ./data/admin_boundary.geojson
    ARCHIVE_BASE_URL   default: https://archive.opensensemap.org
    CONCURRENCY        HTTP workers per day, default: 8
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from tqdm import tqdm

# ─── optional sensor_types import ────────────────────────────────────────────
try:
    from sensor_types import categorize_sensor
except ImportError:
    def categorize_sensor(title: str, unit: str) -> str:  # type: ignore[misc]
        return "other"

# ─── load .env early so constants pick up env values ─────────────────────────
load_dotenv()

# ─── constants ────────────────────────────────────────────────────────────────
ARCHIVE_BASE    = os.getenv("ARCHIVE_BASE_URL", "https://archive.opensensemap.org").rstrip("/")
HTTP_TIMEOUT    = 20
HTTP_RETRIES    = 3
HTTP_BACKOFF    = 2.0
CONCURRENCY     = int(os.getenv("CONCURRENCY", "8"))
DEFAULT_LOG_FILE = "indexer.log"


# ─── resume-file helpers ──────────────────────────────────────────────────────

def resume_file_path(log_file: str) -> Path:
    """Return the .resume sidecar path next to the log file."""
    return Path(log_file).with_suffix(".resume")


def read_resume_file(log_file: str) -> Optional[date]:
    """
    Read the last-successfully-indexed date from the .resume sidecar file.
    Returns None if the file does not exist or is unreadable.
    """
    rf = resume_file_path(log_file)
    try:
        text = rf.read_text(encoding="utf-8").strip()
        return date.fromisoformat(text)
    except Exception:
        return None


def write_resume_file(log_file: str, d: date) -> None:
    """
    Atomically write the last-successfully-indexed date to the .resume file
    so that a bare --resume run always knows exactly where to continue.
    """
    rf = resume_file_path(log_file)
    try:
        tmp = rf.with_suffix(".resume.tmp")
        tmp.write_text(d.isoformat() + "\n", encoding="utf-8")
        tmp.replace(rf)          # atomic on POSIX; best-effort on Windows
    except Exception as exc:
        log.warning("Could not write resume file %s: %s", rf, exc)

# ─── module-level logger — handlers added in setup_logging() ─────────────────
log = logging.getLogger("indexer")


# ══════════════════════════════════════════════════════════════════════════════
# 0.  Logging setup
# ══════════════════════════════════════════════════════════════════════════════

def setup_logging(log_file: str, verbose: bool) -> None:
    """
    Two handlers:
      - File  : always DEBUG, appends to log_file
      - Console: INFO (or DEBUG if --verbose), routes through tqdm.write
                 so progress bars are not broken
    """
    fmt     = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root    = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(fh)

    # Console handler — writes via tqdm so bars stay intact
    class TqdmHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                tqdm.write(self.format(record), file=sys.stderr)
            except Exception:
                self.handleError(record)

    ch = TqdmHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(ch)


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Database helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_connection() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST", "localhost"),
        port     = int(os.getenv("POSTGRES_PORT", "5436")),
        dbname   = os.getenv("POSTGRES_DB", "osem_index_db"),
        user     = os.getenv("POSTGRES_USER", "osem_index"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


def upsert_station(cur,
                   box_id: str,
                   name: Optional[str],
                   lon: Optional[float],
                   lat: Optional[float],
                   country: Optional[str],
                   region: Optional[str],
                   city: Optional[str],
                   first_seen: Optional[date] = None,
                   last_seen: Optional[date]  = None) -> str:
    """Upsert a station row. Returns its UUID."""
    point_wkt = (
        f"SRID=4326;POINT({lon} {lat})"
        if lon is not None and lat is not None else None
    )
    cur.execute(
        """
        INSERT INTO stations
            (box_id, name, location, country, region, city, fs_date, ls_date)
        VALUES
            (%(box_id)s, %(name)s, %(point)s::geometry,
             %(country)s, %(region)s, %(city)s, %(fs)s, %(ls)s)
        ON CONFLICT (box_id) DO UPDATE SET
            name     = COALESCE(EXCLUDED.name,     stations.name),
            location = COALESCE(EXCLUDED.location, stations.location),
            country  = COALESCE(EXCLUDED.country,  stations.country),
            region   = COALESCE(EXCLUDED.region,   stations.region),
            city     = COALESCE(EXCLUDED.city,     stations.city),
            fs_date  = LEAST(   COALESCE(stations.fs_date, EXCLUDED.fs_date), EXCLUDED.fs_date),
            ls_date  = GREATEST(COALESCE(stations.ls_date, EXCLUDED.ls_date), EXCLUDED.ls_date)
        RETURNING uuid
        """,
        {"box_id": box_id, "name": name, "point": point_wkt,
         "country": country, "region": region, "city": city,
         "fs": first_seen, "ls": last_seen},
    )
    return cur.fetchone()[0]


def upsert_sensor(cur,
                  sensor_id: str,
                  station_uuid: str,
                  title: Optional[str],
                  sensor_type: Optional[str],
                  category: str,
                  unit: Optional[str]) -> str:
    """Upsert a sensor row. Returns its UUID."""
    cur.execute(
        """
        INSERT INTO sensors (sensor_id, station_uuid, title, type, category, unit)
        VALUES (%(sid)s, %(st)s, %(title)s, %(stype)s, %(cat)s, %(unit)s)
        ON CONFLICT (sensor_id) DO UPDATE SET
            title    = COALESCE(EXCLUDED.title,    sensors.title),
            type     = COALESCE(EXCLUDED.type,     sensors.type),
            category = EXCLUDED.category,
            unit     = COALESCE(EXCLUDED.unit,     sensors.unit)
        RETURNING uuid
        """,
        {"sid": sensor_id, "st": station_uuid, "title": title,
         "stype": sensor_type, "cat": category, "unit": unit},
    )
    return cur.fetchone()[0]


def upsert_station_date(cur, station_uuid: str, d: date, folder_url: str) -> None:
    cur.execute(
        """
        INSERT INTO station_dates (station_uuid, date, folder_url)
        VALUES (%s, %s, %s)
        ON CONFLICT (station_uuid, date) DO UPDATE SET
            folder_url = EXCLUDED.folder_url
        """,
        (station_uuid, d, folder_url),
    )


def upsert_sensor_file(cur,
                       sensor_uuid: str,
                       station_uuid: str,
                       d: date,
                       csv_url: str) -> None:
    cur.execute(
        """
        INSERT INTO sensor_files (sensor_uuid, station_uuid, date, csv_url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (sensor_uuid, date) DO UPDATE SET
            csv_url = EXCLUDED.csv_url
        """,
        (sensor_uuid, station_uuid, d, csv_url),
    )


def write_index_log(cur,
                    d: date,
                    station_count: int,
                    status: str) -> None:
    cur.execute(
        """
        INSERT INTO index_log (date, station_count, status)
        VALUES (%s, %s, %s)
        ON CONFLICT (date) DO UPDATE SET
            indexed_at    = now(),
            station_count = EXCLUDED.station_count,
            status        = EXCLUDED.status
        """,
        (d, station_count, status),
    )


def get_last_success(cur, start: date, end: date) -> Optional[date]:
    """
    Return the most-recent date in [start, end] that is marked 'success',
    or None if there is none.  Used by --resume.
    """
    cur.execute(
        """
        SELECT MAX(date) FROM index_log
        WHERE status = 'success'
          AND date >= %s
          AND date <= %s
        """,
        (start, end),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def day_already_done(cur, d: date) -> bool:
    cur.execute(
        "SELECT 1 FROM index_log WHERE date = %s AND status = 'success'",
        (d,),
    )
    return cur.fetchone() is not None


# ══════════════════════════════════════════════════════════════════════════════
# 2.  GeoJSON spatial index  (country / region lookup)
# ══════════════════════════════════════════════════════════════════════════════

class GeoLookup:
    """
    Loads admin_boundary.geojson into a Shapely STRtree for fast
    point-in-polygon queries.
    Expected feature properties: adm0_name (country), adm1_name (region).
    """

    def __init__(self, geojson_path: str) -> None:
        path = Path(geojson_path)
        if not path.exists():
            log.warning(
                "GeoJSON not found at %s — country/region will be NULL", path
            )
            self._tree  = None
            self._geoms: list = []
            self._props: list = []
            return

        log.info("Loading GeoJSON from %s …", path)
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)

        self._geoms, self._props = [], []
        for feature in data.get("features", []):
            try:
                self._geoms.append(shape(feature["geometry"]))
                self._props.append(feature.get("properties", {}))
            except Exception:
                pass

        self._tree = STRtree(self._geoms) if self._geoms else None
        log.info("GeoJSON loaded: %d polygons", len(self._geoms))

    def lookup(self,
               lon: float,
               lat: float) -> tuple[Optional[str], Optional[str]]:
        if self._tree is None:
            return None, None
        pt = Point(lon, lat)
        for idx in self._tree.query(pt):
            if self._geoms[idx].contains(pt):
                p = self._props[idx]
                return p.get("adm0_name"), p.get("adm1_name")
        return None, None


# ══════════════════════════════════════════════════════════════════════════════
# 3.  HTTP helpers
# ══════════════════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({"User-Agent": "osem-indexer/2.0 (study-project)"})


def _get(url: str, as_json: bool) -> Optional[str | dict]:
    delay = HTTP_BACKOFF
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = _session.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as exc:
            if attempt == HTTP_RETRIES:
                log.debug("Failed %s after %d tries: %s", url, attempt, exc)
                return None
            time.sleep(delay)
            delay *= 2
    return None


def http_get_json(url: str) -> Optional[dict]:
    return _get(url, as_json=True)  # type: ignore[return-value]


def http_get_text(url: str) -> Optional[str]:
    return _get(url, as_json=False)  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Archive parsing
# ══════════════════════════════════════════════════════════════════════════════

def list_date_folders(d: date) -> list[tuple[str, str]]:
    """
    Fetch the archive day listing.
    Returns [(box_id, folder_url), …].

    Caddy serves relative hrefs in the form:
        href="./53c57d64e79c90a0102f6b9c-PaderBox/"
    """
    url  = f"{ARCHIVE_BASE}/{d.isoformat()}/"
    text = http_get_text(url)
    if not text:
        return []

    # Match  href="./FOLDER/"  (Caddy relative)
    # Also catch bare  href="FOLDER/"  and absolute/root-relative as fallback
    pattern = re.compile(r'href="\.?/?([0-9a-f]{24}-[^/"]+)/"')
    results = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        folder_name = m.group(1)
        if folder_name in seen:
            continue
        seen.add(folder_name)
        box_id     = folder_name.split("-", 1)[0]
        folder_url = f"{ARCHIVE_BASE}/{d.isoformat()}/{folder_name}/"
        results.append((box_id, folder_url))
    return results


def fetch_station_meta(folder_url: str) -> Optional[dict]:
    """Fetch the station JSON metadata file from its folder.
    Caddy serves relative hrefs like:  href="./BOXID-STATIONNAME-DATE.json"
    """
    text = http_get_text(folder_url)
    if not text:
        return None
    # Match any href containing a .json filename (relative or absolute)
    m = re.search(r'href="[^"]*?([^/"]+\.json)"', text)
    if not m:
        return None
    filename  = m.group(1)
    json_url  = folder_url.rstrip("/") + "/" + filename
    return http_get_json(json_url)


def csv_urls_from_folder(folder_url: str, d: date) -> list[tuple[str, str]]:
    """Return [(sensor_id, csv_url), …] for all CSVs in a station folder.
    Caddy serves relative hrefs like:  href="./SENSORID-DATE.csv"
    """
    text = http_get_text(folder_url)
    if not text:
        return []
    date_str = d.isoformat()
    # Match any href that ends with SENSORID-DATE.csv
    # Caddy format: href="./53c57d64e79c90a0102f6b9d-2014-08-03.csv"
    pattern = re.compile(
        r'href="[^"]*?([0-9a-f]{24}-' + re.escape(date_str) + r'\.csv)"'
    )
    results = []
    for m in pattern.finditer(text):
        href      = m.group(0)          # full href="..." match
        filename  = m.group(1)          # just the SENSORID-DATE.csv part
        sensor_id = filename.replace(f"-{date_str}.csv", "")
        csv_url   = folder_url.rstrip("/") + "/" + filename
        results.append((sensor_id, csv_url))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Process one station  (runs in thread pool)
# ══════════════════════════════════════════════════════════════════════════════

def process_station(args: tuple) -> Optional[dict]:
    box_id, folder_url, d, geo_lookup = args

    meta = fetch_station_meta(folder_url)

    # coordinates
    lon = lat = None
    if meta:
        coords = meta.get("currentLocation") or meta.get("loc") or {}
        if isinstance(coords, dict):
            geom = coords.get("geometry", {})
            if isinstance(geom, dict):
                c = geom.get("coordinates", [])
                if len(c) >= 2:
                    lon, lat = float(c[0]), float(c[1])
        if lon is None and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])

    # country / region via spatial lookup
    country = region = None
    if lon is not None and lat is not None and geo_lookup:
        country, region = geo_lookup.lookup(lon, lat)

    # station name & per-sensor metadata
    name        = meta.get("name") if meta else None
    sensor_meta: dict[str, dict] = {}
    if meta:
        for s in meta.get("sensors", []):
            sid = s.get("_id") or s.get("id")
            if sid:
                sensor_meta[sid] = {
                    "title": s.get("title"),
                    "type":  s.get("sensorType"),
                    "unit":  s.get("unit"),
                }

    csv_files = csv_urls_from_folder(folder_url, d)

    return {
        "box_id":      box_id,
        "name":        name,
        "lon":         lon,
        "lat":         lat,
        "country":     country,
        "region":      region,
        "city":        None,       # not in archive JSON
        "folder_url":  folder_url,
        "sensor_meta": sensor_meta,
        "csv_files":   csv_files,  # [(sensor_id, csv_url), …]
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Index one full day
# ══════════════════════════════════════════════════════════════════════════════

def index_day(
    d: date,
    geo_lookup: GeoLookup,
    conn,
    dry_run: bool,
    day_bar: tqdm,
    log_file: str = DEFAULT_LOG_FILE,
) -> dict:
    """
    Index (or dry-run) all stations for one date.
    Returns {status, stations_written, sensors_written, errors}.
    """
    log.info("── %s ──────────────────────────────────────────", d.isoformat())

    stations_list = list_date_folders(d)
    if not stations_list:
        log.warning("No folders found for %s", d.isoformat())
        if not dry_run:
            with conn.cursor() as cur:
                write_index_log(cur, d, 0, "failed")
            conn.commit()
        return {"status": "failed", "stations_written": 0, "sensors_written": 0, "errors": 1}

    total_folders = len(stations_list)
    log.info("  %d station folders", total_folders)

    # ── parallel HTTP fetch ───────────────────────────────────────────────
    tasks        = [(box_id, folder_url, d, geo_lookup)
                    for box_id, folder_url in stations_list]
    results      = []
    fetch_errors = 0

    fetch_bar = tqdm(
        total=total_folders,
        desc=f"  {d.isoformat()} fetch",
        unit="stn",
        leave=False,
        position=1,
    )
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(process_station, t): t for t in tasks}
        for fut in as_completed(futures):
            fetch_bar.update(1)
            try:
                res = fut.result()
                if res:
                    results.append(res)
                else:
                    fetch_errors += 1
            except Exception as exc:
                log.debug("Worker error: %s", exc)
                fetch_errors += 1
    fetch_bar.close()

    total_sensors = sum(len(r["csv_files"]) for r in results)
    log.info("  Fetched %d/%d stations  %d sensors  %d fetch-errors",
             len(results), total_folders, total_sensors, fetch_errors)

    # ── dry run: show what would happen, stop here ────────────────────────
    if dry_run:
        log.info("  [DRY RUN] Would insert/update %d stations, %d sensors",
                 len(results), total_sensors)
        # Sample output: first 3 stations
        for r in results[:3]:
            log.info("    station sample: box_id=%-26s name=%s  country=%s  sensors=%d",
                     r["box_id"], r["name"] or "-", r["country"] or "?",
                     len(r["csv_files"]))
        if len(results) > 3:
            log.info("    … and %d more", len(results) - 3)
        day_bar.set_postfix_str(
            f"DRY  stn={len(results)}  sens={total_sensors}"
        )
        return {
            "status":           "dry",
            "stations_written": len(results),
            "sensors_written":  total_sensors,
            "errors":           fetch_errors,
        }

    # ── DB writes ─────────────────────────────────────────────────────────
    stations_written = sensors_written = db_errors = 0

    write_bar = tqdm(
        total=len(results),
        desc=f"  {d.isoformat()} write ",
        unit="stn",
        leave=False,
        position=1,
    )
    with conn.cursor() as cur:
        for r in results:
            write_bar.update(1)
            try:
                station_uuid = upsert_station(
                    cur,
                    r["box_id"], r["name"],
                    r["lon"], r["lat"],
                    r["country"], r["region"], r["city"],
                    first_seen=d, last_seen=d,
                )
                upsert_station_date(cur, station_uuid, d, r["folder_url"])

                for sensor_id, csv_url in r["csv_files"]:
                    sm       = r["sensor_meta"].get(sensor_id, {})
                    title    = sm.get("title")
                    stype    = sm.get("type")
                    unit     = sm.get("unit")
                    category = categorize_sensor(title or "", unit or "")

                    sensor_uuid = upsert_sensor(
                        cur, sensor_id, station_uuid,
                        title, stype, category, unit,
                    )
                    upsert_sensor_file(cur, sensor_uuid, station_uuid, d, csv_url)
                    sensors_written += 1

                stations_written += 1

            except Exception as exc:
                log.debug("DB error for box %s: %s", r["box_id"], exc)
                conn.rollback()
                db_errors += 1
                continue

        errors = fetch_errors + db_errors
        status = (
            "success" if errors == 0 else
            ("partial" if stations_written > 0 else "failed")
        )
        write_index_log(cur, d, stations_written, status)
        conn.commit()
        # ── persist resume checkpoint so a crash on the NEXT day can recover ──
        if status in ("success", "partial"):
            write_resume_file(log_file, d)

    write_bar.close()
    log.info("  Written: %d stations  %d sensors  %d errors → %s",
             stations_written, sensors_written, errors, status)
    day_bar.set_postfix_str(
        f"stn={stations_written}  sens={sensors_written}  err={errors}"
    )
    return {
        "status":           status,
        "stations_written": stations_written,
        "sensors_written":  sensors_written,
        "errors":           errors,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7.  CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OpenSenseMap archive indexer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python indexer.py --from 2024-01-01 --to 2024-01-31
  python indexer.py --start 2024-01-01 --end 2024-01-31   # aliases for --from/--to
  python indexer.py --from 2024-01-01 --to 2024-01-31     # re-run resumes automatically
  python indexer.py --from 2024-01-01 --to 2024-01-03 --dry
  python indexer.py --last 7 --skip-done
  python indexer.py --date 2024-06-15 -v
        """,
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--date",  metavar="YYYY-MM-DD",
                      help="Index a single date")
    mode.add_argument("--from", "--start", dest="date_from", metavar="YYYY-MM-DD",
                      help="Start of date range, inclusive (use with --to / --end)")
    mode.add_argument("--last",  type=int, metavar="N",
                      help="Index the last N days (today-N … yesterday)")

    p.add_argument("--to", "--end", dest="date_to", metavar="YYYY-MM-DD",
                   help="End of date range, inclusive (required with --from / --start)")

    p.add_argument("--skip-done", action="store_true",
                   help="Skip individual dates already marked 'success'")
    p.add_argument("--dry", action="store_true",
                   help="Fetch and parse only — no DB writes")
    p.add_argument("--log", metavar="FILE", default=DEFAULT_LOG_FILE,
                   help=f"Log file path (default: {DEFAULT_LOG_FILE}). "
                        "A companion .resume file is written alongside it "
                        "to enable crash recovery.")
    p.add_argument("--geojson", default=None,
                   help="Path to admin_boundary.geojson "
                        "(overrides GEOJSON_PATH env var)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable DEBUG logging to console")
    return p.parse_args()


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> None:
    args = parse_args()
    setup_logging(args.log, args.verbose)

    log.info("=" * 60)
    log.info("OpenSenseMap Indexer  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Log file   : %s", Path(args.log).resolve())
    log.info("Resume file: %s", resume_file_path(args.log).resolve())
    if args.dry:
        log.info("Mode       : DRY RUN — no DB writes")
    log.info("=" * 60)

    # ── resolve date list ─────────────────────────────────────────────────
    today = date.today()
    if args.date:
        days = [date.fromisoformat(args.date)]
    elif args.last:
        days = list(date_range(
            today - timedelta(days=args.last),
            today - timedelta(days=1),
        ))
    else:
        # --from / --start
        if not args.date_to:
            log.error("--to / --end is required when using --from / --start")
            sys.exit(1)
        days = list(date_range(
            date.fromisoformat(args.date_from),
            date.fromisoformat(args.date_to),
        ))

    full_range_days = len(days)
    log.info("Range      : %s → %s  (%d days)",
             days[0].isoformat(), days[-1].isoformat(), full_range_days)

    # ── geo lookup ────────────────────────────────────────────────────────
    geojson_path = (
        args.geojson
        or os.getenv("GEOJSON_PATH", "./data/admin_boundary.geojson")
    )
    geo = GeoLookup(geojson_path)

    # ── DB connection (dry-run skips this) ────────────────────────────────
    conn = None
    if not args.dry:
        log.info("Connecting to %s:%s/%s …",
                 os.getenv("POSTGRES_HOST", "localhost"),
                 os.getenv("POSTGRES_PORT", "5436"),
                 os.getenv("POSTGRES_DB", "osem_index_db"))
        try:
            conn = get_connection()
            psycopg2.extras.register_uuid()
            log.info("DB connection OK")
        except Exception as exc:
            log.error("Cannot connect to database: %s", exc)
            sys.exit(1)

        # ── auto-resume: always skip past already-completed days ──────
        # Priority 1: .resume sidecar file (survives a hard crash)
        # Priority 2: index_log table in the DB
        # No flag needed — re-running the same command always continues safely.
        if len(days) > 1:
            last_ok: Optional[date] = None

            file_resume = read_resume_file(args.log)
            if file_resume and days[0] <= file_resume <= days[-1]:
                last_ok = file_resume
                log.info("Auto-resume: .resume file → last success = %s", last_ok.isoformat())
            else:
                with conn.cursor() as cur:
                    last_ok = get_last_success(cur, days[0], days[-1])
                if last_ok:
                    log.info("Auto-resume: index_log  → last success = %s", last_ok.isoformat())

            if last_ok:
                skipped = [d for d in days if d <= last_ok]
                days    = [d for d in days if d >  last_ok]
                log.info(
                    "Auto-resume: skipping %d day(s); resuming from %s",
                    len(skipped),
                    days[0].isoformat() if days else "(nothing left)",
                )
            else:
                log.info("Auto-resume: no prior progress found; starting from %s",
                         days[0].isoformat())

    if not days:
        log.info("Nothing left to index — all days already done.")
        if conn:
            conn.close()
        sys.exit(0)

    days_remaining = len(days)
    days_done_before = full_range_days - days_remaining
    log.info("Days to process: %d  (skipped/done: %d)", days_remaining, days_done_before)

    # ── outer progress bar (days) ─────────────────────────────────────────
    summary = {
        "success":        0,
        "partial":        0,
        "failed":         0,
        "dry":            0,
        "skipped":        0,
        "total_stations": 0,
        "total_sensors":  0,
        "total_errors":   0,
    }

    day_bar = tqdm(
        total=full_range_days,
        initial=days_done_before,
        desc="Overall",
        unit="day",
        position=0,
        bar_format=(
            "{l_bar}{bar}| {n_fmt}/{total_fmt} days "
            "[elapsed {elapsed} / ETA {remaining}  {rate_fmt}]  {postfix}"
        ),
    )

    for d in days:
        days_left = full_range_days - (days_done_before + summary["success"]
                                       + summary["partial"] + summary["failed"]
                                       + summary["dry"] + summary["skipped"])
        day_bar.set_description(
            f"Day {d.isoformat()}  "
            f"✓{summary['success']} ~{summary['partial']} ✗{summary['failed']}"
        )

        # per-day skip-done check
        if args.skip_done and not args.dry and conn:
            with conn.cursor() as cur:
                if day_already_done(cur, d):
                    log.info("Skipping %s (already success)", d.isoformat())
                    summary["skipped"] += 1
                    day_bar.set_postfix_str(
                        f"skipped  stn={summary['total_stations']:,}"
                        f"  sens={summary['total_sensors']:,}"
                        f"  err={summary['total_errors']:,}"
                    )
                    day_bar.update(1)
                    continue

        result = index_day(
            d, geo, conn,
            dry_run=args.dry,
            day_bar=day_bar,
            log_file=args.log,
        )
        status = result["status"]
        summary[status]               = summary.get(status, 0) + 1
        summary["total_stations"]    += result["stations_written"]
        summary["total_sensors"]     += result["sensors_written"]
        summary["total_errors"]      += result["errors"]

        days_processed = (summary["success"] + summary["partial"]
                          + summary["failed"] + summary["dry"]
                          + summary["skipped"])
        days_left      = full_range_days - days_done_before - days_processed
        day_bar.set_postfix_str(
            f"{status}  "
            f"left={days_left}d  "
            f"stn={summary['total_stations']:,}  "
            f"sens={summary['total_sensors']:,}  "
            f"err={summary['total_errors']:,}"
        )
        day_bar.update(1)

    day_bar.close()

    # ── final summary ─────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("Indexing complete")
    log.info("  %-12s %d", "success:",  summary["success"])
    log.info("  %-12s %d", "partial:",  summary["partial"])
    log.info("  %-12s %d", "failed:",   summary["failed"])
    log.info("  %-12s %d", "dry:",      summary["dry"])
    log.info("  %-12s %d", "skipped:",  summary["skipped"])
    log.info("  %-12s %d", "stations:", summary["total_stations"])
    log.info("  %-12s %d", "sensors:",  summary["total_sensors"])
    log.info("  %-12s %d", "errors:",   summary["total_errors"])
    log.info("=" * 60)

    if conn:
        conn.close()

    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
