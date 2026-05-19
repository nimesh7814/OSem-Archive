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

Schema 2 notes
--------------
  - stations PK  : st_id  (TEXT, the OpenSenseMap box id)
  - sensors  PK  : se_id  (TEXT, the OpenSenseMap sensor id)
  - index_log PK : inx_id
  - readings  PK : re_id
  - No UUIDs anywhere — all FKs use the natural text IDs directly.

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
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
from rich.panel import Panel
from rich.text import Text
from rich import box as rich_box

# ─── optional sensor_types import ────────────────────────────────────────────
try:
    from sensor_types import categorize_sensor
except ImportError:
    def categorize_sensor(title: str, unit: str) -> str:  # type: ignore[misc]
        return "other"

# ─── load .env early so constants pick up env values ─────────────────────────
load_dotenv()

# ─── constants ────────────────────────────────────────────────────────────────
ARCHIVE_BASE     = os.getenv("ARCHIVE_BASE_URL", "https://archive.opensensemap.org").rstrip("/")
HTTP_TIMEOUT     = 20
HTTP_RETRIES     = 3
HTTP_BACKOFF     = 2.0
CONCURRENCY      = int(os.getenv("CONCURRENCY", "8"))
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
      - File   : always DEBUG, appends to log_file
      - Console: WARNING only by default (rich Live display takes over);
                 DEBUG if --verbose
    """
    from rich.logging import RichHandler

    fmt     = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root    = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — full detail always
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(fh)

    # Console handler — only warnings/errors reach the terminal;
    # the rich Live panel shows progress. Use DEBUG if --verbose.
    ch = RichHandler(
        level=logging.DEBUG if verbose else logging.WARNING,
        show_time=False,
        show_path=False,
        markup=False,
    )
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
                   exposure: Optional[str] = None,
                   model: Optional[str] = None,
                   first_seen: Optional[date] = None,
                   last_seen: Optional[date]  = None) -> str:
    """
    Upsert a station row.
    Returns st_id (the natural text PK — same as box_id).
    Schema 2: PK is st_id TEXT, no uuid column.
    """
    point_wkt = (
        f"SRID=4326;POINT({lon} {lat})"
        if lon is not None and lat is not None else None
    )
    cur.execute(
        """
        INSERT INTO stations
            (st_id, name, location, country, region, exposure, model, fs_date, ls_date)
        VALUES
            (%(st_id)s, %(name)s, %(point)s::geometry,
             %(country)s, %(region)s, %(exposure)s, %(model)s, %(fs)s, %(ls)s)
        ON CONFLICT (st_id) DO UPDATE SET
            name     = COALESCE(EXCLUDED.name,     stations.name),
            location = COALESCE(EXCLUDED.location, stations.location),
            country  = COALESCE(EXCLUDED.country,  stations.country),
            region   = COALESCE(EXCLUDED.region,   stations.region),
            exposure = COALESCE(EXCLUDED.exposure, stations.exposure),
            model    = COALESCE(EXCLUDED.model,    stations.model),
            fs_date  = LEAST(   COALESCE(stations.fs_date, EXCLUDED.fs_date), EXCLUDED.fs_date),
            ls_date  = GREATEST(COALESCE(stations.ls_date, EXCLUDED.ls_date), EXCLUDED.ls_date)
        RETURNING st_id
        """,
        {"st_id": box_id, "name": name, "point": point_wkt,
         "country": country, "region": region,
         "exposure": exposure, "model": model,
         "fs": first_seen, "ls": last_seen},
    )
    return cur.fetchone()[0]


def upsert_sensor(cur,
                  sensor_id: str,
                  st_id: str,
                  title: Optional[str],
                  sensor_type: Optional[str],
                  category: str,
                  unit: Optional[str]) -> str:
    """
    Upsert a sensor row.
    Returns se_id (the natural text PK — same as sensor_id).
    Schema 2: PK is se_id TEXT, FK to stations.st_id.
    """
    cur.execute(
        """
        INSERT INTO sensors (se_id, st_id, title, type, category, unit)
        VALUES (%(se_id)s, %(st_id)s, %(title)s, %(stype)s, %(cat)s, %(unit)s)
        ON CONFLICT (se_id) DO UPDATE SET
            title    = COALESCE(EXCLUDED.title,    sensors.title),
            type     = COALESCE(EXCLUDED.type,     sensors.type),
            category = EXCLUDED.category,
            unit     = COALESCE(EXCLUDED.unit,     sensors.unit)
        RETURNING se_id
        """,
        {"se_id": sensor_id, "st_id": st_id, "title": title,
         "stype": sensor_type, "cat": category, "unit": unit},
    )
    return cur.fetchone()[0]


def upsert_station_date(cur, st_id: str, d: date, folder_url: str,
                        size_mb: Optional[float] = None) -> None:
    """
    Schema 2: station_dates PK is (st_id, date).
    """
    cur.execute(
        """
        INSERT INTO station_dates (st_id, date, folder_url, size_mb)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (st_id, date) DO UPDATE SET
            folder_url = EXCLUDED.folder_url,
            size_mb    = COALESCE(EXCLUDED.size_mb, station_dates.size_mb)
        """,
        (st_id, d, folder_url, size_mb),
    )


def upsert_sensor_file(cur,
                       se_id: str,
                       st_id: str,
                       d: date,
                       csv_url: str,
                       size_mb: Optional[float] = None) -> None:
    """
    Schema 2: sensor_files PK is (se_id, date), FKs use se_id / st_id.
    """
    cur.execute(
        """
        INSERT INTO sensor_files (se_id, st_id, date, csv_url, size_mb)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (se_id, date) DO UPDATE SET
            csv_url = EXCLUDED.csv_url,
            size_mb = COALESCE(EXCLUDED.size_mb, sensor_files.size_mb)
        """,
        (se_id, st_id, d, csv_url, size_mb),
    )


def upsert_sensor_date(cur,
                       se_id: str,
                       d: date,
                       data_available: int) -> None:
    """
    Upsert a row in sensor_dates for (sensor, date).
    data_available = 1 if a CSV file exists for this sensor on this date,
                     0 if the sensor is known but no data was found.
    Schema 2: PK is (se_id, date).
    """
    cur.execute(
        """
        INSERT INTO sensor_dates (se_id, date, data_available)
        VALUES (%s, %s, %s)
        ON CONFLICT (se_id, date) DO UPDATE SET
            data_available = EXCLUDED.data_available
        """,
        (se_id, d, data_available),
    )


def upsert_reading(cur,
                   se_id: str,
                   st_id: str,
                   d: date,
                   recorded_at,
                   min_val: Optional[float],
                   max_val: Optional[float],
                   avg_val: Optional[float],
                   count: int) -> None:
    """
    Upsert aggregated daily reading stats for a sensor.
    Schema 2: FKs use se_id / st_id; UNIQUE constraint on (se_id, date).
    """
    cur.execute(
        """
        INSERT INTO readings
            (se_id, st_id, date, recorded_at,
             min_value, max_value, avg_value, count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (se_id, date) DO UPDATE SET
            recorded_at = EXCLUDED.recorded_at,
            min_value   = EXCLUDED.min_value,
            max_value   = EXCLUDED.max_value,
            avg_value   = EXCLUDED.avg_value,
            count       = EXCLUDED.count
        """,
        (se_id, st_id, d, recorded_at,
         min_val, max_val, avg_val, count),
    )


def compute_readings(csv_url: str) -> Optional[dict]:
    """
    Fetch a sensor CSV from the archive and compute daily aggregates.

    CSV format (comma-separated, with header):
        createdAt,value

    Returns:
        {recorded_at, min_value, max_value, avg_value, count}
        or None if the file is empty / unparseable.
    """
    import io
    import csv as csv_mod

    text = http_get_text(csv_url)
    if not text:
        log.debug("compute_readings: no text returned for %s", csv_url)
        return None

    log.debug("compute_readings: fetched %d bytes from %s", len(text), csv_url)

    preview = text[:300].replace("\r", "")
    log.debug("compute_readings: preview:\n%s", preview)

    values: list[float] = []
    first_ts = None

    reader = csv_mod.DictReader(io.StringIO(text), delimiter=",")
    log.debug("compute_readings: detected columns: %s", reader.fieldnames)

    for row in reader:
        # timestamp
        raw_ts = row.get("createdAt") or row.get("created_at") or ""
        if first_ts is None and raw_ts:
            try:
                first_ts = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")
                )
            except Exception as e:
                log.debug("compute_readings: ts parse error '%s': %s", raw_ts, e)

        # numeric value
        raw_val = (row.get("value") or "").strip()
        if not raw_val:
            continue
        try:
            values.append(float(raw_val))
        except ValueError:
            log.debug("compute_readings: could not parse value '%s'", raw_val)
            continue

    log.debug("compute_readings: parsed %d values from %s", len(values), csv_url)

    if not values:
        log.debug("compute_readings: no valid values found, skipping reading")
        return None

    return {
        "recorded_at": first_ts,
        "min_value":   min(values),
        "max_value":   max(values),
        "avg_value":   sum(values) / len(values),
        "count":       len(values),
    }


def write_index_log(cur,
                    d: date,
                    station_count: int,
                    status: str) -> None:
    """
    Schema 2: PK column is inx_id (BIGINT GENERATED ALWAYS AS IDENTITY).
    The INSERT itself is unchanged — inx_id is auto-generated.
    """
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
    or None if there is none.  Used by auto-resume.
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


def http_head_size_mb(url: str) -> Optional[float]:
    """
    Issue a HEAD request and return the file size in MB from Content-Length,
    or None if the header is absent or the request fails.
    Falls back to a GET request on servers that don't support HEAD.
    """
    delay = HTTP_BACKOFF
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = _session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            if r.status_code == 405:
                # Server doesn't support HEAD — use GET but discard body
                r = _session.get(url, timeout=HTTP_TIMEOUT, stream=True)
                r.close()
            if r.status_code == 404:
                return None
            r.raise_for_status()
            length = r.headers.get("Content-Length")
            if length is not None:
                return round(int(length) / (1024 * 1024), 6)
            return None
        except Exception as exc:
            if attempt == HTTP_RETRIES:
                log.debug("HEAD failed %s after %d tries: %s", url, attempt, exc)
                return None
            time.sleep(delay)
            delay *= 2
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Archive parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_size_mb(size_str: str) -> Optional[float]:
    """
    Convert a human-readable size string from the archive HTML (e.g. '1.2M',
    '345K', '12', '2.0G') to megabytes.  Returns None if unparseable.
    """
    s = size_str.strip()
    if not s or s == "-":
        return None
    try:
        m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)$", s, re.IGNORECASE)
        if not m:
            return None
        value  = float(m.group(1))
        suffix = m.group(2).upper()
        factor = {"K": 1/1024, "M": 1.0, "G": 1024.0, "T": 1024.0**2}.get(suffix, 1/1048576)
        return round(value * factor, 6)
    except Exception:
        return None


def list_date_folders(d: date) -> list[tuple[str, str, Optional[float]]]:
    """
    Fetch the archive day listing.
    Returns [(box_id, folder_url, size_mb), …].
    size_mb is the total size of the station folder as reported by the
    Content-Length header of a HEAD request against the folder URL,
    or None if not available.
    """
    url  = f"{ARCHIVE_BASE}/{d.isoformat()}/"
    text = http_get_text(url)
    if not text:
        return []

    # Extract folder hrefs — sizes will be fetched via HEAD requests.
    pattern = re.compile(
        r'href="\.?/?([0-9a-f]{24}-[^/"]+)/"',
        re.IGNORECASE,
    )
    results = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        folder_name = m.group(1)
        if folder_name in seen:
            continue
        seen.add(folder_name)
        box_id     = folder_name.split("-", 1)[0]
        folder_url = f"{ARCHIVE_BASE}/{d.isoformat()}/{folder_name}/"
        size_mb    = http_head_size_mb(folder_url)
        results.append((box_id, folder_url, size_mb))
    return results


def fetch_station_meta(folder_url: str) -> Optional[dict]:
    """Fetch the station JSON metadata file from its folder."""
    text = http_get_text(folder_url)
    if not text:
        return None
    m = re.search(r'href="[^"]*?([^/"]+\.json)"', text)
    if not m:
        return None
    filename = m.group(1)
    json_url = folder_url.rstrip("/") + "/" + filename
    return http_get_json(json_url)


def csv_urls_from_folder(folder_url: str, d: date) -> list[tuple[str, str, Optional[float]]]:
    """Return [(sensor_id, csv_url, size_mb), …] for all CSVs in a station folder.

    size_mb is determined from the actual Content-Length header of each CSV
    file (via a HEAD request) so it reflects the real file size on disk rather
    than the approximate human-readable value shown in the directory listing.
    """
    text = http_get_text(folder_url)
    if not text:
        return []
    date_str = d.isoformat()
    pattern = re.compile(
        r'href="[^"]*?([0-9a-f]{24}-' + re.escape(date_str) + r'\.csv)"',
        re.IGNORECASE,
    )
    results = []
    for m in pattern.finditer(text):
        filename  = m.group(1)
        sensor_id = filename.replace(f"-{date_str}.csv", "")
        csv_url   = folder_url.rstrip("/") + "/" + filename
        size_mb   = http_head_size_mb(csv_url)
        results.append((sensor_id, csv_url, size_mb))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Process one station  (runs in thread pool)
# ══════════════════════════════════════════════════════════════════════════════

def process_station(args: tuple) -> Optional[dict]:
    box_id, folder_url, d, geo_lookup, folder_size_mb = args

    meta = fetch_station_meta(folder_url)

    # coordinates — four formats observed in the wild:
    #   1. currentLocation.geometry.coordinates  (newer API export)
    #   2. loc.geometry.coordinates              (older API export, geometry may be null)
    #   3. locations[0].coordinates              (archive format 2022+)
    #   4. top-level longitude / latitude        (archive format 2022+, alongside locations)
    lon = lat = None
    if meta:
        # format 1 & 2: currentLocation or loc → geometry → coordinates
        coords = meta.get("currentLocation") or meta.get("loc") or {}
        if isinstance(coords, dict):
            geom = coords.get("geometry")          # may be None/null
            if isinstance(geom, dict):
                c = geom.get("coordinates", [])
                if len(c) >= 2:
                    lon, lat = float(c[0]), float(c[1])
        if lon is None and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])

        # format 3: locations array  e.g. [{"coordinates": [lon, lat], "type": "Point"}]
        if lon is None:
            locations = meta.get("locations")
            if isinstance(locations, list) and locations:
                c = locations[0].get("coordinates", [])
                if len(c) >= 2:
                    lon, lat = float(c[0]), float(c[1])

        # format 4: top-level longitude / latitude keys
        if lon is None and meta.get("longitude") is not None:
            try:
                lon = float(meta["longitude"])
                lat = float(meta["latitude"])
            except (TypeError, ValueError):
                lon = lat = None

    # country / region via spatial lookup
    country = region = None
    if lon is not None and lat is not None and geo_lookup:
        country, region = geo_lookup.lookup(lon, lat)

    # station name, exposure, model & per-sensor metadata
    name        = meta.get("name") if meta else None
    exposure    = meta.get("exposure") if meta else None
    model       = meta.get("model") if meta else None
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

    # When folder-level size wasn't available from the parent listing,
    # fall back to summing the individual CSV file sizes.
    if folder_size_mb is None:
        individual_sizes = [sz for _, _, sz in csv_files if sz is not None]
        if individual_sizes:
            folder_size_mb = round(sum(individual_sizes), 6)

    return {
        "box_id":         box_id,
        "name":           name,
        "lon":            lon,
        "lat":            lat,
        "country":        country,
        "region":         region,
        "exposure":       exposure,
        "model":          model,
        "folder_url":     folder_url,
        "folder_size_mb": folder_size_mb,
        "sensor_meta":    sensor_meta,
        "csv_files":      csv_files,  # [(sensor_id, csv_url, size_mb), …]
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Index one full day
# ══════════════════════════════════════════════════════════════════════════════

def index_day(
    d: date,
    geo_lookup: GeoLookup,
    conn,
    dry_run: bool,
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
    tasks        = [(box_id, folder_url, d, geo_lookup, size_mb)
                    for box_id, folder_url, size_mb in stations_list]
    results      = []
    fetch_errors = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(process_station, t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                if res:
                    results.append(res)
                else:
                    fetch_errors += 1
            except Exception as exc:
                log.debug("Worker error: %s", exc)
                fetch_errors += 1

    total_sensors = sum(len(r["csv_files"]) for r in results)
    log.info("  Fetched %d/%d stations  %d sensors  %d fetch-errors",
             len(results), total_folders, total_sensors, fetch_errors)

    # ── dry run ───────────────────────────────────────────────────────────
    if dry_run:
        log.info("  [DRY RUN] Would insert/update %d stations, %d sensors "
                 "(sensor_dates rows: 1 per sensor per date, data_available 0/1)",
                 len(results), total_sensors)
        for r in results[:3]:
            log.info("    station sample: box_id=%-26s name=%s  country=%s  sensors=%d  size_mb=%s",
                     r["box_id"], r["name"] or "-", r["country"] or "?",
                     len(r["csv_files"]),
                     f"{r['folder_size_mb']:.3f}" if r["folder_size_mb"] is not None else "?")
        if len(results) > 3:
            log.info("    … and %d more", len(results) - 3)
        return {
            "status":           "dry",
            "stations_written": len(results),
            "sensors_written":  total_sensors,
            "errors":           fetch_errors,
        }

    # ── DB writes ─────────────────────────────────────────────────────────
    stations_written = sensors_written = db_errors = 0

    with conn.cursor() as cur:
        for r in results:
            try:
                # upsert_station now returns st_id (TEXT) directly
                st_id = upsert_station(
                    cur,
                    r["box_id"], r["name"],
                    r["lon"], r["lat"],
                    r["country"], r["region"],
                    r["exposure"], r["model"],
                    first_seen=d, last_seen=d,
                )
                upsert_station_date(cur, st_id, d, r["folder_url"],
                                    r.get("folder_size_mb"))

                # Build a set of sensor_ids that have a CSV for quick lookup
                csv_sensor_ids = {sid for sid, _, _ in r["csv_files"]}

                # Upsert sensors that have a CSV file → data_available = 1
                for sensor_id, csv_url, csv_size_mb in r["csv_files"]:
                    sm       = r["sensor_meta"].get(sensor_id, {})
                    title    = sm.get("title")
                    stype    = sm.get("type")
                    unit     = sm.get("unit")
                    category = categorize_sensor(title or "", unit or "")

                    # upsert_sensor now returns se_id (TEXT) directly
                    se_id = upsert_sensor(
                        cur, sensor_id, st_id,
                        title, stype, category, unit,
                    )
                    upsert_sensor_file(cur, se_id, st_id, d, csv_url, csv_size_mb)
                    upsert_sensor_date(cur, se_id, d, 1)

                    # compute and store daily aggregates from the CSV
                    try:
                        agg = compute_readings(csv_url)
                        if agg:
                            upsert_reading(
                                cur, se_id, st_id, d,
                                agg["recorded_at"],
                                agg["min_value"], agg["max_value"],
                                agg["avg_value"], agg["count"],
                            )
                            log.debug("  reading stored: sensor=%s count=%d avg=%.4f",
                                      sensor_id, agg["count"], agg["avg_value"])
                        else:
                            log.debug("  no reading computed for sensor=%s url=%s",
                                      sensor_id, csv_url)
                    except Exception as exc:
                        log.warning("  reading failed for sensor=%s: %s", sensor_id, exc)

                    sensors_written += 1

                # Upsert sensors known from metadata but missing a CSV → data_available = 0
                for sensor_id, sm in r["sensor_meta"].items():
                    if sensor_id in csv_sensor_ids:
                        continue  # already handled above
                    title    = sm.get("title")
                    stype    = sm.get("type")
                    unit     = sm.get("unit")
                    category = categorize_sensor(title or "", unit or "")
                    se_id = upsert_sensor(
                        cur, sensor_id, st_id,
                        title, stype, category, unit,
                    )
                    upsert_sensor_date(cur, se_id, d, 0)

                stations_written += 1

            except Exception as exc:
                log.warning("DB error for box %s: %s", r["box_id"], exc)
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
        if status in ("success", "partial"):
            write_resume_file(log_file, d)

    log.info("  Written: %d stations  %d sensors  %d errors → %s",
             stations_written, sensors_written, errors, status)
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
    console = Console()

    # ── startup banner (console only, not log) ────────────────────────────
    console.rule("[bold cyan]OpenSenseMap Indexer[/]")
    console.print(f"  [dim]Started  :[/] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    console.print(f"  [dim]Log file :[/] {Path(args.log).resolve()}")
    if args.dry:
        console.print("  [bold yellow]Mode     : DRY RUN — no DB writes[/]")
    console.rule(style="dim")

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
        or os.getenv("GEOJSON_PATH", "../database/data/admin_boundary.geojson")
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
            log.info("DB connection OK")
        except Exception as exc:
            log.error("Cannot connect to database: %s", exc)
            sys.exit(1)

        # ── auto-resume ───────────────────────────────────────────────────
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

    days_remaining   = len(days)
    days_done_before = full_range_days - days_remaining
    log.info("Days to process: %d  (skipped/done: %d)", days_remaining, days_done_before)

    # ── rich progress display ─────────────────────────────────────────────
    summary = {
        "success": 0, "partial": 0, "failed": 0,
        "dry": 0, "skipped": 0,
        "total_stations": 0, "total_sensors": 0, "total_errors": 0,
    }
    current_day    = ""
    current_status = ""

    def make_display(progress: Progress) -> Table:
        """Build the live display: progress bar + stats table side by side."""
        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold cyan",  no_wrap=True)
        stats.add_column(style="white",      no_wrap=True)
        stats.add_column(style="bold cyan",  no_wrap=True)
        stats.add_column(style="white",      no_wrap=True)

        done = (summary["success"] + summary["partial"] + summary["failed"]
                + summary["dry"] + summary["skipped"])
        left = full_range_days - days_done_before - done

        status_colour = {"success": "green", "partial": "yellow",
                         "failed": "red",    "dry": "blue", "": "white"}.get(current_status, "white")

        stats.add_row(
            "Date",      current_day or "—",
            "Status",    Text(current_status or "—", style=status_colour),
        )
        stats.add_row(
            "Days left", str(left),
            "Errors",    str(summary["total_errors"]),
        )
        stats.add_row(
            "Stations",  f"{summary['total_stations']:,}",
            "Sensors",   f"{summary['total_sensors']:,}",
        )
        stats.add_row(
            "✓ success", str(summary["success"]),
            "~ partial", str(summary["partial"]),
        )
        stats.add_row(
            "✗ failed",  str(summary["failed"]),
            "⊘ skipped", str(summary["skipped"]),
        )

        grid = Table.grid(padding=(0, 1))
        grid.add_column()
        grid.add_row(progress)
        grid.add_row(Panel(stats, title="Stats", border_style="dim", box=rich_box.SIMPLE))
        return grid

    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("days"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        expand=False,
    )
    task_id = progress.add_task("Indexing", total=full_range_days, completed=days_done_before)

    with Live(make_display(progress), refresh_per_second=4, transient=False) as live:
        for d in days:
            current_day = d.isoformat()
            live.update(make_display(progress))

            if args.skip_done and not args.dry and conn:
                with conn.cursor() as cur:
                    if day_already_done(cur, d):
                        log.info("Skipping %s (already success)", d.isoformat())
                        summary["skipped"] += 1
                        current_status = "skipped"
                        progress.advance(task_id)
                        live.update(make_display(progress))
                        continue

            result = index_day(
                d, geo, conn,
                dry_run=args.dry,
                log_file=args.log,
            )
            current_status = result["status"]
            summary[current_status]    = summary.get(current_status, 0) + 1
            summary["total_stations"] += result["stations_written"]
            summary["total_sensors"]  += result["sensors_written"]
            summary["total_errors"]   += result["errors"]
            progress.advance(task_id)
            live.update(make_display(progress))

    # ── final summary ─────────────────────────────────────────────────────
    console.rule("[bold cyan]Complete[/]")
    t = Table.grid(padding=(0, 3))
    t.add_column(style="dim")
    t.add_column(style="bold")
    t.add_column(style="dim")
    t.add_column(style="bold")
    t.add_row("✓ success",  str(summary["success"]),  "Stations", f"{summary['total_stations']:,}")
    t.add_row("~ partial",  str(summary["partial"]),  "Sensors",  f"{summary['total_sensors']:,}")
    t.add_row("✗ failed",   str(summary["failed"]),   "Errors",   str(summary["total_errors"]))
    t.add_row("⊘ skipped",  str(summary["skipped"]),  "", "")
    console.print(t)
    console.rule(style="dim")

    log.info("Indexing complete — success=%d partial=%d failed=%d skipped=%d "
             "stations=%d sensors=%d errors=%d",
             summary["success"], summary["partial"], summary["failed"],
             summary["skipped"], summary["total_stations"],
             summary["total_sensors"], summary["total_errors"])

    if conn:
        conn.close()

    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
