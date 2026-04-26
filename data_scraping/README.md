# OpenSenseMap → TimescaleDB Scraper

Downloads the full [OpenSenseMap archive](https://archive.opensensemap.org/) and
stores it in a local TimescaleDB instance with PostGIS. Includes a live progress
monitor and an interactive station map, both served from the same Docker image.

## Directory layout

```
.
├── schema.sql            # Full DB schema — applied automatically on first start
├── scraper.py            # Archive scraper
├── monitor.py            # Progress monitor web app  (port 5051)
├── map_viewer.py         # Station map web app        (port 5052)
├── Dockerfile            # Shared image for scraper + dashboards
├── docker-compose.yml    # DB + Scraper + Monitor + Map + pgAdmin
├── pgadmin_servers.json  # pgAdmin pre-configured server entry
├── pgadmin_pgpass        # pgAdmin passwordless connection (update with your password)
├── requirements.txt
├── .env.example          # Copy to .env and fill in credentials
└── .gitignore
```

## Web interfaces

| Service | URL | Description |
|---|---|---|
| **Progress Monitor** | http://localhost:5051 | Live scraper KPIs, progress bars, sensor + station tables |
| **Station Map** | http://localhost:5052 | Leaflet map of all ingested stations; click for sensor stats |
| **pgAdmin** | http://localhost:5050 | Full database UI — pre-configured, no setup needed |

---

## Quick start

### 1. Configure credentials

```bash
cp .env.example .env
# Edit .env — replace the placeholder passwords with real ones.
# Also update the password line in pgadmin_pgpass to match DB_PASSWORD.
```

Generate strong passwords:
```bash
openssl rand -base64 32
```

### 2. Start the database, dashboards, and pgAdmin

```bash
docker compose up -d db monitor map_viewer pgadmin
```

The database schema (`schema.sql`) is applied automatically the first time the
`postgres_data` volume is created.

- **Progress monitor** → http://localhost:5051 (shows "no data yet" until the scraper runs)
- **Station map** → http://localhost:5052 (map is empty until stations are scraped)
- **pgAdmin** → http://localhost:5050 — the *OpenSenseMap DB* server is pre-configured.

### 3. Start the scraper

```bash
docker compose up -d scraper
```

Watch progress in the terminal:
```bash
docker compose logs -f scraper
```

Or open the **Progress Monitor** at http://localhost:5051 — it auto-refreshes
every 5 seconds and shows live KPIs, progress bars for dates/stations/sensors,
and tables of the most active sensor types and stations.

### 4. Stop the scraper safely

```bash
docker compose stop scraper
```

`docker compose stop` sends **SIGTERM**. The scraper catches it, finishes
writing the current station, commits, then exits cleanly. The next start will
resume from the last fully-committed archive date — no data will be lost or
duplicated.

### 5. Restart / resume

```bash
docker compose start scraper
```

The scraper checks both `progress.json` (in the `scraper_data` volume) **and**
the `_scraper_processed_dates` table in the database. Even if `progress.json`
is lost, the DB table ensures nothing is re-processed.

---

## Progress Monitor (port 5051)

The monitor reads `progress.json` from the shared `scraper_data` volume and
queries the database directly. It never writes to the volume.

**KPIs shown:**
- Dates done / skipped / total
- Stations seen vs. committed
- Sensors processed vs. total
- Readings inserted (valid / parsed)
- Live DB row counts (stations, sensors, readings)

**Tables:**
- Top 30 sensor types by count (title, type, unit)
- Top 100 stations by reading count (name, type, exposure, sensor count)

The status dot in the header reflects the scraper's current state:
`running` (green pulse) / `completed` (blue) / `stopped` (amber) / `error` (red).

---

## Station Map (port 5052)

An interactive Leaflet map backed by PostGIS geometry queries. Markers are
colour-coded by reading volume:

| Colour | Readings |
|---|---|
| Blue | < 100 |
| Green | 100 – 1,000 |
| Amber | 1,000 – 10,000 |
| Red | > 10,000 |

Click any marker to open a side panel showing all sensors for that station with
min / avg / max / reading count statistics.

**Filters** (header dropdowns): filter by `exposure` (outdoor / indoor / mobile)
and `box_type` (fixed / mobile) without reloading — updates the map in-place.

---

## How crash safety works

| Layer | Mechanism |
|---|---|
| HTTP progress | `progress.json` written atomically (`.tmp` → rename) |
| DB bookkeeping | `_scraper_processed_dates` — one row per fully-committed date |
| Per-station atomicity | Readings staged in a `TEMP` table (`ON COMMIT DELETE ROWS`); merged into `readings` in one `INSERT … WHERE NOT EXISTS`; committed once per station |
| SIGTERM / SIGINT | Caught → finish current station → commit → exit |
| Duplicate guard | `NOT EXISTS` check in the merge query prevents double-insertion on re-run |

If the process is killed mid-station (e.g. `kill -9` or OOM), that station's
temp table is discarded and the date is not marked as processed. The scraper
will re-download and re-insert that station on the next run without duplication.

---

## Schema overview

| Table / View | Purpose |
|---|---|
| `stations` | Weather station metadata + PostGIS geometry |
| `sensors` | Sensors belonging to a station |
| `readings` | Raw time-series (TimescaleDB hypertable, weekly chunks) |
| `sensor_data_hourly` | Continuous aggregate — hourly rollup |
| `sensor_data_daily` | Continuous aggregate — daily rollup |
| `sensor_data_monthly` | Continuous aggregate — monthly rollup |
| `sensor_data_yearly` | Continuous aggregate — yearly rollup |
| `_scraper_processed_dates` | Internal bookkeeping — not part of the domain schema |

Compression is enabled on `readings` chunks older than 1 day (~10-20× size reduction).

---

## Environment variables

See `.env.example` for the full list with descriptions.

| Variable | Default | Description |
|---|---|---|
| `DB_*` | — | Database connection (host, port, name, user, password) |
| `PGADMIN_*` | — | pgAdmin email, password, port |
| `MONITOR_PORT` | `5051` | Host port for the progress monitor |
| `MAP_PORT` | `5052` | Host port for the station map |
| `DELAY_MIN` / `MAX` | `0.1` / `0.4` | Per-request polite delay range (seconds) |
| `DELAY_PER_DATE` | `0.5` | Extra pause between archive dates |
| `DELAY_429_BASE` | `60` | Base backoff after a 429 response |
| `MAX_429_RETRIES` | `5` | Max retries on 429 before giving up |
