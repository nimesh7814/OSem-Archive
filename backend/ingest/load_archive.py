import os
import re
import csv
import io
import json
import requests
import psycopg2

from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util import Retry


# Setup database connection
env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=env_path)

DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

conn = psycopg2.connect(
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
)

conn.autocommit = False
print(f"Connected to database: {DB_NAME} at {DB_HOST}:{DB_PORT} as user {DB_USER}\n")

URL = "https://archive.opensensemap.org"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHECKPOINT_PATH = Path(__file__).resolve().parent / ".ingest_checkpoint.json"

# Handle HTTP requests with retries
session = requests.Session()
retry = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=(500, 502, 503, 504),
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

# Load the last completed day
def load_checkpoint() -> date | None:
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        return date.fromisoformat(data["last_completed_day"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

# Save the Last Completed Day
def save_checkpoint(day: date) -> None:
    CHECKPOINT_PATH.write_text(
        json.dumps({"last_completed_day": day.isoformat()}),
        encoding="utf-8",
    )

# Get the available date range
def get_available_date_range() -> tuple[date, date]:
    dirs = list_dirs(f"{URL}/")
    valid_dates = sorted(
        date.fromisoformat(d) for d in dirs if DATE_PATTERN.match(d)
    )
    if not valid_dates:
        raise RuntimeError("No date folders found on the archive root listing")
    return valid_dates[0], valid_dates[-1]


def list_dirs(index_url: str) -> list[str]:
    resp = session.get(index_url, timeout=30)
    resp.raise_for_status()
    hrefs = re.findall(r'href="([^"]+/)"', resp.text)
    names = [h.strip("/").split("/")[-1] for h in hrefs]
    return [n for n in names if n and n != ".." and not n.startswith("?")]


def list_files(index_url: str) -> dict:
    resp = session.get(index_url, timeout=30)
    resp.raise_for_status()
    hrefs = re.findall(r'href="([^"]+\.(?:csv|json))"', resp.text)

    result = {"json": None, "csv": []}
    for h in hrefs:
        full_url = index_url + h
        if h.endswith(".json"):
            result["json"] = full_url
        else:
            result["csv"].append(full_url)
    return result


def upsert_box(box_id: str, box_json: dict) -> None:
    exposure = box_json.get("exposure")
    if exposure not in ("indoor", "outdoor", "mobile"):
        exposure = None

    coordinates = ((box_json.get("loc") or {}).get("geometry") or {}).get("coordinates")
    location_wkt = f"POINT({coordinates[0]} {coordinates[1]})" if coordinates and len(coordinates) >= 2 else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO boxes (id, name, box_type, exposure, model, location, updated_at)
            VALUES (%s, %s, %s, %s, %s, ST_GeomFromText(%s, 4326), now())
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                box_type = EXCLUDED.box_type,
                exposure = EXCLUDED.exposure,
                model = EXCLUDED.model,
                location = COALESCE(EXCLUDED.location, boxes.location),
                updated_at = now()
            """,
            (box_id, box_json.get("name", box_id), None, exposure, box_json.get("model"), location_wkt),
        )


def upsert_sensors(box_id: str, box_json: dict) -> None:
    sensors = box_json.get("sensors", [])
    if not sensors:
        return

    rows = []
    for s in sensors:
        sensor_id = s.get("_id") or s.get("id")
        if not sensor_id:
            continue
        rows.append(
            (sensor_id, box_id, s.get("title", "unknown"), s.get("unit"), s.get("sensorType"))
        )

    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO sensors (id, box_id, title, unit, sensor_type)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                unit = EXCLUDED.unit,
                sensor_type = EXCLUDED.sensor_type
            """,
            rows,
        )


def load_sensor_csv(sensor_id: str, day: date, csv_text: str) -> int:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for raw in reader:
        try:
            ts = raw["createdAt"]
            value = float(raw["value"]) if raw.get("value") not in (None, "") else None
        except (KeyError, ValueError):
            continue
        rows.append((ts, sensor_id, value))

    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM measurements
            WHERE sensor_id = %s
              AND time >= %s::date
              AND time <  %s::date + INTERVAL '1 day'
            """,
            (sensor_id, day, day),
        )
        execute_values(
            cur,
            "INSERT INTO measurements (time, sensor_id, value) VALUES %s",
            rows,
        )

    return len(rows)


def assign_region(box_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE boxes b
            SET region_id = r.id
            FROM regions r
            WHERE b.id = %s
              AND b.location IS NOT NULL
              AND b.region_id IS NULL
              AND ST_Contains(r.geometry::geometry, b.location::geometry)
            """,
            (box_id,),
        )


