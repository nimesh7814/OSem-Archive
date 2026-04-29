#!/usr/bin/env python3
"""
date_resolver.py
----------------
Backfills two columns that record "when did data first appear":

  sensors.sensor_date   — DATE of the earliest reading for that sensor
  stations.station_date — DATE of the earliest reading across all sensors
                          belonging to that station

Both are derived from the readings hypertable using MIN(recorded_at).

Modelled exactly after geo_resolver.py:
  • Configurable initial delay and repeat interval
  • Batched updates to avoid long-running transactions
  • SIGTERM / SIGINT caught for a clean shutdown
  • Progress JSON written to the shared scraper_data volume so the
    monitor can display it

Workflow per run
----------------
1. Count sensors with sensor_date IS NULL that have at least one reading.
2. If none → log and sleep until next interval.
3. Resolve sensors in batches:
     UPDATE sensors SET sensor_date = (SELECT MIN(recorded_at)::date …)
4. After every sensor batch, roll up to stations:
     UPDATE stations SET station_date = (SELECT MIN(sensor_date) …)
     for any station whose station_date is still NULL.
5. Write progress.json with full stats.

Environment variables
---------------------
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD — database connection
PROGRESS_FILE      — path to JSON  (default /data/date_resolver_progress.json)
RESOLVE_INTERVAL   — seconds between runs (default 1800 = 30 min)
INITIAL_DELAY      — seconds before first run (default 60)
BATCH_SIZE         — sensors processed per transaction (default 500)
OVERWRITE_EXISTING — set to "1" to re-fill rows that already have a date
                     (useful if readings were backdated; default "0")
"""

import json
import os
import signal
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "opensensemap"),
    "user":     os.getenv("DB_USER",     "osm"),
    "password": os.getenv("DB_PASSWORD", "changeme"),
}

PROGRESS_FILE      = os.getenv("PROGRESS_FILE",    "/data/date_resolver_progress.json")
RESOLVE_INTERVAL   = int(os.getenv("RESOLVE_INTERVAL",  "1800"))  # 30 min
INITIAL_DELAY      = int(os.getenv("INITIAL_DELAY",       "60"))  # 1 min
BATCH_SIZE         = int(os.getenv("BATCH_SIZE",          "500"))
OVERWRITE_EXISTING = os.getenv("OVERWRITE_EXISTING", "0") == "1"

STOP_REQUESTED = False


def _handle_stop(signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print("Stop signal received — will exit after current batch.", flush=True)


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT,  _handle_stop)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def wait_for_db(retries: int = 30, delay: int = 5) -> None:
    for attempt in range(1, retries + 1):
        try:
            conn = get_conn()
            conn.close()
            print("Database is ready.", flush=True)
            return
        except psycopg2.OperationalError as e:
            print(f"Waiting for DB… attempt {attempt}/{retries}: {e}", flush=True)
            time.sleep(delay)
    raise RuntimeError("Database did not become ready in time.")


def save_progress(data: dict) -> None:
    os.makedirs(os.path.dirname(PROGRESS_FILE) or ".", exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)


def load_progress() -> dict:
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Stats query — called after every resolve pass
# ---------------------------------------------------------------------------

