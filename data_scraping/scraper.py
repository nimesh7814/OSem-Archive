"""
OpenSenseMap Archive → TimescaleDB scraper
==========================================
Target schema:  stations / sensors / readings  (hypertable on recorded_at)

Resume safety
-------------
- A progress.json file tracks the last fully-committed date.
- _scraper_processed_dates (created by schema.sql) records every date that has
  been 100 % committed, so re-running after a crash will skip already-done dates
  at the DB level — no reliance on the progress file alone.
- All reads for one station are staged in a TEMP table and merged in a single
  INSERT … WHERE NOT EXISTS, so a mid-station crash leaves zero partial data.
- SIGTERM / SIGINT are caught; the scraper finishes the current station, commits,
  then exits cleanly.
"""

import os
import json
import requests
import csv
import io
import re
import time
import random
import signal
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
from tqdm import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL        = "https://archive.opensensemap.org/"
PROGRESS_FILE   = os.getenv("PROGRESS_FILE", "/data/progress.json")
REQUEST_TIMEOUT = (10, 120)

DELAY_MIN        = float(os.getenv("DELAY_MIN",        "0.1"))
DELAY_MAX        = float(os.getenv("DELAY_MAX",        "0.4"))
DELAY_PER_DATE   = float(os.getenv("DELAY_PER_DATE",   "0.5"))
DELAY_429_BASE   = float(os.getenv("DELAY_429_BASE",   "60.0"))
DELAY_SERVER_ERR = float(os.getenv("DELAY_SERVER_ERR", "10.0"))
MAX_429_RETRIES  = int(os.getenv(  "MAX_429_RETRIES",  "5"))

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "opensensemap"),
    "user":     os.getenv("DB_USER",     "osm"),
    "password": os.getenv("DB_PASSWORD", "changeme"),
}

# ─── HTTP session ──────────────────────────────────────────────────────────────

