# openSenseMap Archive

Local archive stack for openSenseMap historical measurements. The project includes a FastAPI backend, a React Router frontend, TimescaleDB, Redis, MinIO, Celery exports, and scheduled archive ingestion.

## Services

| Service | Purpose | Local URL / Port |
| --- | --- | --- |
| `frontend` | Web app | http://localhost:3010 |
| `api` | FastAPI archive API | http://localhost:8001 |
| `timescaledb` | Postgres + TimescaleDB | localhost:5433 |
| `pgadmin` | DB admin UI | http://localhost:5151 |
| `redis` | Cache + Celery broker | localhost:6379 |
| `minio` | Export object storage | http://localhost:9001 |
| `celery-worker` | Background export jobs | internal |
| `ingest` | Manual one-shot archive ingest | run on demand |
| `ingest_scheduler` | Daily automatic ingest | runs at midnight |

## Setup

Copy the environment examples and fill the real values:

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item frontend/.env.example frontend/.env
```

Start the stack:

```powershell
docker compose --env-file backend/.env up -d --build
```

Check API dependencies:

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:8001/health/dependencies
```

## Ingest

`ingest` runs the archive loader once and exits:

```powershell
docker compose --env-file backend/.env up ingest
```

`ingest_scheduler` stays running and executes the same ingest algorithm every day at:

```env
INGEST_SCHEDULE_TIME=00:00
INGEST_SCHEDULE_TIMEZONE=Europe/Berlin
```

Ingest progress is stored in the database table `ingest_log`.

## Useful Commands

```powershell
docker compose --env-file backend/.env ps
docker compose --env-file backend/.env logs -f api
docker compose --env-file backend/.env logs -f ingest_scheduler
docker compose --env-file backend/.env down
```

Use `down -v` only when you intentionally want to delete database and service volumes.

## More Details

See:

- [backend/README.md](backend/README.md)
- [frontend/README.md](frontend/README.md)
