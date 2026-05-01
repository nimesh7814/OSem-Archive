# osem_db — Docker Setup

PostgreSQL 16 + TimescaleDB + PostGIS + pgAdmin, fully containerized.

## Stack

| Service     | Image                           | Default Port |
|-------------|---------------------------------|--------------|
| TimescaleDB | `timescale/timescaledb-ha:pg16` | `5435`       |
| pgAdmin     | `dpage/pgadmin4`                | `5050`       |

---

## Files

```
database/
├── docker-compose.yml
├── .env                  ← copy from .env.example and fill in
├── .env.example
├── schema.sql            ← schema, runs automatically on first start
├── servers.json          ← pgAdmin auto-registration
├── load_schema.py        ← manually run schema against DB
├── load_data.py          ← load archive data into DB
├── requirements.txt      ← Python dependencies
└── README.md
```

---

## Quick Start

### 1. Configure environment
```bash
# Windows PowerShell
Copy-Item .env.example .env

# Linux / macOS
cp .env.example .env
```
Edit `.env` and set your passwords.

### 2. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 3. Start everything
```bash
docker compose up -d
```

On first start this will:
1. Create named Docker volumes `osem_postgres_data` and `osem_pgadmin_data` automatically
2. Run `db-init` to fix volume permissions (required for Windows/Docker Desktop)
3. Start TimescaleDB
4. Start pgAdmin with `osem_db` pre-registered

### 4. Load the schema manually
```bash
python load_schema.py
```

### 5. Open pgAdmin
Visit http://localhost:5050 and log in with your `.env` credentials.
The `osem_db` server is pre-registered — enter the DB password when prompted.

---

## Loading Data

### Load archive data
```bash
# Load all dates from a local archive folder
python load_data.py --source /path/to/archive

# Filter by date range
python load_data.py --source /path/to/archive --start 2014-06-01 --end 2014-06-30

# Dry run — preview only, no DB writes, outputs CSVs to ./dry_run_output/
python load_data.py --source /path/to/archive --dry-run

# Dry run with date range
python load_data.py --source /path/to/archive --dry-run --start 2014-06-01 --end 2014-06-30

# Load from internet archive
python load_data.py --source https://archive.example.com/osem/
```

### Manually reload schema (if needed)
```bash
python load_schema.py
```

---

## Volume Safety

Data is stored in named Docker volumes and is **not lost** when containers are removed.

| Command                        | Volumes        |
|--------------------------------|----------------|
| `docker compose down`          | ✅ Kept        |
| `docker compose up -d`         | ✅ Kept        |
| `docker rm osem_timescaledb`   | ✅ Kept        |
| `docker compose down --volumes`| ❌ Destroyed   |
| `docker volume rm osem_postgres_data` | ❌ Destroyed |

> ⚠️ Never run `docker compose down --volumes` unless you intentionally want to wipe all data.

---

## Reset (wipe all data and re-run schema)
```bash
docker compose down --volumes
docker compose up -d
python load_schema.py
```

---

## Connect with psql
```bash
docker exec -it osem_timescaledb psql -U osem_user -d osem_db
```

---

## Stop containers
```bash
# Stop only — data is kept
docker compose down

# Stop and delete all data — irreversible
docker compose down --volumes
```
