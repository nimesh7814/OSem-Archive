# Archive Backend

The backend converts the existing folder-based openSenseMap archive into a queryable, compressed historical data service. It consists of a FastAPI API, an importer, a daily scheduler, a TimescaleDB database, Redis, Celery, and MinIO.

For the complete project purpose and full-stack quick start, begin with the [root README](../README.md).

## Responsibilities

- Import daily station metadata and sensor CSV files from `archive.opensensemap.org`.
- Store raw historical measurements in a dedicated TimescaleDB database.
- Compress raw measurement chunks after seven days.
- Maintain hourly, daily, monthly, and yearly continuous aggregates.
- Query archived data by country, region, date, phenomenon, exposure, or uploaded area of interest.
- Generate large CSV and GeoJSON exports asynchronously.

The archive database is deliberately separate from the live openSenseMap database. The live service can remain optimized for current writes while this mostly read-only database is optimized for historical queries and compression. Its regular writes are the daily archive imports.

## Backend Structure

| Path | Purpose |
| --- | --- |
| `api/main.py` | FastAPI application entrypoint |
| `api/app/app.py` | Routes and dependency health checks |
| `api/app/measurements.py` | Region and AOI measurement queries |
| `api/app/tasks.py` | Celery export tasks |
| `api/app/bucket.py` | MinIO storage and download URLs |
| `ingest/load_archive.py` | Resumable folder archive importer |
| `ingest/scheduler.py` | Daily importer scheduler |
| `db/migrations/` | Schema, compression, aggregates, and indexes |
| `data/admin_boundary.geojson` | Country and region boundary seed data |

## Run With Docker

### 1. Configure the backend

From the repository root:

```powershell
Copy-Item backend/.env.example backend/.env
```

Replace the placeholder passwords in `backend/.env`. In particular, configure the PostgreSQL, pgAdmin, and MinIO credentials before using the stack outside a disposable local environment.

### 2. Start backend services

```powershell
docker compose --env-file backend/.env up -d --build `
  timescaledb redis minio minio-init api celery-worker ingest-scheduler
```

This starts the API and scheduled ingestion but does not start the full historical backfill.

### 3. Check dependency health

```powershell
Invoke-RestMethod http://localhost:8001/health
```

Expected dependencies:

```text
database      ok
redis         ok
minio         ok
celeryWorker  ok
```

Useful API addresses:

- API root: http://localhost:8001
- Dependency health: http://localhost:8001/health
- OpenAPI documentation: http://localhost:8001/docs

## Archive Ingestion

### Initial historical import

Run the one-shot importer explicitly:

```powershell
docker compose --env-file backend/.env up ingest
```

On an empty database, it discovers the earliest and latest date folders on `archive.opensensemap.org` and imports them in chronological order. For every station/day it:

1. Reads the station JSON metadata.
2. Upserts the station and its sensors.
3. Assigns the station to a geographic region.
4. Replaces that sensor/day's measurements with the archived CSV contents.
5. Records completion and row counts in `ingest_log`.

The importer commits progress continuously. If interrupted, the next run resumes after the last completed day and skips station/day combinations already marked as done.

### Daily scheduled import

The scheduler runs the same importer at the configured time:

```env
INGEST_SCHEDULE_TIME=00:00
INGEST_SCHEDULE_TIMEZONE=Europe/Berlin
INGEST_SCHEDULER_RUN_ON_START=false
```

Follow its progress with:

```powershell
docker compose --env-file backend/.env logs -f ingest-scheduler
```

## Storage Design

`measurements` is a TimescaleDB hypertable split into seven-day chunks. Chunks older than seven days are compressed, segmented by `sensor_id`, and ordered by descending timestamp. This fits the archive workload because older measurements are read frequently but changed only when an archive day is re-imported.

Continuous aggregates provide progressively coarser query tiers:

| View | Source | Intended use |
| --- | --- | --- |
| `reading_hourly` | Raw measurements | Short-range detailed analysis |
| `reading_daily` | Hourly aggregate | Multi-month and default archive queries |
| `reading_monthly` | Daily aggregate | Long-range trends |
| `reading_yearly` | Monthly aggregate | Whole-history overview |

Important tables:

| Table | Purpose |
| --- | --- |
| `measurements` | Raw archived sensor readings |
| `boxes` | Station metadata and location |
| `sensors` | Sensor metadata and latest archived value |
| `regions` | Country and administrative-region polygons |
| `summary` | Archive-wide counts |
| `ingest_log` | Resumable import checkpoint and per-day status |
| `export_job` | Export metadata retained for schema compatibility |

Schema files under `db/migrations/` are applied automatically only when the TimescaleDB volume is first created. Do not delete the volume merely to reapply a migration when it contains imported archive data.

## API Overview

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Database, Redis, MinIO, and worker health |
| `GET /stats` | Archive-wide counts |
| `GET /boxes` | Archived station metadata |
| `GET /countries` | Available countries and regions |
| `GET /tags` | Available sensor types |
| `GET /phenomena` | Available measured phenomena |
| `GET /exposure` | Available exposure values |
| `GET /regions/{country}/{region}/measurements` | Region query |
| `POST /aoi/validate` | Validate an uploaded AOI file |
| `POST /aoi/measurements` | AOI measurement query |
| `POST .../exports` | Queue a CSV or GeoJSON export |
| `GET /exports/{job_id}` | Poll export status |
| `GET /exports/{job_id}/download` | Download a completed export |

## Asynchronous Exports

Exports use the following pipeline:

```text
FastAPI -> Redis queue -> Celery worker -> MinIO -> signed download URL
```

The API refuses export work when one of those dependencies is unavailable. Use `/health` as the first diagnostic check.

## Run the API Locally

Docker is recommended for the supporting services:

```powershell
docker compose --env-file backend/.env up -d timescaledb redis minio minio-init
```

For host-based Python development, ensure `backend/.env` uses host addresses:

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
REDIS_HOST=localhost
MINIO_ENDPOINT=localhost:9000
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
```

Then install and run:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Run backend tests with:

```powershell
python -m unittest api.tests.test_backend_api
```

## Database Access

```powershell
docker exec -it timescaledb psql -U <POSTGRES_USER> -d <POSTGRES_DB>
```

Use the values from `backend/.env` in place of the placeholders.