def fetch_stats(conn) -> dict:
    """
    Returns aggregate counts for the progress file / monitor display:
      - total_sensors            — all sensors in DB
      - sensors_with_date        — sensors with sensor_date filled
      - sensors_without_date     — sensors that have readings but no date yet
      - sensors_no_readings      — sensors with zero readings (nothing to fill)
      - total_stations           — all stations in DB
      - stations_with_date       — stations with station_date filled
      - stations_without_date    — stations with sensors but no station_date yet
    """
    stats = {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # Sensor counts
        cur.execute("""
            SELECT
                COUNT(*)                                               AS total_sensors,
                COUNT(*) FILTER (WHERE sensor_date IS NOT NULL)       AS sensors_with_date,
                COUNT(*) FILTER (
                    WHERE sensor_date IS NULL
                      AND EXISTS (
                          SELECT 1 FROM readings r
                           WHERE r.sensor_dbid = sensors.sensor_dbid
                      )
                )                                                      AS sensors_without_date,
                COUNT(*) FILTER (
                    WHERE sensor_date IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM readings r
                           WHERE r.sensor_dbid = sensors.sensor_dbid
                      )
                )                                                      AS sensors_no_readings
            FROM sensors
        """)
        row = cur.fetchone()
        stats.update({k: int(v) for k, v in row.items()})

        # Station counts
        cur.execute("""
            SELECT
                COUNT(*)                                               AS total_stations,
                COUNT(*) FILTER (WHERE station_date IS NOT NULL)      AS stations_with_date,
                COUNT(*) FILTER (
                    WHERE station_date IS NULL
                      AND EXISTS (
                          SELECT 1
                            FROM sensors se
                            JOIN readings r ON r.sensor_dbid = se.sensor_dbid
                           WHERE se.station_dbid = stations.station_dbid
                      )
                )                                                      AS stations_without_date
            FROM stations
        """)
        row = cur.fetchone()
        stats.update({k: int(v) for k, v in row.items()})

    return stats


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------

def _sensor_null_clause() -> str:
    """WHERE clause fragment that selects sensors still needing a date."""
    if OVERWRITE_EXISTING:
        return "TRUE"  # touch every sensor
    return "sensor_date IS NULL"


def resolve_pass(conn) -> dict:
    """
    Pass 1 — fill sensors.sensor_date from MIN(readings.recorded_at).
    Pass 2 — fill stations.station_date from MIN(readings.recorded_at) directly.

    Both passes run in batches so transactions stay short and the DB is
    never locked for a long time.

    Returns a summary dict suitable for the progress file.
    """
    summary = {
        "sensors_before":   0,
        "sensors_resolved": 0,
        "sensors_skipped":  0,   # had no readings → nothing to set
        "stations_resolved": 0,
        "started_at":       now_utc(),
        "finished_at":      None,
    }

    sensor_clause = _sensor_null_clause()

    # ── Count sensors that still need resolution ─────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FROM sensors
            WHERE {sensor_clause}
              AND EXISTS (
                  SELECT 1 FROM readings r WHERE r.sensor_dbid = sensors.sensor_dbid
              )
        """)
        pending_sensors = int(cur.fetchone()[0])

    summary["sensors_before"] = pending_sensors

    if pending_sensors == 0 and not OVERWRITE_EXISTING:
        # Check whether any station dates still need rolling up even if all
        # sensor dates are already set (e.g. new sensors added since last run).
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM stations
                WHERE station_date IS NULL
                  AND EXISTS (
                      SELECT 1
                        FROM sensors se
                        JOIN readings r ON r.sensor_dbid = se.sensor_dbid
                       WHERE se.station_dbid = stations.station_dbid
                  )
            """)
            pending_stations = int(cur.fetchone()[0])

        if pending_stations == 0:
            print("  Nothing to resolve.", flush=True)
            summary["finished_at"] = now_utc()
            return summary
        else:
            print(
                f"  All sensor dates already filled; "
                f"{pending_stations:,} station(s) still need rolling up.",
                flush=True,
            )

    else:
        print(f"  {pending_sensors:,} sensor(s) need sensor_date resolution.", flush=True)

    # ── Pass 1: fill sensors.sensor_date in batches ──────────────────────────
    total_sensor_resolved = 0
    total_sensor_skipped  = 0

    while not STOP_REQUESTED:
        with conn.cursor() as cur:
            # We grab a batch of sensor PKs that still need a date AND have
            # at least one reading.  The subquery MIN(recorded_at) is evaluated
            # per-row inside the UPDATE, so each sensor gets its own minimum.
            cur.execute(f"""
                UPDATE sensors s
                SET sensor_date = (
                    SELECT MIN(r.recorded_at)::date
                      FROM readings r
                     WHERE r.sensor_dbid = s.sensor_dbid
                )
                WHERE s.sensor_dbid IN (
                    SELECT sensor_dbid FROM sensors
                    WHERE {sensor_clause}
                      AND EXISTS (
                          SELECT 1 FROM readings r WHERE r.sensor_dbid = sensors.sensor_dbid
                      )
                    ORDER BY sensor_dbid
                    LIMIT %s
                )
            """, (BATCH_SIZE,))
            updated = cur.rowcount
        conn.commit()

        total_sensor_resolved += updated
        if updated > 0:
            print(
                f"  sensor_date pass: {total_sensor_resolved:,} sensors resolved so far…",
                flush=True,
            )
        if updated < BATCH_SIZE:
            break  # exhausted the pending set

    summary["sensors_resolved"] = total_sensor_resolved

    # ── Pass 2: fill stations.station_date from MIN(readings.recorded_at) ──────
    # Goes directly to the readings hypertable via the station's sensors —
    # same source of truth as sensor_date, not a rollup of sensor_date.
    station_clause = "TRUE" if OVERWRITE_EXISTING else "station_date IS NULL"
    total_station_resolved = 0

    while not STOP_REQUESTED:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE stations st
                SET station_date = (
                    SELECT MIN(r.recorded_at)::date
                      FROM readings r
                      JOIN sensors se ON se.sensor_dbid = r.sensor_dbid
                     WHERE se.station_dbid = st.station_dbid
                )
                WHERE st.station_dbid IN (
                    SELECT station_dbid FROM stations
                    WHERE {station_clause}
                      AND EXISTS (
                          SELECT 1
                            FROM sensors se
                            JOIN readings r ON r.sensor_dbid = se.sensor_dbid
                           WHERE se.station_dbid = stations.station_dbid
                      )
                    ORDER BY station_dbid
                    LIMIT %s
                )
            """, (BATCH_SIZE,))
            updated = cur.rowcount
        conn.commit()

        total_station_resolved += updated
        if updated > 0:
            print(
                f"  station_date pass: {total_station_resolved:,} stations resolved so far…",
                flush=True,
            )
        if updated < BATCH_SIZE:
            break

    summary["stations_resolved"] = total_station_resolved
    summary["finished_at"]       = now_utc()

    print(
        f"  Pass complete — "
        f"sensors: {total_sensor_resolved:,}  |  "
        f"stations: {total_station_resolved:,}",
        flush=True,
    )

    return summary


# ---------------------------------------------------------------------------
# Main loop  (mirrors geo_resolver.py exactly)
# ---------------------------------------------------------------------------

def run() -> None:
    wait_for_db()

    progress = load_progress()
    progress.update({
        "service":          "date_resolver",
        "status":           "starting",
        "started_at":       now_utc(),
        "next_run_at":      None,
        "last_run_at":      progress.get("last_run_at"),
        "run_count":        progress.get("run_count", 0),
        "last_run_summary": progress.get("last_run_summary"),
        "stats":            progress.get("stats"),
    })
    save_progress(progress)

    print(
        f"Date-resolver started. "
        f"Initial delay: {INITIAL_DELAY}s  |  "
        f"Interval: {RESOLVE_INTERVAL}s  |  "
        f"Batch size: {BATCH_SIZE}  |  "
        f"Overwrite existing: {OVERWRITE_EXISTING}",
        flush=True,
    )

    # ── Initial delay ────────────────────────────────────────────────────────
    next_run = time.monotonic() + INITIAL_DELAY
    next_run_iso = datetime.fromtimestamp(
        time.time() + INITIAL_DELAY, tz=timezone.utc
    ).isoformat()

    print(f"First run scheduled at {next_run_iso}", flush=True)
    progress["status"]      = "waiting"
    progress["next_run_at"] = next_run_iso
    save_progress(progress)

    while not STOP_REQUESTED:
        remaining = next_run - time.monotonic()
        if remaining > 0:
            time.sleep(min(remaining, 10))  # wake every 10s to check STOP
            continue

        # ── Run ──────────────────────────────────────────────────────────────
        run_number = progress["run_count"] + 1
        print(f"\n[Run #{run_number}] {now_utc()}", flush=True)
        progress["status"]      = "running"
        progress["last_run_at"] = now_utc()
        save_progress(progress)

        try:
            conn = get_conn()
            try:
                summary = resolve_pass(conn)
                stats   = fetch_stats(conn)
            finally:
                conn.close()

            progress["run_count"]        = run_number
            progress["last_run_summary"] = summary
            progress["stats"]            = stats

        except Exception as e:
            print(f"  ERROR during resolve pass: {e}", flush=True)
            progress["last_error"] = str(e)

        # ── Schedule next run ────────────────────────────────────────────────
        next_run     = time.monotonic() + RESOLVE_INTERVAL
        next_run_iso = datetime.fromtimestamp(
            time.time() + RESOLVE_INTERVAL, tz=timezone.utc
        ).isoformat()

        progress["status"]      = "waiting"
        progress["next_run_at"] = next_run_iso
        save_progress(progress)
        print(f"Next run at {next_run_iso}", flush=True)

    # ── Clean shutdown ───────────────────────────────────────────────────────
    progress["status"] = "stopped"
    save_progress(progress)
    print("Date-resolver stopped cleanly.", flush=True)


if __name__ == "__main__":
    run()
