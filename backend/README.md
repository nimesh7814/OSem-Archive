# Backend

FastAPI backend for querying archived openSenseMap measurements and creating export jobs.

## Main Parts

| Path | Purpose |
| --- | --- |
| `api/main.py` | FastAPI entrypoint |
| `api/app/app.py` | API routes |
| `api/app/tasks.py` | Celery export tasks |
| `ingest/load_archive.py` | Archive ingest algorithm |
| `ingest/scheduler.py` | Daily ingest scheduler |
| `db/migrations/` | Initial schema, indexes, Timescale jobs |

## Run With Docker

From the repository root:

```powershell
docker compose --env-file backend/.env up -d api celery-worker redis minio timescaledb
```

API:

```text
http://localhost:8001
```

Dependency health:

```text
http://localhost:8001/health
```

## Run Locally

Install dependencies:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the API:

```powershell
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

When running locally, make sure `backend/.env` points to the correct database, Redis, and MinIO hosts.

## Ingest

Manual one-shot ingest:

```powershell
docker compose --env-file backend/.env up ingest
```

Scheduled ingest:

```powershell
docker compose --env-file backend/.env up -d ingest-scheduler
```

The scheduler runs `ingest/load_archive.py` every midnight by default. Progress is tracked in Postgres table:

```text
ingest_log
```

There is no file checkpoint. The database is the source of truth.

## Exports

Exports are asynchronous:

1. API creates an export job.
2. Redis queues the task.
3. Celery worker builds the file.
4. MinIO stores the result.
5. API returns job status and download URL.

The API reports dependency problems through `/health` and returns service errors if the database, Redis, MinIO, or workers are unavailable.

## Database

The schema is initialized from `db/migrations/*.sql` when the `timescaledb` volume is first created.

Important tables:

| Table | Purpose |
| --- | --- |
| `measurements` | Timescale hypertable |
| `boxes` | Station metadata |
| `sensors` | Sensor metadata |
| `regions` | Region polygons |
| `ingest_log` | Ingest progress |
| `export_job` | Export job records |

Inspect the DB:

```powershell
docker exec -it timescaledb psql -U osem -d osem_archive
```
