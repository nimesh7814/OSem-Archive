# Frontend

React Router frontend for the openSenseMap archive stack.

## Requirements

- Node.js 24, see `.nvmrc`
- npm
- Docker, if running the full stack

## Environment

Copy the example file:

```powershell
Copy-Item .env.example .env
```

Important values:

```env
DATABASE_URL=postgresql://osem:osem_db_lock@timescaledb:5432/osem_archive
OSEM_API_URL=https://api.opensensemap.org/
```

When running through the root Docker Compose file, the frontend is available at:

```text
http://localhost:3010
```

## Run Locally

```powershell
cd frontend
npm install
npm run dev
```

Default local dev server:

```text
http://localhost:3000
```

## Run With Docker

From the repository root:

```powershell
docker compose --env-file backend/.env up -d osem_frontend
```

## Useful Commands

```powershell
npm run build
npm run start
npm run test
npm run lint
npm run typecheck
npm run format
```

## Structure

| Path | Purpose |
| --- | --- |
| `app/` | Routes, components, services, UI logic |
| `public/` | Static assets |
| `scripts/` | Utility scripts |
| `tests/` | Frontend tests |
| `db/` | Drizzle database files |
