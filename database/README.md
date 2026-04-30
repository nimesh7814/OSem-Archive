# osem_db — Docker Setup

PostgreSQL 16 + TimescaleDB + PostGIS + pgAdmin, fully containerized.

## Stack

| Service     | Image                           | Default Port |
|-------------|---------------------------------|--------------|
| TimescaleDB | `timescale/timescaledb-ha:pg16` | `5432`       |
| pgAdmin     | `dpage/pgadmin4`                | `5050`       |

---

## Files

```
database/
├── docker-compose.yml
├── .env                  ← copy from .env.example and fill in
├── .env.example
├── init.sql              ← schema, runs on first start
├── servers.json          ← pgAdmin auto-registration
└── README.md
```

---

## Quick Start

### 1. Configure environment
```bash
# Windows PowerShell
Copy-Item .env.example .env
```
Edit `.env` and set your passwords.

### 2. Start everything
```powershell
docker compose up -d
```

On first start this will:
1. Run `db-init` to fix volume permissions (required for Windows)
2. Start TimescaleDB and run `init.sql` which creates all tables
3. Start pgAdmin with `osem_db` pre-registered

### 3. Open pgAdmin
Visit http://localhost:5050 and log in with your `.env` credentials.
The `osem_db` server is pre-registered — enter the DB password when prompted.

---

## Reset (re-run init.sql)
`init.sql` only runs when the volume is empty. To reset everything:

```powershell
docker compose down -v
docker compose up -d
```

---

## Connect with psql
```powershell
docker exec -it osem_timescaledb psql -U osem_user -d osem_db
```

---

## Stop containers
```powershell
# Stop only
docker compose down

# Stop and delete all data
docker compose down -v
```
