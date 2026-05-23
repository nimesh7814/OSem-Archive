"""
indexer.py
==========
OpenSenseMap Archive Indexer
-----------------------------
Crawls https://archive.opensensemap.org/ day by day (or reads a local
copy of the archive) and populates the osem_index_db database with:

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
  --require-location
                    skip any station that has no coordinate information.
                    The check is two-layered:
                      1. If the current day's metadata contains no lon/lat
                         the station is skipped immediately (no DB touch).
                      2. If lon/lat IS present but the station row already
                         exists in the DB with location IS NULL (i.e. it
                         was first inserted on a previous run without
                         coordinates), the station is also skipped and
                         a warning is logged.
                    Both layers are enforced so that historical records
                    without location data are never mixed into a
                    location-filtered run.
  --log FILE        write full log to FILE (default: indexer.log)
                    A companion FILE.resume tracks the last successful date
                    for automatic crash recovery.
  --logN            shorthand for --log indexerN.log (e.g. --log2, --log3,
                    --log99). Writes indexerN.log and indexerN.resume.
  -v / --verbose    DEBUG level logging
  --location PATH   path to a local downloaded copy of the archive
                    (e.g. G:\\OSeM\\archive_data).  When supplied the
                    indexer reads files from disk instead of fetching them
                    over HTTP.  The directory must mirror the archive
                    layout:  <location>/<YYYY-MM-DD>/<boxid-…>/<files>
                    If omitted the live archive URL is used.

Usage
-----
    # Online (default) — date range
    python indexer.py --from 2024-01-01 --to 2024-01-31

    # Only index stations that have location data (skip any without coordinates)
    python indexer.py --from 2023-01-01 --to 2024-12-31 --require-location

    # Local archive copy
    python indexer.py --from 2024-01-01 --to 2024-01-31 \\
                      --location G:\\OSeM\\archive_data

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
                       default: data/admin_boundary.geojson
    ARCHIVE_BASE_URL   default: https://archive.opensensemap.org
    CONCURRENCY        HTTP workers per day, default: 8
    ARCHIVE_LOCATION   local archive path (overridden by --location flag)
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
DEFAULT_LOG_FILE  = "indexer.log"

# ─── module-level logger — handlers added in setup_logging() ─────────────────
log = logging.getLogger("indexer")

# ─── module-level local-archive path (set by CLI, overrides HTTP) ─────────────
# Populated in main() from --location / ARCHIVE_LOCATION env var.
_LOCAL_ARCHIVE: Optional[Path] = None


# ══════════════════════════════════════════════════════════════════════════════
# 0-a.  Local-archive I/O helpers
# ══════════════════════════════════════════════════════════════════════════════

def _local_read_text(path: Path) -> Optional[str]:
    """Read a local file as text, returning None if it doesn't exist."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log.debug("local: file not found: %s", path)
        return None
    except Exception as exc:
        log.debug("local: read error %s: %s", path, exc)
        return None


def _local_read_json(path: Path) -> Optional[dict]:
    text = _local_read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception as exc:
        log.debug("local: JSON parse error %s: %s", path, exc)
        return None


def _local_size_mb(path: Path) -> Optional[float]:
    """Return a file's size in MB, or None if unavailable."""
    try:
        return round(path.stat().st_size / (1024 * 1024), 6)
    except Exception:
        return None


