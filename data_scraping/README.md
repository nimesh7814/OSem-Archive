# OpenSenseMap → TimescaleDB Pipeline

A self-contained Docker stack that downloads the full [OpenSenseMap](https://opensensemap.org/) historical archive and stores it locally in a TimescaleDB database. Comes with a live progress monitor and an interactive station map — no external services required after first run.

---

## What is OpenSenseMap?

OpenSenseMap is an open platform where anyone can register a weather or environmental sensor station and stream readings to a public archive. The archive covers temperature, humidity, air pressure, particulate matter (PM2.5/PM10), UV index, and dozens of other phenomena, from tens of thousands of stations worldwide, going back to 2016. The full dataset is freely downloadable in daily CSV bundles from [archive.opensensemap.org](https://archive.opensensemap.org/).

This pipeline fetches every one of those bundles, parses the metadata and sensor readings, and loads them into a local database you fully control — enabling offline analysis, large-scale queries, and geospatial exploration that would be impractical to do against the live API.

---

## Why TimescaleDB?

The central table in this database — `readings` — is a time-series table. Every row is a `(sensor_id, recorded_at, value)` triple. At full archive scale this table will contain **hundreds of millions to billions of rows**. That specific shape of data is where TimescaleDB earns its keep over plain PostgreSQL or a general-purpose relational database.

### The core problem: vanilla PostgreSQL at scale

A standard PostgreSQL table degrades predictably as it grows. When you insert a new reading, the database must update indexes that span the entire history of the table. When you query a single sensor's readings over the last week, the planner has to scan or traverse indexes across all time periods. Vacuuming and autovacuum slow down as dead tuple chains grow across hundreds of millions of rows. Beyond ~100M rows, routine operations become noticeably sluggish without heroic manual partitioning work.

### How TimescaleDB solves this: hypertables and chunks

TimescaleDB introduces the concept of a **hypertable** — a table that transparently partitions its data into fixed time-range chunks behind the scenes. This pipeline uses weekly chunks.

```
readings  (hypertable)
├── chunk: 2016-01-01 → 2016-01-08
├── chunk: 2016-01-08 → 2016-01-15
├── ...
└── chunk: 2024-12-25 → 2025-01-01   ← recent writes land here
```

The practical benefits are substantial:

**Inserts stay fast forever.** New readings always land in the most recent chunk — a small, hot table with a compact index. Insert performance does not degrade as historical data accumulates, because the historical chunks are untouched.

**Time-range queries are dramatically faster.** When you ask for a sensor's readings between two dates, the query planner uses chunk exclusion to physically skip every chunk outside that range before evaluating a single row. A query for last month's data touches one or two chunks rather than a table spanning a decade.

**Compression reduces storage by 10–20×.** TimescaleDB can compress older chunks using columnar encoding. This pipeline compresses any chunk older than one day. A week of sensor readings that might occupy 500 MB uncompressed can shrink to 30–50 MB on disk — without any change to how you query the data. Decompression is transparent.

**Continuous aggregates are materialised and incrementally refreshed.** This pipeline defines four pre-computed rollup views — hourly, daily, monthly, and yearly. TimescaleDB refreshes only the buckets that have changed since the last refresh, rather than recomputing the entire history. These aggregates are themselves hypertables, so they compress and partition the same way.

### Why not InfluxDB, ClickHouse, or a plain time-series store?

Two features make TimescaleDB the right choice here specifically:

**PostGIS.** Each weather station has GPS coordinates stored as a `GEOMETRY(Point, 4326)` column. PostGIS enables queries like "find all stations within 50 km of Münster" or "aggregate PM2.5 readings by country polygon" using standard SQL. InfluxDB and most purpose-built TSDB systems have no equivalent geospatial capability.

**Standard SQL.** The data model has real relational structure: stations have sensors, sensors have readings, sensors have units and types. Joining across these with GROUP BY, window functions, CTEs, and subqueries is natural in SQL. TimescaleDB extends PostgreSQL rather than replacing it, so every SQL tool, ORM, and analytics library that speaks PostgreSQL works here without modification.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker Compose                         │
│                                                             │
│  ┌─────────────┐    ┌───────────────────────────────────┐  │
│  │   scraper   │───▶│          TimescaleDB (db)          │  │
│  │  port: n/a  │    │  PostgreSQL 16 + TimescaleDB       │  │
│  └──────┬──────┘    │  + PostGIS  —  port 5432           │  │
│         │           └───────────────┬───────────────────┘  │
│         │ progress.json             │ SQL queries           │
│         ▼                           ▼                       │
│  ┌──────────────┐   ┌───────────────────────────────────┐  │
│  │ scraper_data │   │   monitor        map_viewer        │  │
│  │   (volume)   │◀──│   port 5051      port 5052         │  │
│  └──────────────┘   └───────────────────────────────────┘  │
│                                                             │
│                     ┌─────────────┐                        │
│                     │   pgAdmin   │                        │
│                     │  port 5050  │                        │
│                     └─────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

Five services, one shared image. The scraper, monitor, and map viewer are all built from the same Dockerfile — the `command:` override in `docker-compose.yml` selects which Python script runs in each container.

---

## Web Interfaces

| Service | URL | Purpose |
|---|---|---|
| **Progress Monitor** | http://localhost:5051 | Live scraper KPIs, progress bars for dates/stations/sensors, top sensor types and stations |
| **Station Map** | http://localhost:5052 | Interactive Leaflet map of all ingested stations with per-sensor stats on click |
| **pgAdmin** | http://localhost:5050 | Full SQL database UI — pre-configured, no setup required |

---

## Schema

The schema is in `schema.sql` and is applied automatically when the database container first starts. It will never run twice — PostgreSQL's `docker-entrypoint-initdb.d` mechanism only executes these scripts when the data directory is empty.

### Domain type: `osm_public_id`

```sql
CREATE DOMAIN osm_public_id AS VARCHAR(24)
    CHECK (VALUE ~ '^[0-9a-f]{24}$');
```

OpenSenseMap uses 24-character lowercase hex strings as IDs for both stations and sensors. Rather than scattering the regex constraint across every table definition and letting bad data silently pass as a plain `VARCHAR(24)`, the schema enforces the format at the type level. Any insert with a malformed ID fails immediately with a clear constraint violation. This is a deliberate data quality decision, not boilerplate.

### Table: `stations`

```sql
CREATE TABLE stations (
    station_id  osm_public_id  PRIMARY KEY,
    name        TEXT,
    box_type    TEXT,           -- e.g. "fixed", "mobile"
    exposure    TEXT,           -- e.g. "outdoor", "indoor"
    geometry    GEOMETRY(Point, 4326),
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_stations_geometry ON stations USING GIST (geometry);
```

One row per physical weather station. The `geometry` column stores the station's GPS position using the WGS 84 coordinate system (EPSG:4326 — the same system used by GPS and OpenStreetMap). The GiST index on `geometry` makes bounding-box and radius queries fast, which is the whole point of storing coordinates as a spatial type rather than plain `(latitude, longitude)` floats.

`box_type` and `exposure` are stored as free-text rather than enums because OpenSenseMap does not enforce a controlled vocabulary — values like `"DIY"`, `"homestation"`, and `"mobile"` all appear in the wild.

### Table: `sensors`

```sql
CREATE TABLE sensors (
    sensor_id   osm_public_id  PRIMARY KEY,
    station_id  osm_public_id  NOT NULL REFERENCES stations(station_id) ON DELETE CASCADE,
    title       TEXT,           -- e.g. "Temperature"
    sensor_type TEXT,           -- e.g. "HDC1080", "SDS011"
    unit        TEXT,           -- e.g. "°C", "µg/m³"
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
```

Each station can have multiple sensors — temperature, humidity, PM2.5, and so on are all separate sensor rows pointing at the same station. The `ON DELETE CASCADE` means deleting a station automatically removes all its sensors (and through the same cascade on `readings`, all its historical data). This is intentional: sensor data is only meaningful in context of the station it belongs to.

### Table: `readings` (hypertable)

```sql
CREATE TABLE readings (
    recorded_at TIMESTAMPTZ   NOT NULL,
    sensor_id   osm_public_id NOT NULL REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    value       DOUBLE PRECISION
);

SELECT create_hypertable('readings', 'recorded_at',
    chunk_time_interval => INTERVAL '1 week');
```

This is the core of the dataset. `recorded_at` is always stored as UTC (`TIMESTAMPTZ` normalises any timezone offset on insert). There is no surrogate primary key — readings are identified by `(sensor_id, recorded_at)`. The absence of a serial primary key is deliberate: it avoids a sequence bottleneck on bulk inserts and removes a column that carries no domain meaning.

The composite index `(sensor_id, recorded_at DESC)` reflects the primary access pattern: "give me sensor X's readings in reverse chronological order." The `DESC` ordering matches how data is typically consumed (most recent first) and aligns with the chunk compression ordering, allowing compressed scans to short-circuit early.

**Weekly chunks** are the right granularity here. Daily chunks would create too many small objects in the PostgreSQL catalog. Monthly chunks would make recent-data writes touch larger indexes. One week is a proven default for datasets with this kind of continuous append-only workload.

### Compression policy

```sql
ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby   = 'recorded_at DESC'
);

SELECT add_compression_policy('readings', compress_after => INTERVAL '1 day');
```

Chunks older than one day are compressed automatically. The `compress_segmentby = 'sensor_id'` setting groups all readings from the same sensor together within a compressed chunk — this maximises the compression ratio (sensor readings are highly repetitive within a single sensor) and makes per-sensor queries faster to decompress, since the database can skip entire sensor groups that don't match the query.

### Continuous aggregates

Four materialised rollup views chain from fine to coarse:

```
readings  (raw, weekly chunks)
    └── sensor_data_hourly    (1-hour buckets)
            └── sensor_data_daily     (1-day buckets, built on hourly)
                    └── sensor_data_monthly   (1-month buckets, built on daily)
                            └── sensor_data_yearly    (1-year buckets, built on monthly)
```

Each view stores `avg_value`, `min_value`, `max_value`, and `reading_count` per `(sensor_id, bucket)`. Chaining finer aggregates into coarser ones means the daily rollup does not re-scan the raw `readings` table — it reads from `sensor_data_hourly`, which is smaller by orders of magnitude.

The refresh policies are intentionally conservative — each level refreshes with a `start_offset` of 3× its interval to handle late-arriving data from the scraper. The `end_offset` keeps the trailing edge slightly in the past to avoid refreshing buckets that are still being written.

Each continuous aggregate is itself a hypertable, so it compresses, partitions, and indexes identically to `readings`.

### Scraper bookkeeping: `_scraper_processed_dates`

```sql
CREATE TABLE _scraper_processed_dates (
    date_str     DATE        PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

A single-column audit table. When the scraper finishes committing all stations for an archive date, it inserts a row here. On resume, the scraper checks this table before re-downloading anything. This is the second line of defence against data duplication — the first being the `NOT EXISTS` guard in the merge query. Even if `progress.json` is deleted or corrupted, this table ensures already-processed dates are always skipped.

---

## Crash Safety

The scraper is designed to be interrupted at any point — including hard kills — without leaving partial or duplicate data.

| Layer | Mechanism |
|---|---|
| HTTP progress | `progress.json` written atomically via `.tmp` rename — a crash mid-write leaves the old file intact |
| DB bookkeeping | `_scraper_processed_dates` — one row per fully-committed archive date |
| Per-station atomicity | Readings are staged in a `TEMP TABLE … ON COMMIT DELETE ROWS`, then merged into `readings` in a single `INSERT … WHERE NOT EXISTS`, committed once per station |
| SIGTERM / SIGINT | Caught by the scraper — it finishes the current station, commits, then exits cleanly |
| Duplicate guard | The `NOT EXISTS` subquery prevents re-inserting a reading that already exists, even on a full re-run of a date |

The worst case is a `SIGKILL` or OOM mid-station: the temp table vanishes, the commit never happens, and the date is not marked as processed. The next run re-downloads and re-inserts that station — and because of the `NOT EXISTS` guard, no data is duplicated.

---

## Quick Start

### Prerequisites

- Docker Engine 24+ and Docker Compose v2
- ~20 GB free disk space to start (the full archive is several terabytes — the scraper runs continuously and you can stop it whenever)

### 1. Configure credentials

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder passwords. The database password and the pgAdmin password should be different. Generate strong ones:

```bash
openssl rand -base64 32
```

### 2. Start the database, monitor, and map

```bash
docker compose up -d db monitor map_viewer pgadmin
```

This starts everything except the scraper. The database schema is applied automatically on first boot. Give it about 10–15 seconds for the database to initialise, then check:

```bash
docker compose ps
```

All four services should show `healthy` or `running`. Open:

- **Progress monitor** → http://localhost:5051
- **Station map** → http://localhost:5052
- **pgAdmin** → http://localhost:5050

### 3. Start the scraper

```bash
docker compose up -d scraper
```

The scraper begins at the oldest available archive date and works forward. Watch it in real time:

```bash
docker compose logs -f scraper
```

Or keep the **Progress Monitor** open at http://localhost:5051. It polls every 5 seconds and shows exactly where the scraper is, including which date is being processed, how many stations and sensors have been committed, and live row counts from the database.

### 4. Stop the scraper safely

```bash
docker compose stop scraper
```

This sends `SIGTERM`. The scraper finishes its current station, commits the data, writes the checkpoint, then exits cleanly. You will see a `Stop signal received` message in the logs. Allow up to 2 minutes (the configured grace period) if it is mid-download on a large station.

> Do **not** use `docker compose kill` unless absolutely necessary — that sends `SIGKILL` and bypasses the clean shutdown.

### 5. Resume scraping

```bash
docker compose start scraper
```

The scraper reads `progress.json` and the `_scraper_processed_dates` table, determines the last fully-committed date, and continues from there. Nothing is re-downloaded or re-inserted.

### 6. Rebuild the image after code changes

If you edit `scraper.py`, `monitor.py`, or `map_viewer.py`:

```bash
docker compose build
docker compose up -d
```

---

## Progress Monitor — port 5051

The monitor is a lightweight Flask app that reads `progress.json` from the shared `scraper_data` volume (read-only mount — it never writes) and queries the database for live counts.

**What it shows:**

- A status indicator — pulsing green when running, amber when stopped, blue when complete, red on error
- KPI cards: dates done/skipped/total, stations seen vs committed, sensors processed, readings inserted, live DB row counts
- Progress bars for dates, stations, and sensors
- The scraper's most recent status message
- A table of the 30 most common sensor types across all stations
- A table of the 100 stations with the most readings

The status and KPI section refreshes every 5 seconds. The tables refresh every 30 seconds.

---

## Station Map — port 5052

An interactive Leaflet map served by a Flask app. On load it fetches a GeoJSON FeatureCollection of up to 5,000 stations (those with valid coordinates) from the database, renders them as circle markers, and fits the viewport to show all of them.

**Marker colours** indicate reading volume:

| Colour | Readings |
|---|---|
| Blue | < 100 |
| Green | 100 – 1,000 |
| Amber | 1,000 – 10,000 |
| Red | > 10,000 |

**Click any marker** to open a side panel showing the station's name, ID, box type, and exposure, plus a card for every sensor with its min, average, max, and total reading count.

**Filters** in the header let you narrow by `exposure` (outdoor / indoor / mobile) and `box_type` (fixed / mobile). Filtering updates the map immediately without reloading the page.

---

## Querying the Data

Connect to the database from pgAdmin (http://localhost:5050) or any PostgreSQL client using the credentials from your `.env`:

```
Host:     localhost
Port:     5432  (or DB_PORT if you changed it)
Database: opensensemap  (or DB_NAME)
User:     osm  (or DB_USER)
```

Some useful queries to get started:

```sql
-- How many readings do we have per sensor type?
SELECT s.title, s.unit, COUNT(*) AS reading_count
FROM readings r
JOIN sensors s ON s.sensor_id = r.sensor_id
GROUP BY s.title, s.unit
ORDER BY reading_count DESC
LIMIT 20;

-- Daily average temperature for a specific sensor (use sensor_data_daily — much faster)
SELECT bucket, avg_value, min_value, max_value
FROM sensor_data_daily
WHERE sensor_id = '<your-sensor-id>'
ORDER BY bucket DESC
LIMIT 30;

-- All stations within 20 km of Münster, Germany
SELECT station_id, name,
       ROUND((ST_Distance(
           geometry::geography,
           ST_MakePoint(7.6261, 51.9607)::geography
       ) / 1000)::numeric, 1) AS km
FROM stations
WHERE ST_DWithin(geometry::geography,
                 ST_MakePoint(7.6261, 51.9607)::geography,
                 20000)
ORDER BY km;

-- How much disk space is the readings hypertable using?
SELECT pg_size_pretty(hypertable_size('readings'));

-- Compression savings on readings
SELECT
    pg_size_pretty(before_compression_total_bytes) AS before,
    pg_size_pretty(after_compression_total_bytes)  AS after
FROM hypertable_compression_stats('readings');
```

---

## Environment Variables

Copy `.env.example` to `.env` and set your own values before starting.

| Variable | Default | Description |
|---|---|---|
| `DB_HOST` | `db` | Database hostname (use `db` inside Docker) |
| `DB_PORT` | `5432` | Database port exposed on the host |
| `DB_NAME` | `opensensemap` | Database name |
| `DB_USER` | `osm` | Database username |
| `DB_PASSWORD` | *(set this)* | Database password — use 32+ random characters |
| `PGADMIN_EMAIL` | *(set this)* | pgAdmin login email |
| `PGADMIN_PASSWORD` | *(set this)* | pgAdmin login password |
| `PGADMIN_PORT` | `5050` | Host port for pgAdmin |
| `MONITOR_PORT` | `5051` | Host port for the progress monitor |
| `MAP_PORT` | `5052` | Host port for the station map |
| `DELAY_MIN` | `0.1` | Minimum per-request delay in seconds |
| `DELAY_MAX` | `0.4` | Maximum per-request delay in seconds |
| `DELAY_PER_DATE` | `0.5` | Extra pause between archive dates |
| `DELAY_429_BASE` | `60` | Base backoff in seconds after a 429 response |
| `MAX_429_RETRIES` | `5` | Maximum retries on a 429 before giving up on a date |

---

## Directory Layout

```
.
├── schema.sql              DB schema — applied once on first container start
├── scraper.py              Archive downloader and DB writer
├── monitor.py              Progress monitor web app  (port 5051)
├── map_viewer.py           Interactive station map   (port 5052)
├── Dockerfile              Single image for scraper + both dashboards
├── docker-compose.yml      Full service definitions
├── pgadmin_servers.json    Pre-configured pgAdmin server connection
├── pgadmin_init.sh         Entrypoint script — writes pgpass from env vars
├── requirements.txt        Python dependencies
├── .env.example            Template — copy to .env and fill in credentials
└── .gitignore
```
