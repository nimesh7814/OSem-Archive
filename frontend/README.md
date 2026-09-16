# Integrated Archive Frontend

This React Router application adapts the openSenseMap interface so users can explore recent and historical data in one map experience.

The map header contains an **Archive** switch:

- With archive mode off, the interface uses the regular openSenseMap API for live and recent station data.
- With archive mode on, it uses this project's archive API for the imported historical record.

The archive mode supports date ranges, country and region filters, indoor/outdoor exposure, phenomena, uploaded SHP/KML/GeoJSON areas of interest, result visualization, and asynchronous CSV or GeoJSON downloads.

For the project motivation, architecture, and full-stack setup, see the [root README](../README.md).

## Data Sources

The frontend talks to two independent APIs:

| Variable | Used for | Typical local value |
| --- | --- | --- |
| `OSEM_API_URL` | Regular openSenseMap live/recent data | `https://api.opensensemap.org/` |
| `ARCHIVE_API_URL` | Browser access to the archive API | `http://127.0.0.1:8001` |
| `ARCHIVE_API_INTERNAL_URL` | Server-rendered access to the archive API | `http://127.0.0.1:8001` locally, `http://api:8000` in Docker |

The separate internal URL matters in Docker: `127.0.0.1` inside the frontend container is the frontend container itself, while `api:8000` addresses the backend container over the Compose network.

## Recommended: Run the Full Stack

From the repository root:

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item frontend/.env.example frontend/.env

docker compose --env-file backend/.env up -d --build `
  timescaledb redis minio minio-init api celery-worker ingest-scheduler osem_frontend
```

Open http://localhost:3010.

Compose supplies the correct internal database and archive API addresses to the frontend container. The values in `frontend/.env` remain useful for running the frontend directly on the host.

## Run the Frontend Locally

### Requirements

- Node.js 24, matching `.nvmrc`
- npm
- A reachable PostgreSQL database used by the React Router application
- The archive API running at `http://localhost:8001`

### 1. Start the supporting stack

From the repository root:

```powershell
docker compose --env-file backend/.env up -d --build `
  timescaledb redis minio minio-init api celery-worker
```

### 2. Configure the frontend

```powershell
cd frontend
Copy-Item .env.example .env
```

For host-based frontend development, use the host-mapped database and API addresses:

```env
DATABASE_URL="postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@localhost:5433/<POSTGRES_DB>"
ARCHIVE_API_URL="http://127.0.0.1:8001"
ARCHIVE_API_INTERNAL_URL="http://127.0.0.1:8001"
OSEM_API_URL="https://api.opensensemap.org/"
```

Use the PostgreSQL values from `backend/.env` in place of the placeholders.

### 3. Install and start

```powershell
npm ci
npm run dev
```

Open http://localhost:3000.

Run npm commands from the `frontend` directory. Running `npm run dev` from the repository root fails because the root does not contain a `package.json`.

## Frontend Structure

| Path | Purpose |
| --- | --- |
| `app/routes/explore.tsx` | Main map, live data, archive queries, and downloads |
| `app/components/header/nav-bar/` | Search and live/archive mode controls |
| `app/components/map/` | Map layers and archive result presentation |
| `app/lib/archive-api.ts` | Public/internal archive API URL selection |
| `app/lib/opensensemap-api.server.ts` | Regular openSenseMap API access and archive statistics |
| `app/lib/env.server.ts` | Validated server and browser environment configuration |
| `public/` | Static images, icons, fonts, and manifest |
| `tests/` | Vitest test suite |

## Commands

Run these from `frontend/`:

```powershell
npm run dev          # Development server on port 3000
npm run build        # Production client and server build
npm run start        # Serve an existing production build
npm run test         # Vitest in watch mode
npm run test -- --run
npm run lint
npm run typecheck
npm run format:check
```

Database-backed tests require the `DATABASE_URL` in `frontend/.env` to point to a reachable test database. Do not aim destructive tests at a database containing archive data that must be preserved.

## Troubleshooting

### The page returns HTTP 500 in Docker

Check that the frontend container received the internal API URL:

```powershell
docker compose --env-file backend/.env exec osem_frontend printenv ARCHIVE_API_INTERNAL_URL
```

It should be `http://api:8000`.

### The API reports `degraded`

```powershell
Invoke-RestMethod http://localhost:8001/health
docker compose --env-file backend/.env ps -a
```

All four dependencies should report `ok`. `minio-init` may show `Exited (0)` because it is designed to finish after preparing the bucket.

### Archive filters are empty

The archive database starts without station measurements. Run the importer and follow its logs:

```powershell
docker compose --env-file backend/.env up ingest
docker compose --env-file backend/.env logs -f ingest
```