def _local_dir_size_mb(directory: Path) -> Optional[float]:
    """Sum the sizes of all immediate children of *directory* in MB."""
    try:
        total = sum(
            f.stat().st_size
            for f in directory.iterdir()
            if f.is_file()
        )
        return round(total / (1024 * 1024), 6)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 0-b.  Resume-file helpers
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Logging setup
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
# 2.  Database helpers
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
    Fetch (or read locally) a sensor CSV and compute daily aggregates.

    CSV format (comma-separated, with header):
        createdAt,value

    When _LOCAL_ARCHIVE is set the function maps csv_url back to a local
    file path instead of making an HTTP request.

    Returns:
        {recorded_at, min_value, max_value, avg_value, count}
        or None if the file is empty / unparseable.
    """
    import io
    import csv as csv_mod

    text = http_get_text(csv_url)   # transparently local or remote
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
        raw_ts = row.get("createdAt") or row.get("created_at") or ""
        if first_ts is None and raw_ts:
            try:
                first_ts = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")
                )
            except Exception as e:
                log.debug("compute_readings: ts parse error '%s': %s", raw_ts, e)

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


def station_has_location_in_db(cur, st_id: str) -> bool:
    """
    Return True if the station row already exists in the DB AND has a
    non-NULL location.  Used by --require-location to catch stations that
    were previously inserted without coordinates: even if the current day's
    metadata now carries a lon/lat we still honour the filter because the
    historical record is location-less.

    Returns False when the station is not yet in the DB (new station) — the
    caller will then rely on the metadata lon/lat check instead.
    """
    cur.execute(
        "SELECT location IS NOT NULL FROM stations WHERE st_id = %s",
        (st_id,),
    )
    row = cur.fetchone()
    if row is None:
        # Station not yet in DB — not a previously-inserted location-less record
        return False
    return bool(row[0])


# ══════════════════════════════════════════════════════════════════════════════
# 3.  GeoJSON spatial index  (country / region lookup)
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
# 4.  HTTP / local-file helpers
#
#     All archive reads go through http_get_text / http_get_json.
#     When _LOCAL_ARCHIVE is set those functions resolve the URL to a
#     local path and read from disk instead of making HTTP requests.
# ══════════════════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({"User-Agent": "osem-indexer/2.0 (study-project)"})


def _url_to_local_path(url: str) -> Optional[Path]:
    """
    Map an archive URL (or a local file:// / plain path string stored as the
    "url") back to a Path under _LOCAL_ARCHIVE.

    The archive URL structure is:
        https://archive.opensensemap.org/<date>/<folder>/<file>

    The local mirror is expected to follow the same relative layout:
        <_LOCAL_ARCHIVE>/<date>/<folder>/<file>

    The function strips the ARCHIVE_BASE prefix (or any http/https host) and
    joins the remainder onto _LOCAL_ARCHIVE.  It also handles the case where
    the url was already stored as a local path string (starts with a drive
    letter or '/').
    """
    if _LOCAL_ARCHIVE is None:
        return None

    # Already a local path (e.g. stored folder_url from a previous local run)
    p = Path(url)
    if p.is_absolute():
        return p

    # Strip the base URL prefix to get the relative portion
    relative = url
    if relative.startswith(ARCHIVE_BASE):
        relative = relative[len(ARCHIVE_BASE):]
    else:
        # Strip scheme + host generically
        m = re.match(r"https?://[^/]+(/.*)$", relative)
        if m:
            relative = m.group(1)

    # Remove leading slash
    relative = relative.lstrip("/")

    # Remove trailing slash for directory probing — we will check both
    is_dir_url = url.endswith("/")
    local = _LOCAL_ARCHIVE / relative.rstrip("/")
    return local


def _local_get(url: str, as_json: bool):
    """
    Serve a request from the local archive directory.

    For a *directory* URL (ends with '/') we synthesise a minimal HTML
    listing from the real directory entries so that the existing regex
    parsers in list_date_folders / csv_urls_from_folder work unchanged.

    For a *file* URL we read the file directly.
    """
    local = _url_to_local_path(url)
    if local is None:
        return None

    is_dir_url = url.endswith("/")

    if is_dir_url:
        # Synthesise an HTML directory listing identical in structure to
        # what the live archive returns so that all regex parsers work.
        if not local.is_dir():
            log.debug("local: directory not found: %s", local)
            return None
        lines = [f'<html><body>']
        for child in sorted(local.iterdir()):
            if child.is_dir():
                lines.append(f'<a href="{child.name}/">{child.name}/</a>')
            else:
                lines.append(f'<a href="{child.name}">{child.name}</a>')
        lines.append('</body></html>')
        html = "\n".join(lines)
        if as_json:
            log.debug("local: directory requested as JSON — returning None")
            return None
        return html
    else:
        # Plain file
        if as_json:
            return _local_read_json(local)
        return _local_read_text(local)


def _remote_get(url: str, as_json: bool):
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
    if _LOCAL_ARCHIVE is not None:
        return _local_get(url, as_json=True)
    return _remote_get(url, as_json=True)  # type: ignore[return-value]


def http_get_text(url: str) -> Optional[str]:
    if _LOCAL_ARCHIVE is not None:
        return _local_get(url, as_json=False)
    return _remote_get(url, as_json=False)  # type: ignore[return-value]


def http_head_size_mb(url: str) -> Optional[float]:
    """
    Return file size in MB.

    For local files the size is read from the filesystem stat.
    For remote URLs a HEAD request is issued (with GET fallback).
    """
    if _LOCAL_ARCHIVE is not None:
        local = _url_to_local_path(url)
        if local is None:
            return None
        if local.is_dir():
            return _local_dir_size_mb(local)
        return _local_size_mb(local)

    # ── remote ────────────────────────────────────────────────────────────
    delay = HTTP_BACKOFF
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = _session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            if r.status_code == 405:
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
# 5.  Archive parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_size_mb(size_str: str) -> Optional[float]:
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


def _make_folder_url(d: date, folder_name: str) -> str:
    """
    Build the canonical folder URL (or local path string) for a station folder.

    When running in local mode the "URL" is a file:// style string built from
    the local path so that _url_to_local_path can reconstruct the path later.
    We still use the same relative structure as the live archive so that the
    folder_url stored in the DB is human-readable and consistent.
    """
    if _LOCAL_ARCHIVE is not None:
        # Store as a plain local path string (str(Path)) — cross-platform
        local_folder = _LOCAL_ARCHIVE / d.isoformat() / folder_name
        return str(local_folder) + os.sep
    return f"{ARCHIVE_BASE}/{d.isoformat()}/{folder_name}/"


def list_date_folders(d: date) -> list[tuple[str, str, Optional[float]]]:
    """
    List all station folders for a date.
    Returns [(box_id, folder_url, size_mb), …].

    Local mode: scans the filesystem directly instead of parsing HTML.
    Remote mode: fetches & parses the archive HTML listing.
    """
    if _LOCAL_ARCHIVE is not None:
        return _list_date_folders_local(d)
    return _list_date_folders_remote(d)


def _list_date_folders_local(d: date) -> list[tuple[str, str, Optional[float]]]:
    """Scan <_LOCAL_ARCHIVE>/<date>/ for station sub-directories."""
    day_dir = _LOCAL_ARCHIVE / d.isoformat()  # type: ignore[operator]
    if not day_dir.is_dir():
        log.debug("local: day directory not found: %s", day_dir)
        return []

    # Match the same pattern as the remote parser: 24-char hex id + optional name
    pattern = re.compile(r"^([0-9a-f]{24})(?:-.*)?$", re.IGNORECASE)
    results = []
    for child in sorted(day_dir.iterdir()):
        if not child.is_dir():
            continue
        m = pattern.match(child.name)
        if not m:
            continue
        box_id     = m.group(1)
        folder_url = str(child) + os.sep       # local path with trailing sep
        size_mb    = _local_dir_size_mb(child)
        results.append((box_id, folder_url, size_mb))

    log.debug("local: found %d station folders in %s", len(results), day_dir)
    return results


def _list_date_folders_remote(d: date) -> list[tuple[str, str, Optional[float]]]:
    url  = f"{ARCHIVE_BASE}/{d.isoformat()}/"
    text = http_get_text(url)
    if not text:
        return []

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
    """
    Fetch the station JSON metadata file from its folder.

    Local mode: scans the folder directory for a .json file.
    Remote mode: fetches the folder HTML then the JSON URL found inside.
    """
    if _LOCAL_ARCHIVE is not None:
        return _fetch_station_meta_local(folder_url)
    return _fetch_station_meta_remote(folder_url)


def _fetch_station_meta_local(folder_url: str) -> Optional[dict]:
    local = _url_to_local_path(folder_url)
    if local is None or not local.is_dir():
        log.debug("local: station folder not found: %s", folder_url)
        return None
    for f in local.iterdir():
        if f.suffix.lower() == ".json":
            return _local_read_json(f)
    log.debug("local: no JSON metadata in %s", local)
    return None


def _fetch_station_meta_remote(folder_url: str) -> Optional[dict]:
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
    """
    Return [(sensor_id, csv_url, size_mb), …] for all CSVs in a station folder.

    Local mode: scans the filesystem for matching CSV files.
    Remote mode: fetches the folder HTML and parses CSV hrefs.
    """
    if _LOCAL_ARCHIVE is not None:
        return _csv_urls_from_folder_local(folder_url, d)
    return _csv_urls_from_folder_remote(folder_url, d)


def _csv_urls_from_folder_local(folder_url: str, d: date) -> list[tuple[str, str, Optional[float]]]:
    local = _url_to_local_path(folder_url)
    if local is None or not local.is_dir():
        log.debug("local: station folder not found for CSV scan: %s", folder_url)
        return []

    date_str = d.isoformat()
    pattern  = re.compile(
        r"^([0-9a-f]{24})-" + re.escape(date_str) + r"\.csv$",
        re.IGNORECASE,
    )
    results = []
    for f in sorted(local.iterdir()):
        if not f.is_file():
            continue
        m = pattern.match(f.name)
        if not m:
            continue
        sensor_id = m.group(1)
        csv_url   = str(f)           # store local path as "url"
        size_mb   = _local_size_mb(f)
        results.append((sensor_id, csv_url, size_mb))

    log.debug("local: found %d CSV files in %s", len(results), local)
    return results


def _csv_urls_from_folder_remote(folder_url: str, d: date) -> list[tuple[str, str, Optional[float]]]:
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
# 6.  Process one station  (runs in thread pool)
# ══════════════════════════════════════════════════════════════════════════════

def process_station(args: tuple) -> Optional[dict]:
    box_id, folder_url, d, geo_lookup, folder_size_mb = args

    meta = fetch_station_meta(folder_url)

    lon = lat = None
    if meta:
        coords = meta.get("currentLocation") or meta.get("loc") or {}
        if isinstance(coords, dict):
            geom = coords.get("geometry")
            if isinstance(geom, dict):
                c = geom.get("coordinates", [])
                if len(c) >= 2:
                    lon, lat = float(c[0]), float(c[1])
        if lon is None and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])

        if lon is None:
            locations = meta.get("locations")
            if isinstance(locations, list) and locations:
                c = locations[0].get("coordinates", [])
                if len(c) >= 2:
                    lon, lat = float(c[0]), float(c[1])

        if lon is None and meta.get("longitude") is not None:
            try:
                lon = float(meta["longitude"])
                lat = float(meta["latitude"])
            except (TypeError, ValueError):
                lon = lat = None

    country = region = None
    if lon is not None and lat is not None and geo_lookup:
        country, region = geo_lookup.lookup(lon, lat)

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
        "csv_files":      csv_files,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7.  Index one full day
# ══════════════════════════════════════════════════════════════════════════════

def index_day(
    d: date,
    geo_lookup: GeoLookup,
    conn,
    dry_run: bool,
    log_file: str = DEFAULT_LOG_FILE,
    require_location: bool = False,
) -> dict:
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

    if dry_run:
        log.info("  [DRY RUN] Would insert/update %d stations, %d sensors "
                 "(sensor_dates rows: 1 per sensor per date, data_available 0/1)%s",
                 len(results), total_sensors,
                 "  [location filter active]" if require_location else "")
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

    stations_written = sensors_written = db_errors = 0

    DEADLOCK_RETRIES = 5
    DEADLOCK_BACKOFF = 1.5

    def _write_station(cur, r: dict) -> None:
        nonlocal sensors_written

        # ── location guard (--require-location) ──────────────────────────
        # Decision matrix (metadata lon/lat  ×  existing DB location):
        #
        #   metadata   |  DB row       |  action
        #   -----------+---------------+----------------------------------
        #   has coords |  no row yet   |  ALLOW  (new station with location)
        #   has coords |  has location |  ALLOW  (known station, still good)
        #   has coords |  NULL location|  SKIP   (previously inserted w/o loc)
        #   no coords  |  no row yet   |  SKIP   (brand-new, no location at all)
        #   no coords  |  has location |  ALLOW  (DB has it; today's file just missing)
        #   no coords  |  NULL location|  SKIP   (no location anywhere)
        #
        if require_location:
            has_meta_loc = r["lon"] is not None and r["lat"] is not None
            cur.execute(
                "SELECT location IS NOT NULL FROM stations WHERE st_id = %s",
                (r["box_id"],),
            )
            existing = cur.fetchone()
            # existing is None     → station not yet in DB
            # existing is (True,)  → station in DB with a location
            # existing is (False,) → station in DB without a location

            db_has_loc = existing is not None and bool(existing[0])

            if not has_meta_loc and not db_has_loc:
                log.debug(
                    "  --require-location: skipping box %s — no coordinates in "
                    "metadata and no location in DB",
                    r["box_id"],
                )
                return

            if has_meta_loc and existing is not None and not existing[0]:
                log.warning(
                    "  --require-location: skipping box %s — metadata now has "
                    "coordinates but existing DB record has no location (previously "
                    "inserted without coords)",
                    r["box_id"],
                )
                return

            # Remaining allowed cases:
            #   has_meta_loc + no DB row  → new station with location
            #   has_meta_loc + db_has_loc → known station, still has location
            #   no meta loc  + db_has_loc → DB already holds the location; proceed

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

        csv_sensor_ids = {sid for sid, _, _ in r["csv_files"]}

        for sensor_id, csv_url, csv_size_mb in r["csv_files"]:
            sm       = r["sensor_meta"].get(sensor_id, {})
            title    = sm.get("title")
            stype    = sm.get("type")
            unit     = sm.get("unit")
            category = categorize_sensor(title or "", unit or "")
            se_id = upsert_sensor(cur, sensor_id, st_id, title, stype, category, unit)
            upsert_sensor_file(cur, se_id, st_id, d, csv_url, csv_size_mb)
            upsert_sensor_date(cur, se_id, d, 1)

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

        for sensor_id, sm in r["sensor_meta"].items():
            if sensor_id in csv_sensor_ids:
                continue
            title    = sm.get("title")
            stype    = sm.get("type")
            unit     = sm.get("unit")
            category = categorize_sensor(title or "", unit or "")
            se_id = upsert_sensor(cur, sensor_id, st_id, title, stype, category, unit)
            upsert_sensor_date(cur, se_id, d, 0)

    with conn.cursor() as cur:
        for r in results:
            attempt = 0
            backoff = DEADLOCK_BACKOFF
            while True:
                try:
                    _write_station(cur, r)
                    stations_written += 1
                    break
                except psycopg2.errors.DeadlockDetected as exc:
                    conn.rollback()
                    attempt += 1
                    if attempt >= DEADLOCK_RETRIES:
                        log.warning("DB deadlock for box %s (gave up after %d retries): %s",
                                    r["box_id"], attempt, exc)
                        db_errors += 1
                        break
                    log.warning("DB deadlock for box %s (retry %d/%d in %.1fs)",
                                r["box_id"], attempt, DEADLOCK_RETRIES, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                except Exception as exc:
                    log.warning("DB error for box %s: %s", r["box_id"], exc)
                    conn.rollback()
                    db_errors += 1
                    break

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
# 8.  CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OpenSenseMap archive indexer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Remote archive (default)
  python indexer.py --from 2024-01-01 --to 2024-01-31
  python indexer.py --start 2024-01-01 --end 2024-01-31   # --start/--end are aliases

  # Only index stations that have location data
  python indexer.py --from 2023-01-01 --to 2024-12-31 --require-location

  # Local downloaded archive
  python indexer.py --from 2024-01-01 --to 2024-01-31 --location G:\\OSeM\\archive_data
  python indexer.py --date 2024-06-15  --location /mnt/archive

  # Re-run resumes automatically from last success
  python indexer.py --start 2024-01-01 --end 2024-01-31

  # Dry run — verify parsing without touching the DB
  python indexer.py --from 2024-01-01 --to 2024-01-03 --dry

  # Last 7 days, skip already-done dates
  python indexer.py --last 7 --skip-done

  # Single date with verbose output
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

    p.add_argument(
        "--location", metavar="PATH",
        default=None,
        help=(
            "Path to a local downloaded copy of the archive "
            "(e.g. G:\\\\OSeM\\\\archive_data or /mnt/archive). "
            "The directory must mirror the archive layout: "
            "<PATH>/<YYYY-MM-DD>/<boxid-…>/<files>. "
            "When omitted the live archive URL is used. "
            "Can also be set via the ARCHIVE_LOCATION environment variable."
        ),
    )

    p.add_argument("--skip-done", action="store_true",
                   help="Skip individual dates already marked 'success'")
    p.add_argument(
        "--require-location", action="store_true", dest="require_location",
        help=(
            "Skip stations that have no coordinate information. "
            "Two checks are applied: (1) stations whose current-day metadata "
            "contains no lon/lat are dropped before any DB access; "
            "(2) stations that already exist in the DB with location IS NULL "
            "(inserted on a previous run without coordinates) are also skipped, "
            "even if today's metadata now carries coordinates. "
            "Use this flag to keep location-filtered runs strictly clean."
        ),
    )
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
    global _LOCAL_ARCHIVE

    # Detect --logN (e.g. --log2, --log3, --log99) before normal arg parsing.
    import re as _re
    for _arg in sys.argv[1:]:
        _m = _re.fullmatch(r"--log(\d+)", _arg)
        if _m:
            sys.argv.remove(_arg)
            _n = _m.group(1)
            sys.argv.extend(["--log", f"indexer{_n}.log"])
            break

    args = parse_args()
    setup_logging(args.log, args.verbose)
    console = Console()

    # ── resolve local archive location ────────────────────────────────────
    location_str = args.location or os.getenv("ARCHIVE_LOCATION")
    if location_str:
        _LOCAL_ARCHIVE = Path(location_str)
        if not _LOCAL_ARCHIVE.is_dir():
            log.error(
                "Local archive path does not exist or is not a directory: %s",
                _LOCAL_ARCHIVE,
            )
            sys.exit(1)
        log.info("Local archive mode: reading from %s", _LOCAL_ARCHIVE.resolve())

    # ── startup banner ────────────────────────────────────────────────────
    console.rule("[bold cyan]OpenSenseMap Indexer[/]")
    console.print(f"  [dim]Started  :[/] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    console.print(f"  [dim]Log file :[/] {Path(args.log).resolve()}")
    if _LOCAL_ARCHIVE is not None:
        console.print(f"  [dim]Source   :[/] [green]LOCAL[/] {_LOCAL_ARCHIVE.resolve()}")
    else:
        console.print(f"  [dim]Source   :[/] [blue]REMOTE[/] {ARCHIVE_BASE}")
    if args.require_location:
        console.print("  [bold yellow]Filter   : --require-location active — stations without coordinates will be skipped[/]")
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
        or os.getenv("GEOJSON_PATH", "data/admin_boundary.geojson")
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

        source_label = (
            f"LOCAL {_LOCAL_ARCHIVE}" if _LOCAL_ARCHIVE is not None else f"REMOTE {ARCHIVE_BASE}"
        )

        stats.add_row(
            "Date",      current_day or "—",
            "Status",    Text(current_status or "—", style=status_colour),
        )
        stats.add_row(
            "Source",    source_label,
            "Errors",    str(summary["total_errors"]),
        )
        stats.add_row(
            "Days left", str(left),
            "Stations",  f"{summary['total_stations']:,}",
        )
        stats.add_row(
            "Sensors",   f"{summary['total_sensors']:,}",
            "", "",
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
                require_location=args.require_location,
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