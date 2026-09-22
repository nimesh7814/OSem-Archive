# Frontend

The frontend is a React Router application that adds Archive mode to the openSenseMap map interface. Users can switch between current and historical data without leaving the application. For the project overview and complete Docker workflow, see the [root README](../README.md).

## Frontend Responsibilities

- Preserve the existing Live mode for recent data from the regular openSenseMap API.
- Provide Archive mode filters for country, region, exposure, sensor type, date range, and custom areas of interest.
- Display matching stations and measurement summaries on the map.
- Start and monitor CSV or GeoJSON exports from the archive API.

## Requirements

- Node.js 24, see `.nvmrc`
- npm
- Docker, if running the full stack

## Local Environment

From the `frontend` directory, create the local environment file:

```powershell
Copy-Item .env.example .env
```

For a frontend process running on the host, make sure its database connection uses the host port exposed by Docker:

```env
DATABASE_URL=postgresql://<user>:<password>@localhost:5433/<database>
OSEM_API_URL=https://api.opensensemap.org/
ARCHIVE_API_URL=http://localhost:8001
ARCHIVE_API_INTERNAL_URL=http://localhost:8001
```

Use the same PostgreSQL credentials configured in `backend/.env`. `ARCHIVE_API_URL` is the address used by the browser, while `ARCHIVE_API_INTERNAL_URL` is used during server-side rendering. Both point to the host API during local development. The root Docker Compose configuration overrides the internal address with the API container name automatically.

## Run the Frontend Locally

Start the required backend services from the repository root, then install and run the frontend:

```powershell
docker compose --env-file backend/.env up -d timescaledb redis minio minio-init api celery-worker
cd frontend
npm ci
npm run dev
```

Open http://localhost:3000. When the complete stack is started through the root Compose workflow, the frontend is instead available at http://localhost:3010.

## Validate Changes

The route and database tests create and remove records, so point `DATABASE_URL` at a disposable development or test database before running them.

```powershell
npm run build
npm run test -- --run
npm run lint
npm run typecheck
npm run format:check
```

Use `npm run format` to apply formatting when needed.

## Project Structure

| Path | Purpose |
| --- | --- |
| `app/routes/` | Pages and API routes |
| `app/components/` | Reusable interface components |
| `app/services/` | Application and data-access services |
| `app/lib/` | Shared clients, schemas, and utilities |
| `db/` | Drizzle schema and migration files |
| `public/` | Static assets |
| `scripts/` | Database and development utilities |
| `tests/` | Frontend tests |