def create_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "OpenSenseMap-Archiver/2.0 (research data archiver; polite-bot)"
    })
    retries = Retry(
        total=5, connect=5, read=5,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


HTTP_SESSION   = create_http_session()
STOP_REQUESTED = False

PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{24}$")
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")


def _request_stop(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    tqdm.write("Stop signal received — finishing current station then exiting safely.")


_last_request_time: float = 0.0


def _polite_wait() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait    = random.uniform(DELAY_MIN, DELAY_MAX)
    if elapsed < wait:
        time.sleep(wait - elapsed)
    _last_request_time = time.monotonic()


def safe_get(url: str, *, allow_404: bool = False,
             return_text: bool = False,
             progress_desc: str | None = None):
    for attempt_429 in range(MAX_429_RETRIES + 1):
        _polite_wait()
        try:
            if return_text:
                response = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=True)
            else:
                response = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                if attempt_429 >= MAX_429_RETRIES:
                    return None, 429
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else DELAY_429_BASE * (2 ** attempt_429)
                tqdm.write(f"  429 rate-limited. Waiting {wait:.0f}s "
                           f"(attempt {attempt_429+1}/{MAX_429_RETRIES})…")
                time.sleep(wait)
                continue

            if response.status_code == 404 and allow_404:
                return None, 404

            response.raise_for_status()

            if return_text:
                total_bytes = response.headers.get("Content-Length")
                total_bytes = int(total_bytes) if total_bytes and total_bytes.isdigit() else None
                chunks: list[bytes] = []
                with tqdm(total=total_bytes, desc=progress_desc or "Downloading",
                          unit="B", unit_scale=True, unit_divisor=1024, leave=False) as bar:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        bar.update(len(chunk))
                response.close()
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace"), response.status_code

            return response, response.status_code

        except requests.exceptions.Timeout:
            time.sleep(DELAY_SERVER_ERR)
            return None, -1
        except requests.exceptions.ConnectionError:
            time.sleep(DELAY_SERVER_ERR)
            return None, -1
        except requests.exceptions.RequestException:
            return None, -1

    return None, -1

# ─── Progress file ─────────────────────────────────────────────────────────────

def load_progress() -> dict[str, Any]:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            corrupt = PROGRESS_FILE + ".corrupt"
            tqdm.write(f"Warning: progress file corrupted ({e}), backed up to {corrupt}")
            try:
                os.replace(PROGRESS_FILE, corrupt)
            except OSError:
                pass
    return {"last_processed_date": None}


def save_progress(data: dict[str, Any]) -> None:
    """Atomic write: write to .tmp then rename — crash-safe."""
    os.makedirs(os.path.dirname(PROGRESS_FILE) or ".", exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)

# ─── Database ──────────────────────────────────────────────────────────────────

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def wait_for_db(retries: int = 30, delay: int = 3) -> None:
    for attempt in range(1, retries + 1):
        try:
            conn = get_db_connection()
            conn.close()
            tqdm.write("Database is ready.")
            return
        except psycopg2.OperationalError as e:
            tqdm.write(f"Waiting for database… attempt {attempt}/{retries}: {e}")
            time.sleep(delay)
    raise RuntimeError("Database did not become ready in time.")


def is_date_processed(conn, date_str: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM _scraper_processed_dates WHERE date_str = %s",
            (date_str,),
        )
        return cur.fetchone() is not None


def mark_date_processed(conn, date_str: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO _scraper_processed_dates (date_str) VALUES (%s) "
            "ON CONFLICT DO NOTHING",
            (date_str,),
        )
    conn.commit()


def upsert_station(conn, station_id: str, name: str | None,
                   box_type: str | None, exposure: str | None,
                   longitude: float | None, latitude: float | None) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO stations (station_id, name, box_type, exposure, geometry)
            VALUES (
                %s, %s, %s, %s,
                CASE WHEN %s IS NOT NULL AND %s IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                     ELSE NULL
                END
            )
            ON CONFLICT (station_id) DO UPDATE SET
                name     = EXCLUDED.name,
                box_type = EXCLUDED.box_type,
                exposure = EXCLUDED.exposure,
                geometry = COALESCE(EXCLUDED.geometry, stations.geometry)
        """, (
            station_id, name, box_type, exposure,
            longitude, latitude,
            longitude, latitude,
        ))


def upsert_sensor(conn, sensor_id: str, station_id: str,
                  title: str | None, unit: str | None,
                  sensor_info: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sensors (sensor_id, station_dbid, station_id, title, unit, sensor_info)
            SELECT %s, st.station_dbid, %s, %s, %s, %s
            FROM   stations st
            WHERE  st.station_id = %s
            ON CONFLICT (sensor_id) DO UPDATE SET
                title       = EXCLUDED.title,
                unit        = EXCLUDED.unit,
                sensor_info = EXCLUDED.sensor_info
        """, (sensor_id, station_id, title, unit, sensor_info, station_id))


