"""
OpenSenseMap Archive Indexer
============================
Crawls https://archive.opensensemap.org daily, extracts station metadata,
resolves country / region via PostGIS ST_Within against a pre-loaded
admin-boundary GeoJSON, and stores everything in PostgreSQL for fast
querying.  DuckDB uses sensor_files.csv_url for data access.

Usage
-----
    python indexer.py --mode daily        # index yesterday
    python indexer.py --mode backfill     # index all missing historical dates
    python indexer.py --mode date --date 2024-01-15   # index one date
    python indexer.py --mode seed-boundaries           # load admin_boundary.geojson
    python indexer.py --mode scheduler   # persistent daily scheduler (default)

Boundary file
-------------
Place your GeoJSON at  data/admin_boundary.geojson  (relative to this script).
Required properties on each feature:
    adm0_name  →  country name   (e.g. "Germany")
    adm1_name  →  region / state (e.g. "Bavaria")

Run  --mode seed-boundaries  once (or whenever the file changes) to load /
refresh the admin_boundaries table.
"""

import asyncio
import argparse
import json
import logging
import os
import pathlib
from datetime import date, timedelta, datetime

import httpx
import asyncpg
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# Local sensor-categorisation module
from sensor_types import categorize_sensor

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/indexer.log"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config from environment
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL        = os.getenv("ARCHIVE_BASE_URL", "https://archive.opensensemap.org")
DATABASE_URL    = os.getenv("DATABASE_URL")
CONCURRENCY     = int(os.getenv("CRAWL_CONCURRENCY", "5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
ARCHIVE_START   = date(2014, 6, 3)   # earliest date in the archive

# Path to the admin boundary GeoJSON (can be overridden by env var)
BOUNDARY_PATH = pathlib.Path(
    os.getenv("ADMIN_BOUNDARY_PATH", "data/admin_boundary.geojson")
)


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────
async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


async def is_date_indexed(pool: asyncpg.Pool, date_str: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM index_log WHERE date = $1", date_str
        )
        return bool(row and row["status"] == "success")


async def get_all_indexed_dates(pool: asyncpg.Pool) -> set:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date FROM index_log WHERE status = 'success'"
        )
        return {str(r["date"]) for r in rows}


async def get_pending_dates(pool: asyncpg.Pool) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date FROM index_log WHERE status IN ('pending', 'failed') ORDER BY date"
        )
        return [str(r["date"]) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Boundary seeding  (run once: python indexer.py --mode seed-boundaries)
# ─────────────────────────────────────────────────────────────────────────────
async def seed_boundaries(pool: asyncpg.Pool) -> None:
    """
    Load (or refresh) admin_boundaries from BOUNDARY_PATH.

    The GeoJSON must have features whose properties include:
        adm0_name  – country name
        adm1_name  – region / state name
    Geometry may be Polygon or MultiPolygon; both are cast to MULTIPOLYGON.
    """
    if not BOUNDARY_PATH.exists():
        raise FileNotFoundError(
            f"Admin boundary file not found: {BOUNDARY_PATH}\n"
            "Set ADMIN_BOUNDARY_PATH env var or place the file at data/admin_boundary.geojson"
        )

    log.info(f"Loading boundaries from {BOUNDARY_PATH} …")
    with open(BOUNDARY_PATH, encoding="utf-8") as fh:
        geojson = json.load(fh)

    features = geojson.get("features", [])
    log.info(f"Found {len(features)} features in boundary file")

    inserted = updated = skipped = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Truncate and reload for a clean refresh
            await conn.execute("TRUNCATE admin_boundaries RESTART IDENTITY CASCADE")

            for feat in features:
                props = feat.get("properties") or {}
                geom  = feat.get("geometry")

                adm0 = props.get("adm0_name") or props.get("NAME_0") or props.get("COUNTRY")
                adm1 = props.get("adm1_name") or props.get("NAME_1") or props.get("region")

                if not geom:
                    skipped += 1
                    continue

                # Normalise to MULTIPOLYGON
                geom_type = geom.get("type", "")
                if geom_type == "Polygon":
                    geom = {"type": "MultiPolygon", "coordinates": [geom["coordinates"]]}
                elif geom_type != "MultiPolygon":
                    log.warning(f"Skipping unsupported geometry type: {geom_type}")
                    skipped += 1
                    continue

                geom_json = json.dumps(geom)

                await conn.execute("""
                    INSERT INTO admin_boundaries (adm0_name, adm1_name, geom)
                    VALUES ($1, $2, ST_SetSRID(ST_GeomFromGeoJSON($3), 4326))
                """, adm0, adm1, geom_json)

                inserted += 1

    log.info(
        f"Boundary seed complete — {inserted} inserted, {skipped} skipped"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PostGIS location lookup  (replaces Nominatim reverse geocoding)
# ─────────────────────────────────────────────────────────────────────────────
_location_cache: dict[tuple[float, float], dict] = {}


async def lookup_location(pool: asyncpg.Pool, lat: float, lon: float) -> dict:
    """
    Resolve country and region for a (lat, lon) point using ST_Within
    against the admin_boundaries table.

    Returns a dict with keys: country, region  (both may be None if the
    point falls outside all boundaries, e.g. ocean / unmapped territory).

    Results are cached in memory (rounded to ~1 km precision) to avoid
    redundant DB round-trips for stations that are close together.
    """
    if lat is None or lon is None:
        return {}

    # Round to 2 dp (~1 km) for cache key
    cache_key = (round(lat, 2), round(lon, 2))
    if cache_key in _location_cache:
        return _location_cache[cache_key]

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT adm0_name AS country,
                       adm1_name AS region
                FROM   admin_boundaries
                WHERE  ST_Within(
                           ST_SetSRID(ST_MakePoint($1, $2), 4326),
                           geom
                       )
                LIMIT 1
            """, lon, lat)   -- ST_MakePoint(lon, lat) — X=lon, Y=lat

        result = {
            "country": row["country"] if row else None,
            "region":  row["region"]  if row else None,
        }

    except Exception as exc:
        log.warning(f"Location lookup failed for ({lat}, {lon}): {exc}")
        result = {}

    _location_cache[cache_key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Archive crawlers
# ─────────────────────────────────────────────────────────────────────────────
async def date_exists_on_archive(client: httpx.AsyncClient, date_str: str) -> bool:
    try:
        res = await client.head(f"{BASE_URL}/{date_str}/")
        return res.status_code == 200
    except Exception as e:
        log.warning(f"Could not check archive for {date_str}: {e}")
        return False


async def fetch_station_folders(client: httpx.AsyncClient, date_str: str) -> list[str]:
    """Return list of station folder names for a given date."""
    res = await client.get(f"{BASE_URL}/{date_str}/")
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    folders = []
    for a in soup.select("table a[href]"):
        href = a["href"]
        if href.startswith("?") or href in ("/", "../", f"/{date_str}/"):
            continue
        folders.append(href.strip("/"))
    return folders


async def fetch_station_metadata(
    client: httpx.AsyncClient, date_str: str, station_folder: str
) -> dict | None:
    """
    Fetch the .json metadata file for a station.
    Folder format: "{boxID}-{stationName}"
    """
    parts = station_folder.split("-", 1)
    if len(parts) < 2:
        return None

    station_name = parts[1]
    json_url = f"{BASE_URL}/{date_str}/{station_folder}/{station_name}-{date_str}.json"

    try:
        res = await client.get(json_url, timeout=REQUEST_TIMEOUT)
        if res.status_code == 200:
            return res.json()
        log.debug(f"No metadata at {json_url} (status {res.status_code})")
    except Exception as e:
        log.debug(f"Failed metadata fetch for {station_folder}: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Station & sensor upserts
# ─────────────────────────────────────────────────────────────────────────────
async def upsert_station(
    conn: asyncpg.Connection,
    meta: dict,
    date_str: str,
    location: dict,
) -> str | None:
    """
    Upsert a station row.  Returns the station UUID (str) or None.

    *location* is the dict returned by lookup_location():
        {"country": "Germany", "region": "Bavaria"}
    """
    box_id = meta.get("_id")
    if not box_id:
        return None

    coords = meta.get("currentLocation", {}).get("coordinates", [None, None])
    lon = coords[0] if coords and len(coords) > 0 else None
    lat = coords[1] if coords and len(coords) > 1 else None

    # Build WKT point for PostGIS; keep NULL when coordinates are absent
    point_wkt = f"SRID=4326;POINT({lon} {lat})" if lon is not None and lat is not None else None

    station_uuid = await conn.fetchval("""
        INSERT INTO stations (
            box_id, name,
            location,
            country, region,
            fs_date, ls_date,
            init_date
        )
        VALUES (
            $1, $2,
            ST_GeomFromEWKT($3),
            $4, $5,
            $6, $6,
            now()
        )
        ON CONFLICT (box_id) DO UPDATE SET
            name     = EXCLUDED.name,
            location = COALESCE(EXCLUDED.location, stations.location),
            country  = COALESCE(EXCLUDED.country,  stations.country),
            region   = COALESCE(EXCLUDED.region,   stations.region),
            ls_date  = GREATEST(stations.ls_date,  EXCLUDED.ls_date)
        RETURNING uuid
    """,
        box_id,
        meta.get("name"),
        point_wkt,
        location.get("country"),
        location.get("region"),
        datetime.strptime(date_str, "%Y-%m-%d").date(),
    )

    return str(station_uuid)


async def upsert_station_date(
    conn: asyncpg.Connection,
    station_uuid: str,
    date_str: str,
    station_folder: str,
) -> None:
    folder_url = f"{BASE_URL}/{date_str}/{station_folder}/"
    await conn.execute("""
        INSERT INTO station_dates (station_uuid, date, folder_url)
        VALUES ($1, $2, $3)
        ON CONFLICT (station_uuid, date) DO NOTHING
    """, station_uuid, date_str, folder_url)


async def upsert_sensors(
    conn: asyncpg.Connection,
    station_uuid: str,
    meta: dict,
    date_str: str,
    station_folder: str,
) -> int:
    """
    Upsert all sensors for a station and create sensor_files rows.
    Returns the number of sensors processed.
    """
    sensors_raw = meta.get("sensors", [])
    count = 0

    for sensor in sensors_raw:
        sensor_id = sensor.get("_id")
        if not sensor_id:
            continue

        title    = sensor.get("title", "") or ""
        unit     = sensor.get("unit",  "") or ""
        category = categorize_sensor(title, unit)

        sensor_uuid = await conn.fetchval("""
            INSERT INTO sensors (
                sensor_id, station_uuid, title, type, category, unit, init_date
            )
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (sensor_id) DO UPDATE SET
                title    = COALESCE(EXCLUDED.title,    sensors.title),
                type     = COALESCE(EXCLUDED.type,     sensors.type),
                category = COALESCE(EXCLUDED.category, sensors.category),
                unit     = COALESCE(EXCLUDED.unit,     sensors.unit)
            RETURNING uuid
        """,
            sensor_id,
            station_uuid,
            title,
            sensor.get("sensorType"),
            category,
            unit,
        )

        csv_url = f"{BASE_URL}/{date_str}/{station_folder}/{sensor_id}-{date_str}.csv"

        await conn.execute("""
            INSERT INTO sensor_files (sensor_uuid, station_uuid, date, csv_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (sensor_uuid, date) DO NOTHING
        """, str(sensor_uuid), station_uuid, date_str, csv_url)

        count += 1

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Process one station folder
# ─────────────────────────────────────────────────────────────────────────────
async def process_station(
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    date_str: str,
    station_folder: str,
    semaphore: asyncio.Semaphore,
) -> bool:
    async with semaphore:
        meta = await fetch_station_metadata(client, date_str, station_folder)
        if not meta:
            log.debug(f"No metadata for {station_folder} on {date_str}")
            return False

        # Extract coordinates and resolve country/region via PostGIS
        coords = meta.get("currentLocation", {}).get("coordinates", [None, None])
        lon = coords[0] if coords and len(coords) > 0 else None
        lat = coords[1] if coords and len(coords) > 1 else None
        location = await lookup_location(pool, lat, lon)

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    station_uuid = await upsert_station(conn, meta, date_str, location)
                    if not station_uuid:
                        return False

                    await upsert_station_date(conn, station_uuid, date_str, station_folder)
                    await upsert_sensors(conn, station_uuid, meta, date_str, station_folder)

            return True

        except Exception as e:
            log.error(f"DB error for station {station_folder} on {date_str}: {e}")
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Index a single date
# ─────────────────────────────────────────────────────────────────────────────
async def index_date(date_str: str, pool: asyncpg.Pool) -> None:
    log.info(f"Starting index for {date_str}")

    if await is_date_indexed(pool, date_str):
        log.info(f"{date_str} already indexed — skipping")
        return

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        if not await date_exists_on_archive(client, date_str):
            log.warning(f"{date_str} not yet available on archive — marking pending")
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO index_log (date, status, station_count)
                    VALUES ($1, 'pending', 0)
                    ON CONFLICT (date) DO NOTHING
                """, date_str)
            return

        try:
            folders = await fetch_station_folders(client, date_str)
        except Exception as e:
            log.error(f"Failed to fetch folder list for {date_str}: {e}")
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO index_log (date, status, station_count)
                    VALUES ($1, 'failed', 0)
                    ON CONFLICT (date) DO UPDATE SET status = 'failed'
                """, date_str)
            return

        log.info(f"{date_str}: found {len(folders)} station folders")

        semaphore = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            process_station(client, pool, date_str, folder, semaphore)
            for folder in folders
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if r is True)
        failed_count  = sum(1 for r in results if r is False or isinstance(r, Exception))
        status = "success" if failed_count == 0 else "partial"

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.error(f"Exception for folder {folders[i]}: {r}")

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO index_log (date, status, station_count, indexed_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (date) DO UPDATE SET
                    status        = EXCLUDED.status,
                    station_count = EXCLUDED.station_count,
                    indexed_at    = now()
            """, date_str, status, success_count)

        log.info(
            f"{date_str}: done — {success_count} success, "
            f"{failed_count} failed, status={status}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Daily job  (scheduler target)
# ─────────────────────────────────────────────────────────────────────────────
async def daily_job(pool: asyncpg.Pool) -> None:
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"Daily job triggered for {yesterday}")
    await index_date(yesterday, pool)

    pending = await get_pending_dates(pool)
    if pending:
        log.info(f"Retrying {len(pending)} pending/failed dates")
        for d in pending:
            await index_date(d, pool)


# ─────────────────────────────────────────────────────────────────────────────
# Backfill — index all missing historical dates
# ─────────────────────────────────────────────────────────────────────────────
async def backfill(pool: asyncpg.Pool) -> None:
    indexed   = await get_all_indexed_dates(pool)
    yesterday = date.today() - timedelta(days=1)

    all_dates: list[str] = []
    current = ARCHIVE_START
    while current <= yesterday:
        all_dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    missing = [d for d in all_dates if d not in indexed]
    log.info(f"Backfill: {len(missing)} dates to index out of {len(all_dates)} total")

    for d in missing:
        await index_date(d, pool)
        await asyncio.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    parser = argparse.ArgumentParser(description="OpenSenseMap Archive Indexer")
    parser.add_argument(
        "--mode",
        choices=["daily", "backfill", "date", "seed-boundaries", "scheduler"],
        default="scheduler",
        help=(
            "daily            = index yesterday\n"
            "backfill         = index all missing historical dates\n"
            "date             = index a specific date (use --date YYYY-MM-DD)\n"
            "seed-boundaries  = load / refresh admin_boundary.geojson into DB\n"
            "scheduler        = persistent daily scheduler (default)\n"
        ),
    )
    parser.add_argument("--date", help="Specific date to index (YYYY-MM-DD)", default=None)
    args = parser.parse_args()

    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")

    pool = await create_pool()
    log.info("Database connection pool created")

    try:
        if args.mode == "seed-boundaries":
            await seed_boundaries(pool)

        elif args.mode == "daily":
            await daily_job(pool)

        elif args.mode == "backfill":
            await backfill(pool)

        elif args.mode == "date":
            if not args.date:
                raise ValueError("--date is required when mode=date")
            await index_date(args.date, pool)

        elif args.mode == "scheduler":
            scheduler = AsyncIOScheduler()

            scheduler.add_job(
                daily_job,
                "cron",
                hour=6, minute=0,
                args=[pool],
                id="daily_index",
            )

            # Retry pending dates at 08:00 UTC (archive may publish late)
            async def retry_pending() -> None:
                pending = await get_pending_dates(pool)
                if pending:
                    await asyncio.gather(*[index_date(d, pool) for d in pending])

            scheduler.add_job(
                retry_pending,
                "cron",
                hour=8, minute=0,
                id="retry_pending",
            )

            scheduler.start()
            log.info("Scheduler started — daily job at 06:00 UTC, retry at 08:00 UTC")

            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, SystemExit):
                scheduler.shutdown()
                log.info("Scheduler stopped")

    finally:
        await pool.close()
        log.info("Database pool closed")


if __name__ == "__main__":
    asyncio.run(main())