def is_already_done(box_id: str, day: date) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM ingest_log WHERE box_id = %s AND archive_date = %s",
            (box_id, day),
        )
        row = cur.fetchone()
        return row is not None and row[0] == "done"


def mark_ingest_log(box_id: str, day: date, status: str, row_count: int = 0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_log (box_id, archive_date, status, row_count, loaded_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (box_id, archive_date) DO UPDATE SET
                status    = EXCLUDED.status,
                row_count = EXCLUDED.row_count,
                loaded_at = now()
            """,
            (box_id, day, status, row_count),
        )


def refresh_summary() -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_summary()")
    conn.commit()


def scrape_day(day: date) -> None:
    day_url = f"{URL}/{day.isoformat()}/"

    try:
        box_dirs = list_dirs(day_url)
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            tqdm.write(f"  {day}: no archive folder (404) — skipping")
            return
        raise

    station_bar = tqdm(
        box_dirs,
        desc=f"{day} stations",
        unit="box",
        leave=False,
    )

    for box_dir_name in station_bar:
        box_id = box_dir_name.split("-", 1)[0]
        station_bar.set_postfix_str(box_id)

        if is_already_done(box_id, day):
            continue

        box_url = f"{day_url}{box_dir_name}/"
        try:
            files = list_files(box_url)
            if not files["json"]:
                mark_ingest_log(box_id, day, status="failed")
                conn.commit()
                continue

            box_json = session.get(files["json"], timeout=30).json()

            upsert_box(box_id, box_json)
            conn.commit()

            upsert_sensors(box_id, box_json)
            assign_region(box_id)

            total_rows = 0
            for csv_url in files["csv"]:
                sensor_id = csv_url.split("/")[-1].split("-", 1)[0]
                csv_text = session.get(csv_url, timeout=30).text
                total_rows += load_sensor_csv(sensor_id, day, csv_text)

            mark_ingest_log(box_id, day, status="done", row_count=total_rows)
            conn.commit()

        except Exception as exc:
            conn.rollback()
            try:
                mark_ingest_log(box_id, day, status="failed")
                conn.commit()
            except Exception:
                conn.rollback()
            tqdm.write(f"  failed: {box_id} on {day} — {exc}")

    station_bar.close()


def scrape_range(date_from: date | None = None, date_to: date | None = None) -> None:
    archive_first, archive_latest = get_available_date_range()

    if date_to is None:
        date_to = archive_latest

    if date_from is None:
        checkpoint = load_checkpoint()
        if checkpoint is not None:
            date_from = checkpoint + timedelta(days=1)
            print(f"Resuming from checkpoint: last completed day was {checkpoint}")
        else:
            date_from = archive_first

    total_days = (date_to - date_from).days + 1
    print(f"Scraping: {date_from} -> {date_to} ({total_days} days)\n")

    day_bar = tqdm(total=total_days, desc="Days remaining", unit="day")
    day = date_from
    try:
        while day <= date_to:
            scrape_day(day)
            save_checkpoint(day)
            day += timedelta(days=1)
            day_bar.update(1)
    except KeyboardInterrupt:
        tqdm.write(f"\nInterrupted. Checkpoint saved at {day - timedelta(days=1)}. Re-run to resume.")
        raise
    finally:
        day_bar.close()


if __name__ == "__main__":
    scrape_range()
    refresh_summary()
    conn.close()
    print("\nDone.")