def insert_readings_bulk(conn, rows: list[tuple]) -> tuple[int, int]:
    """
    Stage rows into a TEMP table (deleted on commit) then merge into readings
    with a NOT EXISTS guard.  If the transaction is rolled back or the process
    crashes before commit, the temp table vanishes and readings is untouched.
    Returns (valid_count, inserted_count).

    rows items: (sensor_id [hex], recorded_at, raw_value)
    The INSERT resolves sensor_id → sensor_dbid via a JOIN so readings only
    stores the integer FK.
    """
    clean: list[tuple] = []
    for sensor_id, recorded_at, raw_value in rows:
        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        clean.append((sensor_id, recorded_at, value))

    if not clean:
        return 0, 0

    buf = io.StringIO()
    csv.writer(buf).writerows(clean)
    buf.seek(0)

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _readings_stage (
                sensor_id   TEXT,
                recorded_at TIMESTAMPTZ,
                value       DOUBLE PRECISION
            ) ON COMMIT DELETE ROWS;
        """)
        cur.execute("TRUNCATE _readings_stage;")

        cur.copy_expert(
            "COPY _readings_stage (sensor_id, recorded_at, value) "
            "FROM STDIN WITH (FORMAT csv)",
            buf,
        )

        # Resolve sensor_id → sensor_dbid in the INSERT; readings never stores
        # the hex string — only the integer surrogate FK.
        cur.execute("""
            INSERT INTO readings (recorded_at, sensor_dbid, value)
            SELECT s.recorded_at, sen.sensor_dbid, s.value
            FROM   _readings_stage s
            JOIN   sensors sen ON sen.sensor_id = s.sensor_id
            WHERE  NOT EXISTS (
                SELECT 1 FROM readings r
                WHERE  r.sensor_dbid  = sen.sensor_dbid
                  AND  r.recorded_at  = s.recorded_at
            )
        """)
        inserted = cur.rowcount

    return len(clean), inserted

# ─── Archive discovery ─────────────────────────────────────────────────────────

def get_archive_dates() -> list[str]:
    response, status_code = safe_get(BASE_URL)
    if response is None:
        raise RuntimeError(f"Could not load archive root (status={status_code})")
    hrefs = re.findall(r'href="([^"]+)"', response.text)
    dates: set[str] = set()
    for href in hrefs:
        path = href.split("?", 1)[0].split("#", 1)[0]
        path = path.removeprefix("./").removeprefix("../").rstrip("/")
        if re.match(r'^\d{4}-\d{2}-\d{2}$', path):
            dates.add(path)
    return sorted(dates)


def get_station_folders(date_html: str) -> list[str]:
    normalized: list[str] = []
    for folder in re.findall(r'href="([^"]+)"', date_html):
        folder = folder.split("?", 1)[0].split("#", 1)[0]
        folder = folder.removeprefix("./").removeprefix("../")
        if re.match(r'^[0-9a-f]{24}-.+/$', folder):
            normalized.append(folder)
    return normalized


def parse_station_folder(folder_name: str) -> tuple[str | None, str | None, str | None]:
    path  = folder_name.removeprefix("./").removeprefix("../").rstrip("/")
    match = re.match(r'^([0-9a-f]{24})-(.+)$', path)
    if not match:
        return None, None, None
    return match.group(1), match.group(2), path


def fetch_station_metadata(date: str, station_path: str) -> dict | None:
    base         = f"{BASE_URL}{date}/{station_path}/"
    station_name = station_path.split("-", 1)[1] if "-" in station_path else station_path
    candidates   = list(dict.fromkeys([
        f"{station_path}-{date}.json",
        f"{station_name}-{date}.json",
    ]))
    for filename in candidates:
        resp, code = safe_get(base + filename, allow_404=True)
        if resp is not None:
            return resp.json()
        if code not in (404, -1):
            break
    listing, _ = safe_get(base, allow_404=True)
    if listing is None:
        return None
    for href in re.findall(r'href="([^"]+)"', listing.text):
        fname = href.split("?", 1)[0].split("#", 1)[0]
        fname = fname.removeprefix("./").removeprefix("../")
        if fname.endswith(f"-{date}.json"):
            resp, _ = safe_get(base + fname, allow_404=True)
            if resp is not None:
                return resp.json()
    return None


def fetch_sensor_csv(date: str, station_path: str, sensor_id: str) -> str | None:
    url = f"{BASE_URL}{date}/{station_path}/{sensor_id}-{date}.csv"
    text, code = safe_get(url, allow_404=True, return_text=True,
                          progress_desc=f"CSV {sensor_id[:8]} {date}")
    return None if (code == 404 or text is None) else text


def is_public_id(value: str | None) -> bool:
    return bool(value and PUBLIC_ID_RE.match(value))


def normalize_rfc3339_utc(ts: str) -> str | None:
    """
    Accept OpenSenseMap RFC 3339 UTC timestamps:
      2018-02-01T23:18:02Z
      2018-02-01T23:18:02.412Z
    Returns a canonical ISO-8601 UTC string for TIMESTAMPTZ insertion.
    """
    if not RFC3339_UTC_RE.match(ts):
        return None

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    if "." in ts:
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"

    try:
        return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def parse_csv(content: str) -> list[tuple[str, str]]:
    readings: list[tuple[str, str]] = []
    for row in csv.DictReader(content.splitlines()):
        ts  = row.get("createdAt") or row.get("timestamp")
        val = row.get("value")
        if not ts or val is None:
            continue

        norm_ts = normalize_rfc3339_utc(ts)
        if norm_ts is None:
            continue

        readings.append((norm_ts, val))
    return readings

# ─── Main loop ─────────────────────────────────────────────────────────────────

def run_scraper() -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = False

    sigterm_prev = signal.getsignal(signal.SIGTERM)
    sigint_prev  = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT,  _request_stop)

    wait_for_db()

    progress      = load_progress()
    archive_dates = get_archive_dates()
    tqdm.write(f"Total archive dates available: {len(archive_dates)}")

    start_index = 0
    last_date   = progress.get("last_processed_date")
    if last_date and last_date in archive_dates:
        start_index = archive_dates.index(last_date) + 1
        tqdm.write(f"Resuming after '{last_date}' (skipping {start_index} already-done dates)")
    elif last_date:
        tqdm.write(f"Warning: saved date '{last_date}' not in archive — resetting.")
        progress = {"last_processed_date": None}

    dates_to_process = archive_dates[start_index:]
    if not dates_to_process:
        tqdm.write("All dates already processed. Nothing to do.")
        return

    def now_utc() -> str:
        return datetime.now(timezone.utc).isoformat()

    stats: dict[str, int] = {
        "dates_total":         len(dates_to_process),
        "dates_done":          0,
        "dates_skipped":       0,
        "stations_total_seen": 0,
        "stations_done":       0,
        "sensors_total":       0,
        "sensors_done":        0,
        "csv_downloaded":      0,
        "csv_missing":         0,
        "readings_parsed":     0,
        "readings_valid":      0,
        "readings_inserted":   0,
    }

    started_at = progress.get("started_at", now_utc())
    progress.update({
        "status":          "running",
        "started_at":      started_at,
        "updated_at":      now_utc(),
        "finished_at":     None,
        "current_date":    None,
        "message":         "Scraper started",
        "dates_total":     len(dates_to_process),
        "date_range_from": archive_dates[0],
        "date_range_to":   archive_dates[-1],
        "stats":           stats.copy(),
    })
    save_progress(progress)

    def persist(message: str | None = None, **fields) -> None:
        progress.update(fields)
        progress["updated_at"] = now_utc()
        if message is not None:
            progress["message"] = message
        progress["stats"] = stats.copy()
        save_progress(progress)

    conn = get_db_connection()
    try:
        stop_cleanly = False

        for date_str in tqdm(dates_to_process, desc="Dates", unit="date"):

            if STOP_REQUESTED:
                stop_cleanly = True
                progress["status"] = "stopped"
                persist(message="Stop signal — safe checkpoint saved.", current_date=date_str)
                break

            persist(message=f"Processing {date_str}", current_date=date_str)

            # Double-check the DB: handles crash-resume even if progress.json is stale
            if is_date_processed(conn, date_str):
                tqdm.write(f"Skipping {date_str}: already committed in DB.")
                progress["last_processed_date"] = date_str
                stats["dates_done"]    += 1
                stats["dates_skipped"] += 1
                persist(last_processed_date=date_str)
                continue

            time.sleep(DELAY_PER_DATE)

            date_resp, status_code = safe_get(f"{BASE_URL}{date_str}/", allow_404=True)

            if status_code == 404:
                mark_date_processed(conn, date_str)
                progress["last_processed_date"] = date_str
                stats["dates_done"]    += 1
                stats["dates_skipped"] += 1
                persist(last_processed_date=date_str)
                continue

            if status_code in (429, None) or date_resp is None:
                progress["status"] = "stopped"
                persist(message=f"Stopped on rate-limit/error at {date_str}")
                stop_cleanly = True
                break

            station_folders = get_station_folders(date_resp.text)
            stats["stations_total_seen"] += len(station_folders)
            tqdm.write(f"{date_str}: {len(station_folders)} station(s)")

            abort_date = False
            for folder in tqdm(station_folders, desc=f"  {date_str}",
                               unit="station", leave=False):

                if STOP_REQUESTED:
                    conn.commit()
                    progress["status"] = "stopped"
                    persist(message=f"Stop signal during {date_str}")
                    stop_cleanly = True
                    abort_date   = True
                    break

                station_id, _, station_path = parse_station_folder(folder)
                if not station_id:
                    continue

                metadata = fetch_station_metadata(date_str, station_path)
                if metadata is None:
                    continue

                metadata_station_id = metadata.get("id")
                if not is_public_id(metadata_station_id):
                    tqdm.write(f"Skipping station with invalid public ID: {metadata_station_id}")
                    continue

                try:
                    coords    = metadata["loc"]["geometry"]["coordinates"]
                    longitude = float(coords[0])
                    latitude  = float(coords[1])
                except (KeyError, IndexError, TypeError, ValueError):
                    longitude, latitude = None, None

                upsert_station(conn,
                               station_id = metadata_station_id,
                               name       = metadata.get("name"),
                               box_type   = metadata.get("boxType"),
                               exposure   = metadata.get("exposure"),
                               longitude  = longitude,
                               latitude   = latitude)

                sensors = metadata.get("sensors", [])
                stats["sensors_total"] += len(sensors)

                for sensor in sensors:
                    sensor_id = sensor.get("id") or sensor.get("_id")
                    if not is_public_id(sensor_id):
                        continue

                    upsert_sensor(conn,
                                  sensor_id   = sensor_id,
                                  station_id  = metadata_station_id,
                                  title       = sensor.get("title"),
                                  unit        = sensor.get("unit"),
                                  sensor_info = sensor.get("sensorType"))

                    csv_text = fetch_sensor_csv(date_str, station_path, sensor_id)
                    if csv_text is None:
                        stats["csv_missing"] += 1
                        continue

                    stats["csv_downloaded"] += 1
                    raw = parse_csv(csv_text)
                    stats["readings_parsed"] += len(raw)

                    valid, inserted = insert_readings_bulk(
                        conn, [(sensor_id, ts, val) for ts, val in raw]
                    )
                    stats["readings_valid"]    += valid
                    stats["readings_inserted"] += inserted
                    stats["sensors_done"]      += 1

                # Commit once per station — temp stage table is cleared on commit
                conn.commit()
                stats["stations_done"] += 1
                persist()

            if abort_date:
                break

            mark_date_processed(conn, date_str)
            progress["last_processed_date"] = date_str
            stats["dates_done"] += 1
            persist(message=f"Completed {date_str}", last_processed_date=date_str)
            tqdm.write(
                f"✓ {date_str} | "
                f"dates {stats['dates_done']}/{stats['dates_total']} | "
                f"readings inserted: {stats['readings_inserted']:,}"
            )

        if not stop_cleanly and progress.get("status") == "running":
            progress["status"]      = "completed"
            progress["finished_at"] = now_utc()
            persist(message="Scraping completed successfully.")
            tqdm.write("All done!")

    finally:
        conn.close()
        signal.signal(signal.SIGTERM, sigterm_prev)
        signal.signal(signal.SIGINT,  sigint_prev)


if __name__ == "__main__":
    run_scraper()
