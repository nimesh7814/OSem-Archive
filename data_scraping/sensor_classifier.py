#!/usr/bin/env python3
"""
sensor_classifier.py
--------------------
Populates the `type` column on the sensors table by matching each sensor's
sensor_info and unit fields against a rule table.

Mirrors geo_resolver.py exactly — runs on a configurable interval, processes
in batches, writes progress JSON to the shared scraper_data volume.

Safety guarantee
----------------
Only fills rows where `type IS NULL`.  Existing values are NEVER touched,
so data already present in the sensors table (whether scraped or set manually)
is always preserved.

To re-classify rows currently set to "Unknown" after adding new rules, set
OVERWRITE_UNKNOWN=1 for a single run:
    docker compose run --rm -e OVERWRITE_UNKNOWN=1 sensor-classifier

Workflow per run
----------------
1. Count sensors with type IS NULL (or type = 'Unknown' if OVERWRITE_UNKNOWN).
2. If none → log and sleep.
3. Fetch unclassified sensors in batches of BATCH_SIZE.
4. Apply rule matching in Python (sensor_info → unit → fallback).
5. Bulk-UPDATE type.  Rows matching no rule → set to "Unknown" so they are
   not re-evaluated on every run.
6. Write progress JSON.

Categories
----------
Temperature         Humidity            Pressure
Particulate Matter  UV                  Light
Noise               CO2 / Gas           Wind Speed
Wind Direction      Precipitation       Radiation
Altitude            Water Level         Voltage
Current             Power               Signal Strength
Motion / Vibration  Conductivity        GPS Coordinate
Rain Presence       Cloud Ceiling       Unknown

Environment variables
---------------------
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
PROGRESS_FILE       path to JSON          (default /data/sensor_classifier_progress.json)
CLASSIFY_INTERVAL   seconds between runs  (default 1800 = 30 min)
INITIAL_DELAY       seconds before first  (default 60)
BATCH_SIZE          sensors per tx        (default 500)
OVERWRITE_UNKNOWN   "1" to redo Unknowns  (default "0")
"""

import json
import os
import re
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

PROGRESS_FILE     = os.getenv("PROGRESS_FILE",    "/data/sensor_classifier_progress.json")
CLASSIFY_INTERVAL = int(os.getenv("CLASSIFY_INTERVAL", "1800"))
INITIAL_DELAY     = int(os.getenv("INITIAL_DELAY",      "60"))
BATCH_SIZE        = int(os.getenv("BATCH_SIZE",         "500"))
OVERWRITE_UNKNOWN = os.getenv("OVERWRITE_UNKNOWN", "0") == "1"

STOP_REQUESTED = False


def _handle_stop(signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print("Stop signal received — will exit after current batch.", flush=True)


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT,  _handle_stop)


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------
# Each entry:  (category, sensor_info_patterns, unit_patterns)
#
# Patterns are case-insensitive regex strings.
# If BOTH lists are non-empty → BOTH must match (AND).
# If only one list is non-empty → only that field is checked.
# Rules are evaluated in order; first match wins.

