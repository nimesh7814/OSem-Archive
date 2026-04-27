#!/usr/bin/env python3
"""
geo_resolver.py
---------------
Resolves the region_code foreign key on the stations table by spatially
matching each station's geometry point against the boundaries table using
ST_Within (exact containment) with a nearest-neighbour fallback for
offshore / border stations.

Runs every 30 minutes. The first run starts 30 minutes after container
start so the boundaries table has time to be populated by boundaries-loader.

Progress and statistics are written to /data/geo_resolver_progress.json
so the monitor can display them.

Workflow per run
----------------
1. Count unresolved stations (region_code IS NULL AND geometry IS NOT NULL).
2. If none → log and sleep.
3. Resolve in batches of BATCH_SIZE using ST_Within, then nearest-neighbour
   fallback for anything still NULL.
4. Write progress.json with full stats.

Environment variables
---------------------
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD  — database connection
PROGRESS_FILE   — path to progress JSON (default /data/geo_resolver_progress.json)
RESOLVE_INTERVAL — seconds between runs (default 1800 = 30 min)
INITIAL_DELAY   — seconds to wait before the very first run (default 1800 = 30 min)
BATCH_SIZE      — stations resolved per transaction (default 500)
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

PROGRESS_FILE     = os.getenv("PROGRESS_FILE",    "/data/geo_resolver_progress.json")
RESOLVE_INTERVAL  = int(os.getenv("RESOLVE_INTERVAL",  "1800"))   # 30 min
INITIAL_DELAY     = int(os.getenv("INITIAL_DELAY",     "1800"))   # 30 min
BATCH_SIZE        = int(os.getenv("BATCH_SIZE",        "500"))

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
# Stats query — runs after every resolve pass
# ---------------------------------------------------------------------------

def fetch_stats(conn) -> dict:
    """
    Returns a dict with:
      - total_stations          — all stations in DB
      - stations_with_geometry  — stations that have a geometry point
      - stations_resolved       — stations with region_code set
      - stations_unresolved     — stations with geometry but no region_code
      - stations_no_geometry    — stations without any geometry
      - top_countries           — list of {country_code, country_name, station_count}
      - top_regions             — list of {region_code, region_name, country_name, station_count}
    """
    stats = {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # Totals
        cur.execute("""
            SELECT
                COUNT(*)                                          AS total_stations,
                COUNT(*) FILTER (WHERE geometry IS NOT NULL)     AS stations_with_geometry,
                COUNT(*) FILTER (WHERE region_code IS NOT NULL)  AS stations_resolved,
                COUNT(*) FILTER (
                    WHERE geometry IS NOT NULL
                      AND region_code IS NULL
                )                                                 AS stations_unresolved,
                COUNT(*) FILTER (WHERE geometry IS NULL)         AS stations_no_geometry
            FROM stations
        """)
        row = cur.fetchone()
        stats.update(dict(row))

        # Convert to plain ints (psycopg2 returns Decimal for COUNT)
        for k in stats:
            stats[k] = int(stats[k])

        # Top countries (by station count, limit 30)
        cur.execute("""
            SELECT
                b.country_code,
                b.country_name,
                COUNT(s.station_id) AS station_count
            FROM stations s
            JOIN boundaries b ON b.region_code = s.region_code
            GROUP BY b.country_code, b.country_name
            ORDER BY station_count DESC
            LIMIT 30
        """)
        stats["top_countries"] = [
            {
                "country_code":  r["country_code"],
                "country_name":  r["country_name"],
                "station_count": int(r["station_count"]),
            }
            for r in cur.fetchall()
        ]

        # Top regions (by station count, limit 50)
        cur.execute("""
            SELECT
                b.region_code,
                b.region_name,
                b.country_name,
                b.country_code,
                COUNT(s.station_id) AS station_count
            FROM stations s
            JOIN boundaries b ON b.region_code = s.region_code
            GROUP BY b.region_code, b.region_name, b.country_name, b.country_code
            ORDER BY station_count DESC
            LIMIT 50
        """)
        stats["top_regions"] = [
            {
                "region_code":   r["region_code"],
                "region_name":   r["region_name"],
                "country_name":  r["country_name"],
                "country_code":  r["country_code"],
                "station_count": int(r["station_count"]),
            }
            for r in cur.fetchall()
        ]

    return stats


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------

def resolve_pass(conn) -> dict:
    """
    Resolves region_code for all stations where geometry IS NOT NULL
    and region_code IS NULL.

    Pass 1 — ST_Within (exact containment, uses GIST index).
    Pass 2 — nearest-neighbour fallback for any still unresolved.

    Returns a summary dict with counts.
    """
    summary = {
        "unresolved_before":   0,
        "resolved_within":     0,
        "resolved_nn":         0,
        "still_unresolved":    0,
        "started_at":          now_utc(),
        "finished_at":         None,
    }

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM stations
            WHERE geometry IS NOT NULL AND region_code IS NULL
        """)
        unresolved_before = cur.fetchone()[0]

    summary["unresolved_before"] = int(unresolved_before)

    if unresolved_before == 0:
        summary["finished_at"] = now_utc()
        return summary

    print(f"  {unresolved_before:,} stations need region_code resolution.", flush=True)

    # ── Pass 1: ST_Within in batches ────────────────────────────────────────
    total_within = 0
    while not STOP_REQUESTED:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE stations s
                SET region_code = (
                    SELECT b.region_code
                    FROM boundaries b
                    WHERE ST_Within(s.geometry, b.geometry)
                    LIMIT 1
                )
                WHERE s.station_id IN (
                    SELECT station_id FROM stations
                    WHERE geometry IS NOT NULL AND region_code IS NULL
                    LIMIT %s
                )
                  AND EXISTS (
                    SELECT 1 FROM boundaries b
                    WHERE ST_Within(s.geometry, b.geometry)
                  )
            """, (BATCH_SIZE,))
            updated = cur.rowcount
        conn.commit()

        total_within += updated
        if updated > 0:
            print(f"  ST_Within pass: {total_within:,} resolved so far…", flush=True)
        if updated < BATCH_SIZE:
            break   # no more rows to process in this pass

    summary["resolved_within"] = total_within

    # ── Pass 2: nearest-neighbour fallback ──────────────────────────────────
    total_nn = 0
    while not STOP_REQUESTED:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE stations s
                SET region_code = (
                    SELECT b.region_code
                    FROM boundaries b
                    ORDER BY b.geometry <-> s.geometry
                    LIMIT 1
                )
                WHERE s.station_id IN (
                    SELECT station_id FROM stations
                    WHERE geometry IS NOT NULL AND region_code IS NULL
                    LIMIT %s
                )
            """, (BATCH_SIZE,))
            updated = cur.rowcount
        conn.commit()

        total_nn += updated
        if updated > 0:
            print(f"  Nearest-neighbour pass: {total_nn:,} resolved so far…", flush=True)
        if updated < BATCH_SIZE:
            break

    summary["resolved_nn"] = total_nn

    # ── Final count ──────────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM stations
            WHERE geometry IS NOT NULL AND region_code IS NULL
        """)
        still_unresolved = cur.fetchone()[0]

    summary["still_unresolved"] = int(still_unresolved)
    summary["finished_at"]      = now_utc()

    total_resolved = total_within + total_nn
    print(
        f"  Pass complete — "
        f"ST_Within: {total_within:,}  |  "
        f"Nearest-neighbour: {total_nn:,}  |  "
        f"Still unresolved: {still_unresolved:,}",
        flush=True,
    )

    return summary


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    wait_for_db()

    progress = load_progress()
    progress.update({
        "service":          "geo_resolver",
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
        f"Geo-resolver started. "
        f"Initial delay: {INITIAL_DELAY}s  |  "
        f"Interval: {RESOLVE_INTERVAL}s  |  "
        f"Batch size: {BATCH_SIZE}",
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
            time.sleep(min(remaining, 10))   # wake every 10s to check STOP
            continue

        # ── Run ──────────────────────────────────────────────────────────────
        run_number = progress["run_count"] + 1
        print(f"\n[Run #{run_number}] {now_utc()}", flush=True)
        progress["status"]     = "running"
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
    print("Geo-resolver stopped cleanly.", flush=True)


if __name__ == "__main__":
    run()
