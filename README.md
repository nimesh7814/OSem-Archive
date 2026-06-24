# openSenseMap Archive API

A FastAPI-based read service for the [openSenseMap](https://opensensemap.org) historical archive
([archive.opensensemap.org](https://archive.opensensemap.org)), backed by **TimescaleDB**.
The API mirrors the route/parameter structure of [api.opensensemap.org](https://api.opensensemap.org)
(documented at [docs.opensensemap.org](https://docs.opensensemap.org)), but is read-only and optimized
for serving and exporting historical data at scale (15B+ measurement rows).

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Download / Export](#download--export)
- [Data Ingestion](#data-ingestion)
- [Schema Migrations](#schema-migrations)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running Locally](#running-locally)
- [Running with Docker](#running-with-docker)
- [Scaling Notes](#scaling-notes)
- [Testing](#testing)
- [License](#license)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                            FRONTEND                                  │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │ REST/JSON
┌────────────────────────────────▼───────────────────────────────────────┐
│                       BACKEND: FastAPI Archive API                     │
│   /boxes  /boxes/:id  /boxes/data  /stats  /tags  /download  ...      │
│                                                                         │
│                  Service / Repository layer (SQLAlchemy async)        │
└───────────────────────────────────────┬─────────────────────────────────┘
                                         │
┌────────────────────────────────────────▼─────────────────────────────────┐
│                          TimescaleDB (Postgres + TS)                    │
│  boxes | sensors | regions | phenomena | measurements (hypertable)      │
│  measurements_hourly / measurements_daily (continuous aggregates)       │
└────────────────────────────────────────▲─────────────────────────────────┘
                                         │ bulk COPY (per chunk/day)
┌────────────────────────────────────────┴─────────────────────────────────┐
│                  INGEST WORKER (separate process, not the API)           │
│   fetch archive dumps → enrich (country/region) → bulk load →            │
│   compress chunk → record in ingest_log                                  │
└────────────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────────┐
│                  EXPORT WORKER (background download jobs)               │
│   query → stream to file → upload to object storage → signed URL        │
└────────────────────────────────────────────────────────────────────────┘
```

**Design principle:** ingestion, serving, and export are three independent processes.
The API never runs ingestion or large exports inline — both are handed off to background
workers so the request/response path stays fast regardless of dataset size.

---

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app entrypoint
│   │   ├── core/
│   │   │   ├── config.py            # env-based settings
│   │   │   └── db.py                # async SQLAlchemy engine/session
│   │   ├── models/                  # ORM models
│   │   │   ├── box.py
│   │   │   ├── sensor.py
│   │   │   ├── region.py
│   │   │   ├── phenomenon.py
│   │   │   ├── measurement.py
│   │   │   └── ingest_log.py
│   │   ├── schemas/                 # Pydantic request/response models
│   │   ├── api/
│   │   │   ├── routes_root.py       # GET /
│   │   │   ├── routes_stats.py      # GET /stats, /statistics/descriptive
│   │   │   ├── routes_tags.py       # GET /tags
│   │   │   ├── routes_boxes.py      # GET /boxes, /boxes/:id, /boxes/:id/sensors
│   │   │   ├── routes_data.py       # GET /boxes/data, /boxes/data/bytag, /boxes/:id/data/:sensorId
│   │   │   └── routes_download.py   # GET /download, /download/status, /download/result
│   │   ├── services/                # business logic, range routing (raw vs aggregate)
│   │   └── repositories/            # SQL/ORM query layer
│   │
│   ├── ingest/
│   │   ├── fetch_archive.py         # pulls daily dumps from archive.opensensemap.org
│   │   ├── geocode.py               # offline country/region enrichment
│   │   ├── transform.py             # CSV/JSON normalization
│   │   ├── load.py                  # bulk COPY into TimescaleDB
│   │   └── scheduler.py             # cron / Celery beat entrypoint
│   │
│   ├── jobs/
│   │   └── export_worker.py         # background download job processor
│   │
│   ├── migrations/                  # Alembic
│   │   ├── env.py
│   │   ├── alembic.ini
│   │   └── versions/
│   │
│   └── tests/
│
├── frontend/                        # existing frontend application
│
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Database Schema

TimescaleDB (PostgreSQL + Timescale extension).

### `regions`
Normalized country/region lookup (Option B), used to filter and group boxes geographically.

| Column        | Type    | Notes                          |
|---------------|---------|---------------------------------|
| id            | SERIAL  | PK                              |
| country_code  | CHAR(2) | ISO 3166-1 alpha-2              |
| country_name  | TEXT    |                                  |
| region_name   | TEXT    | state/province                  |

Unique on `(country_code, region_name)`.

### `boxes`
SenseBox station metadata.

| Column              | Type                  | Notes                              |
|---------------------|------------------------|-------------------------------------|
| id                  | TEXT PK                | original senseBox ID                |
| name                | TEXT                    |                                      |
| exposure            | TEXT                    | indoor / outdoor / mobile            |
| model               | TEXT                    |                                      |
| location            | GEOGRAPHY(POINT, 4326)  | PostGIS point                        |
| region_id           | INT FK → regions.id     |                                      |
| created_at          | TIMESTAMPTZ             |                                      |
| updated_at          | TIMESTAMPTZ             |                                      |
| last_measurement_at | TIMESTAMPTZ             |                                      |

Indexes: `region_id`, GiST on `location`.

### `sensors`
Sensor metadata per box.

| Column        | Type   | Notes                          |
|---------------|--------|----------------------------------|
| id            | TEXT PK| original sensor ID                |
| box_id        | TEXT FK → boxes.id |                       |
| title         | TEXT   | e.g. "Temperatur"                 |
| unit          | TEXT   |                                    |
| sensor_type   | TEXT   |                                    |
| phenomenon_id | INT FK → phenomena.id |                   |

### `phenomena`
Normalized phenomenon/unit reference.

| Column        | Type   |
|---------------|--------|
| id            | SERIAL PK |
| name          | TEXT   |
| default_unit  | TEXT   |

### `measurements` (hypertable)
The core time-series table. **No denormalized `box_id`** — box-level queries join through `sensors`
to keep row width minimal at 15B+ scale.

| Column      | Type          | Notes                              |
|-------------|---------------|--------------------------------------|
| time        | TIMESTAMPTZ   | hypertable partition column          |
| sensor_id   | TEXT FK       |                                       |
| value       | REAL          | 4 bytes — sensor precision doesn't need float64 |

PK: `(sensor_id, time)`. Chunk interval: `1 day` (tune to actual ingestion rate).
Compression enabled (`segmentby = sensor_id`, `orderby = time DESC`), policy after 7 days.

### `measurements_hourly` / `measurements_daily`
Continuous aggregates (avg/min/max/count) used for any query spanning more than ~2 days.
Routing between raw table and aggregates happens in `services/`, not in the route handlers.

### `ingest_log`
Tracks ingestion progress for safe, resumable backfills.

| Column        | Type   |
|---------------|--------|
| id            | SERIAL PK |
| box_id        | TEXT   |
| archive_date  | DATE   |
| status        | TEXT (pending/done/failed) |
| row_count     | INT    |
| loaded_at     | TIMESTAMPTZ |

---

## API Reference

Mirrors [api.opensensemap.org](https://api.opensensemap.org) route shape; **read-only**.

| Method | Path                              | Description                                      |
|--------|------------------------------------|---------------------------------------------------|
| GET    | `/`                                | Route index                                        |
| GET    | `/stats`                           | Global counts (boxes, sensors, measurements)       |
| GET    | `/tags`                            | All known box tags                                 |
| GET    | `/statistics/descriptive`         | Descriptive stats over a phenomenon/range          |
| GET    | `/boxes`                           | List boxes — filter by `exposure`, `model`, `phenomenon`, `bbox`, `country`, `region` |
| GET    | `/boxes/:boxId`                    | Single box metadata                                |
| GET    | `/boxes/:boxId/sensors`            | Sensors for a box                                  |
| GET    | `/boxes/:boxId/data/:sensorId`     | Measurements for one sensor                        |
| GET    | `/boxes/data`                      | Multi-box measurement query (`bbox`, `phenomenon`, `from`, `to`) |
| GET    | `/boxes/data/bytag`                | Measurements grouped by tag                        |
| GET    | `/download`                        | Request a CSV/JSON export (see below)              |
| GET    | `/download/status/{job_id}`        | Check export job status                            |
| GET    | `/download/result/{job_id}`        | Retrieve signed URL / stream for completed export  |

Example:

```bash
curl "http://localhost:8000/boxes?country=DE&region=NRW&exposure=outdoor"

curl "http://localhost:8000/boxes/data?phenomenon=Temperatur&bbox=7,51,8,52&from=2024-01-01&to=2024-01-31"
```

---

## Download / Export

Given the dataset size, exports are **always asynchronous** — there is no synchronous CSV
download path, to avoid a single request trying to materialize tens of millions of rows.

1. `GET /download?boxId=...&phenomenon=...&from=...&to=...&format=csv` → `202 Accepted` + `job_id`
2. `jobs/export_worker.py` picks up the job, streams the query via a server-side cursor,
   writes CSV in batches, uploads the result to object storage (S3/MinIO).
3. `GET /download/status/{job_id}` → `pending | running | done | failed`
4. `GET /download/result/{job_id}` → signed URL to the generated file

Unauthenticated requests are capped on row count / date range per job, mirroring the limits
of the public openSenseMap API.

---

## Data Ingestion

`ingest/` is a standalone process, run on a schedule — **never** invoked from the API.

```
fetch_archive.py   → pulls daily CSV/JSON dumps from archive.opensensemap.org
geocode.py          → offline country/region enrichment (GeoNames-based, no external API calls)
transform.py        → normalizes raw dump rows into box/sensor/measurement records
load.py              → bulk COPY into TimescaleDB, per chunk/day; compress_chunk() after load
scheduler.py         → cron / Celery beat entrypoint, resumable via ingest_log
```

Run manually:

```bash
python -m ingest.fetch_archive --from 2024-01-01 --to 2024-01-31
python -m ingest.load --from 2024-01-01 --to 2024-01-31
```

Or via the scheduler (recommended for ongoing daily syncs):

```bash
python -m ingest.scheduler
```

Failed days are retried from `ingest_log` without re-importing already-loaded data.

---

## Schema Migrations

Managed with **Alembic**. Timescale-specific operations (hypertable creation, compression
policies, continuous aggregates) are written as raw SQL inside migration files — Alembic's
autogenerate does not produce these automatically.

```bash
cd backend
alembic revision --autogenerate -m "description of change"
# review the generated file, then:
alembic upgrade head
```

Current migration history:

```
0001_init_schema.py               # boxes, sensors, phenomena, measurements + hypertable
0002_regions_and_boxes_fk.py       # regions table, boxes.region_id (Option B)
0003_value_to_real.py              # measurements.value DOUBLE PRECISION -> REAL
0004_enable_compression.py         # compression settings on measurements
0005_compression_policy.py         # add_compression_policy
0006_continuous_aggregate_hourly.py
0007_continuous_aggregate_daily.py
0008_retention_policy.py           # optional — only if raw data isn't kept indefinitely
```

> At billions-of-rows scale, test migrations against a representative-size chunk before
> running on production. Prefer add-column → backfill → swap over in-place `ALTER COLUMN`
> for any migration touching the `measurements` table.

---

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ with the TimescaleDB extension
- PostGIS extension (for `boxes.location`)
- Docker & Docker Compose (recommended for local dev)

### Install

```bash
git clone <repo-url>
cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and set:

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/osem_archive
ARCHIVE_BASE_URL=https://archive.opensensemap.org
OBJECT_STORAGE_ENDPOINT=http://localhost:9000
OBJECT_STORAGE_BUCKET=osem-exports
OBJECT_STORAGE_ACCESS_KEY=
OBJECT_STORAGE_SECRET_KEY=
EXPORT_MAX_ROWS=5000000
LOG_LEVEL=INFO
```

---

## Running Locally

```bash
cd backend
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

API available at `http://localhost:8000`, interactive docs at `http://localhost:8000/docs`.

Run the ingest worker separately:

```bash
python -m ingest.scheduler
```

Run the export worker separately:

```bash
python -m jobs.export_worker
```

---

## Running with Docker

```bash
docker-compose up -d
```

This starts:
- `timescaledb` — PostgreSQL + TimescaleDB + PostGIS
- `api` — FastAPI app
- `ingest-worker` — scheduled archive sync
- `export-worker` — background download job processor
- `object-storage` — MinIO (S3-compatible) for export files

---

## Scaling Notes

This project is built around a measurements table in the **billions of rows**:

- `measurements` has no denormalized `box_id` — box filtering joins through `sensors` to keep
  row width minimal.
- `value` is stored as `REAL`, not `DOUBLE PRECISION`.
- Compression is enabled and mandatory, not optional — applied after 7 days via policy.
- Any query spanning more than ~2 days is routed to `measurements_hourly` or
  `measurements_daily` continuous aggregates, never the raw table, by the service layer.
- All exports are asynchronous background jobs with row/range caps.
- Ingestion loads with compression disabled, then explicitly compresses each chunk after load.
- Chunk interval is tuned to keep ~25M–100M rows per chunk based on actual ingestion rate.

See inline comments in `app/services/` and `migrations/versions/0004_enable_compression.py`
onward for the specific Timescale settings in use.

---

## Testing

```bash
cd backend
pytest tests/ -v
```

Integration tests run against a disposable TimescaleDB container (see `tests/conftest.py`
for fixture setup) and use a small representative dataset, not the full archive.

---

## License

Specify your project's license here (e.g. MIT, Apache 2.0). The openSenseMap archive data
itself is published as Open Data — confirm attribution requirements at
[opensensemap.org](https://opensensemap.org) before redistribution.