RULES: list[tuple[str, list[str], list[str]]] = [

    # ── Temperature ──────────────────────────────────────────────────────────
    ("Temperature", [], [
        r"^°[Cc]$", r"^[Cc]$", r"^[Cc]°$", r"^deg\.?\s*[Cc]$",
        r"celsius", r"celcius", r"grad celsius",
        r"^[Ff]$", r"fahrenheit",
        r"^ºC$",    # Latin small letter o (U+00BA) — common typo
    ]),
    ("Temperature", [
        r"^temp\d*$", r"thermometer", r"DS18B20", r"DS1820",
        r"NTC", r"SHT\d", r"HTU21", r"S300TH",
    ], []),

    # ── Humidity ─────────────────────────────────────────────────────────────
    ("Humidity", [], [
        r"^%$", r"^%RH$", r"^RH$", r"^Rh$", r"^%rel$", r"^%RF$",
        r"rel\.?\s*feuchte", r"^Percent$",
    ]),
    ("Humidity", [r"^humidity\d*$", r"^GW$"], [r"^%$"]),

    # ── Pressure ─────────────────────────────────────────────────────────────
    ("Pressure", [], [
        r"^hPa$", r"^hpa$", r"^HPa$", r"^Pa$", r"^PA$", r"^Pascal$",
        r"^mBar$", r"^mbar$", r"^C\s*/\s*bar$",
    ]),
    ("Pressure", [r"^pressure\d*$", r"barometer", r"BMP\d", r"BME\d"], []),

    # ── Particulate Matter ───────────────────────────────────────────────────
    ("Particulate Matter", [], [
        r"µg/m[³3]", r"μg/m[³3]", r"µg$", r"µg/m\^3",
        r"pcs/", r"hppcf",
    ]),
    ("Particulate Matter", [
        r"SDS011", r"PPD42", r"SHINYEI", r"SPS30", r"PMS\d",
    ], []),

    # ── UV ───────────────────────────────────────────────────────────────────
    ("UV", [], [
        r"UV.?[Ii]ndex", r"µW/cm", r"μW/cm", r"µW/m", r"mW/cm",
        r"uw/cm", r"yw/m", r"yW/m",
    ]),
    ("UV", [r"VEML6070", r"GUVA", r"UV"], []),

    # ── Light ────────────────────────────────────────────────────────────────
    ("Light", [], [
        r"^[Ll]ux$", r"^LUX$", r"^[Ll]x$", r"^kLx$",
        r"lichtpegel", r"pegel", r"/1024",
    ]),
    ("Light", [
        r"TSL\d+", r"BH\d+", r"^GL\d+", r"brightness\d*",
        r"FWG14-SO", r"FWG14-SS", r"FWG14-SW", r"FWG14-D",
    ], []),

    # ── Noise ────────────────────────────────────────────────────────────────
    ("Noise", [], [r"^[Dd][Bb]$", r"schallpegel", r"^a\.u\.$"]),
    ("Noise", [
        r"^(sound|mic|MIC|LM358|LM386|ADMP401|SEN02281P|Mic.breakout)$",
        r"microphone", r"condenser",
    ], []),

    # ── CO2 / Gas ────────────────────────────────────────────────────────────
    ("CO2 / Gas", [], [r"^ppm$"]),
    ("CO2 / Gas", [
        r"MH.Z\d+", r"CCS811", r"SEN\d+", r"Multi.Gas",
        r"MULTICHANNEL", r"gas",
    ], []),

    # ── Wind Speed ───────────────────────────────────────────────────────────
    ("Wind Speed", [], [
        r"^m/s$", r"^km/h$", r"^bft$",
        r"m/s wind speed", r"m/second square",
    ]),
    ("Wind Speed", [r"anemometer", r"windspeed", r"wind", r"FWG14-V"], []),

    # ── Wind Direction ───────────────────────────────────────────────────────
    ("Wind Direction", [], [r"^°$", r"^˚$"]),
    ("Wind Direction", [r"FWG14-RI"], []),

    # ── Precipitation ────────────────────────────────────────────────────────
    ("Precipitation", [], [
        r"^mm$", r"^mm/min$", r"l/m.²?/h", r"mm\s*/m.²?\s*Tag",
    ]),
    ("Precipitation", [
        r"rain", r"regen", r"Davis.*Collector", r"Davis.*sammler", r"FWG14-R",
    ], []),

    # ── Radiation ────────────────────────────────────────────────────────────
    ("Radiation", [], [r"^CPM$"]),
    ("Radiation", [r"SBM.20", r"geiger", r"radiation"], []),

    # ── Altitude ─────────────────────────────────────────────────────────────
    ("Altitude", [r"^alt$"], [r"^m$"]),

    # ── Water Level ──────────────────────────────────────────────────────────
    ("Water Level", [], [r"^cm$"]),
    ("Water Level", [r"pegel"], []),

    # ── Voltage ──────────────────────────────────────────────────────────────
    ("Voltage", [], [r"^[Vv]$", r"^mV$"]),

    # ── Current ──────────────────────────────────────────────────────────────
    ("Current", [], [r"^A$"]),

    # ── Power / Energy ───────────────────────────────────────────────────────
    ("Power", [], [r"^W$", r"^Wh$"]),

    # ── Signal Strength ──────────────────────────────────────────────────────
    ("Signal Strength", [], [r"^dBm$"]),
    ("Signal Strength", [r"WiFi", r"ESP8266", r"internal"], []),

    # ── Motion / Vibration ───────────────────────────────────────────────────
    ("Motion / Vibration", [r"motion", r"vibration", r"PIR"], []),
    ("Motion / Vibration", [], [r"^G$", r"Meldungen"]),

    # ── Conductivity ─────────────────────────────────────────────────────────
    ("Conductivity", [], [r"^µS$"]),

    # ── GPS Coordinate ───────────────────────────────────────────────────────
    ("GPS Coordinate", [r"^lat$", r"^lng$", r"^lon$"], []),
    ("GPS Coordinate", [], [r"^d$"]),

    # ── Rain Presence ────────────────────────────────────────────────────────
    ("Rain Presence", [r"water sensor", r"regen", r"rain"], [r"^0/1"]),
    ("Rain Presence", [], [r"^0/1"]),

    # ── Cloud Ceiling ────────────────────────────────────────────────────────
    ("Cloud Ceiling", [r"Ceilometer"], []),
]


def _compile_rules():
    return [
        (cat,
         [re.compile(p, re.IGNORECASE) for p in si],
         [re.compile(p, re.IGNORECASE) for p in un])
        for cat, si, un in RULES
    ]


_COMPILED_RULES = _compile_rules()


def classify(sensor_info: str | None, unit: str | None) -> str:
    """Return a type string. Returns 'Unknown' when no rule matches."""
    si = (sensor_info or "").strip()
    un = (unit or "").strip()
    for category, si_pats, unit_pats in _COMPILED_RULES:
        si_ok   = not si_pats   or any(p.search(si) for p in si_pats)
        unit_ok = not unit_pats or any(p.search(un) for p in unit_pats)
        if si_ok and unit_ok:
            return category
    return "Unknown"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def wait_for_db(retries: int = 30, delay: int = 5) -> None:
    for attempt in range(1, retries + 1):
        try:
            get_conn().close()
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
# Classify pass
# ---------------------------------------------------------------------------

def _null_clause() -> str:
    if OVERWRITE_UNKNOWN:
        return "(type IS NULL OR type = 'Unknown')"
    return "type IS NULL"


def classify_pass(conn) -> dict:
    summary = {
        "unclassified_before": 0,
        "classified":          0,
        "still_unclassified":  0,
        "category_counts":     {},
        "started_at":          now_utc(),
        "finished_at":         None,
    }

    clause = _null_clause()

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM sensors WHERE {clause}")
        total = int(cur.fetchone()[0])

    summary["unclassified_before"] = total

    if total == 0:
        print("  Nothing to classify.", flush=True)
        summary["finished_at"] = now_utc()
        return summary

    print(f"  {total:,} sensors need type resolution.", flush=True)

    classified  = 0
    cat_counts: dict[str, int] = {}

    while not STOP_REQUESTED:
        # Always fetch at offset 0 — classified rows drop out of the WHERE
        # clause automatically, so offset 0 always gets the next fresh batch.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT sensor_id, sensor_info, unit
                FROM   sensors
                WHERE  {clause}
                ORDER  BY sensor_id
                LIMIT  %s
            """, (BATCH_SIZE,))
            batch = cur.fetchall()

        if not batch:
            break

        updates = []
        for row in batch:
            cat = classify(row["sensor_info"], row["unit"])
            updates.append((cat, row["sensor_id"]))
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                "UPDATE sensors SET type = %s WHERE sensor_id = %s",
                updates,
                page_size=BATCH_SIZE,
            )
        conn.commit()

        classified += len(updates)
        print(f"  Classified {classified:,} / {total:,}…", flush=True)

        if len(batch) < BATCH_SIZE:
            break

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM sensors WHERE {clause}")
        still = int(cur.fetchone()[0])

    summary["classified"]         = classified
    summary["still_unclassified"] = still
    summary["category_counts"]    = dict(sorted(cat_counts.items()))
    summary["finished_at"]        = now_utc()

    print(
        f"  Pass complete — {classified:,} resolved  |  {still:,} still NULL",
        flush=True,
    )
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat:<28} {cnt:,}", flush=True)

    return summary


def fetch_stats(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COALESCE(type, 'Unclassified') AS type,
                   COUNT(*) AS sensor_count
            FROM   sensors
            GROUP  BY type
            ORDER  BY sensor_count DESC
        """)
        return {r["type"]: int(r["sensor_count"]) for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Main loop — identical structure to geo_resolver.py
# ---------------------------------------------------------------------------

def run() -> None:
    wait_for_db()

    progress = load_progress()
    progress.update({
        "service":          "sensor_classifier",
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
        f"Sensor-classifier started. "
        f"Initial delay: {INITIAL_DELAY}s  |  "
        f"Interval: {CLASSIFY_INTERVAL}s  |  "
        f"Batch: {BATCH_SIZE}  |  "
        f"Overwrite unknown: {OVERWRITE_UNKNOWN}",
        flush=True,
    )

    next_run     = time.monotonic() + INITIAL_DELAY
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
            time.sleep(min(remaining, 10))
            continue

        run_number = progress["run_count"] + 1
        print(f"\n[Run #{run_number}] {now_utc()}", flush=True)
        progress["status"]      = "running"
        progress["last_run_at"] = now_utc()
        save_progress(progress)

        try:
            conn = get_conn()
            try:
                summary = classify_pass(conn)
                stats   = fetch_stats(conn)
            finally:
                conn.close()

            progress["run_count"]        = run_number
            progress["last_run_summary"] = summary
            progress["stats"]            = stats

        except Exception as e:
            print(f"  ERROR during classify pass: {e}", flush=True)
            progress["last_error"] = str(e)

        next_run     = time.monotonic() + CLASSIFY_INTERVAL
        next_run_iso = datetime.fromtimestamp(
            time.time() + CLASSIFY_INTERVAL, tz=timezone.utc
        ).isoformat()

        progress["status"]      = "waiting"
        progress["next_run_at"] = next_run_iso
        save_progress(progress)
        print(f"Next run at {next_run_iso}", flush=True)

    progress["status"] = "stopped"
    save_progress(progress)
    print("Sensor-classifier stopped cleanly.", flush=True)


if __name__ == "__main__":
    run()
