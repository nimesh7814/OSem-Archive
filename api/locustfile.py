"""
Locust load test for OpenSenseMap Index API
============================================
Covers ALL endpoints (download=True is intentionally excluded):

  GET /
  GET /summary
  GET /countries
  GET /regions?country=<c>
  GET /sensor_categories
  GET /get_stations                                  (paginated map markers)
  GET /get_stations?station_id=<id>                 (single station detail)
  GET /station_readings?st_id=<id>&month=<m>&year=<y>
  GET /country_region_data                           (no filters)
  GET /country_region_data?country=<c>&category=<cat>
  GET /country_region_data?country=<c>&region=<r>
  GET /country_region_data?country=<c1>,<c2>&from_date=<d>&to_date=<d>
  GET /bbox_data?aoi=<geojson>                       (no extra filters)
  GET /bbox_data?aoi=<geojson>&category=<cat>
  GET /bbox_data?aoi=<geojson>&from_date=<d>&to_date=<d>

Usage
-----
1. Install:
       pip install locust

2. Web UI (interactive):
       locust -f locustfile.py --host http://localhost:8000
   Open http://localhost:8089 → set users / spawn-rate / run-time.

3. Headless (CI):
       locust -f locustfile.py \
         --host http://localhost:8000 \
         --headless \
         --users 200 \
         --spawn-rate 20 \
         --run-time 10m \
         --html report.html \
         --csv results

4. Distributed (high-volume):
   Master:  locust -f locustfile.py --master --host http://localhost:8000
   Workers: locust -f locustfile.py --worker --master-host <master-ip>

Station IDs
-----------
  Place st_id.csv (single column "st_id") in the same folder as this file.
  All 15,540 real IDs will be loaded automatically at startup.
  Fallback: set STATION_IDS env-var as a comma-separated list.

Notes on download=True exclusion
---------------------------------
  All tasks below use download=False (the default) or omit the parameter
  entirely.  The download path streams large cursor-based result sets and
  is intentionally excluded from load testing to avoid saturating disk I/O
  and network bandwidth during the test run.
"""

import csv
import json
import os
import random
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from locust import HttpUser, TaskSet, between, task, events

# ---------------------------------------------------------------------------
# Load real station IDs from st_id.csv (must be in the same folder as this
# file).  Falls back to the STATION_IDS env-var if the CSV is not found.
# ---------------------------------------------------------------------------
def _load_station_ids() -> list[str]:
    csv_path = Path(__file__).parent / "st_id.csv"
    if csv_path.exists():
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            ids = [row["st_id"].strip() for row in reader if row.get("st_id", "").strip()]
        if ids:
            print(f"[locust] Loaded {len(ids):,} station IDs from {csv_path}")
            return ids
    # Fallback: env-var or placeholder
    fallback = os.environ.get(
        "STATION_IDS",
        "station-001,station-002,station-003,station-004,station-005",
    ).split(",")
    print(f"[locust] st_id.csv not found — using {len(fallback)} fallback IDs")
    return fallback

STATION_IDS: list[str] = _load_station_ids()

COUNTRIES   = ["DE", "FR", "US", "GB", "NL", "ES", "IT"]
REGIONS     = ["Bayern", "Hessen", "Berlin", "NRW", "Hamburg"]
CATEGORIES  = ["temperature", "humidity", "pressure", "PM2.5", "PM10"]
YEARS       = [2023, 2024, 2025]
MONTHS      = list(range(1, 13))

# Pagination variants for /get_stations
PAGINATION_VARIANTS = [
    {"limit": 5000, "offset": 0},
    {"limit": 1000, "offset": 0},
    {"limit": 1000, "offset": 1000},
    {"limit": 500,  "offset": 5000},
]

# Multiple bounding boxes so spatial queries aren't always identical
BBOXES = [
    # Central Europe
    {"type": "Polygon", "coordinates": [[[6.0,47.0],[15.0,47.0],[15.0,55.0],[6.0,55.0],[6.0,47.0]]]},
    # Western Europe
    {"type": "Polygon", "coordinates": [[[-5.0,42.0],[10.0,42.0],[10.0,52.0],[-5.0,52.0],[-5.0,42.0]]]},
    # UK + Ireland
    {"type": "Polygon", "coordinates": [[[-10.5,49.5],[2.0,49.5],[2.0,61.0],[-10.5,61.0],[-10.5,49.5]]]},
    # Scandinavia
    {"type": "Polygon", "coordinates": [[[4.0,54.0],[32.0,54.0],[32.0,71.0],[4.0,71.0],[4.0,54.0]]]},
]
ENCODED_BBOXES = [quote(json.dumps(b)) for b in BBOXES]


def random_date_range(max_days_back: int = 365 * 3):
    """Return a (from_date, to_date) pair as ISO strings."""
    today = date.today()
    start_offset = random.randint(30, max_days_back)
    end_offset   = random.randint(0, start_offset - 1)
    from_d = (today - timedelta(days=start_offset)).isoformat()
    to_d   = (today - timedelta(days=end_offset)).isoformat()
    return from_d, to_d


# ---------------------------------------------------------------------------
# TaskSet 1: Lightweight — cached / simple SELECT endpoints
# ---------------------------------------------------------------------------
class LightweightEndpoints(TaskSet):
    """
    Fast endpoints that hit the TTL cache most of the time.
    Given high task weights so the test generates lots of traffic volume.
    """

    @task(6)
    def health_check(self):
        """GET /  — health probe, always instant."""
        self.client.get("/", name="GET /")

    @task(4)
    def summary(self):
        """GET /summary — one-row table read, 60 s cache."""
        self.client.get("/summary", name="GET /summary")

    @task(4)
    def countries(self):
        """GET /countries — distinct query, 5 min cache."""
        self.client.get("/countries", name="GET /countries")

    @task(4)
    def sensor_categories(self):
        """GET /sensor_categories — distinct query, 5 min cache."""
        self.client.get("/sensor_categories", name="GET /sensor_categories")

    @task(3)
    def regions(self):
        """GET /regions — varies by country, 5 min cache per country."""
        country = random.choice(COUNTRIES)
        self.client.get(f"/regions?country={country}", name="GET /regions")

    @task(5)
    def get_stations_default(self):
        """GET /get_stations — default page (limit=5000 offset=0)."""
        self.client.get("/get_stations", name="GET /get_stations (default page)")

    @task(3)
    def get_stations_paginated(self):
        """GET /get_stations with varied limit/offset — tests cache keying."""
        p = random.choice(PAGINATION_VARIANTS)
        self.client.get(
            f"/get_stations?limit={p['limit']}&offset={p['offset']}",
            name="GET /get_stations (paginated)",
        )

    @task(3)
    def get_stations_single(self):
        """GET /get_stations?station_id=<id> — single station detail."""
        st_id = random.choice(STATION_IDS)
        self.client.get(
            f"/get_stations?station_id={st_id}",
            name="GET /get_stations?station_id=<id>",
        )


# ---------------------------------------------------------------------------
# TaskSet 2: Heavy — multi-join aggregate queries, no or short caching
# ---------------------------------------------------------------------------
class HeavyEndpoints(TaskSet):
    """
    Endpoints that run expensive SQL aggregations.
    Lower task weights keep the DB from being overwhelmed.
    """

    # --- /station_readings variants ---

    @task(4)
    def station_readings(self):
        """GET /station_readings — random station + month + year."""
        st_id = random.choice(STATION_IDS)
        month = random.choice(MONTHS)
        year  = random.choice(YEARS)
        self.client.get(
            f"/station_readings?st_id={st_id}&month={month}&year={year}",
            name="GET /station_readings",
        )

    # --- /country_region_data variants (download omitted / always False) ---

    @task(3)
    def country_region_no_filter(self):
        """GET /country_region_data — no filters, most expensive variant."""
        self.client.get(
            "/country_region_data",
            name="GET /country_region_data (no filter)",
        )

    @task(4)
    def country_region_by_country_and_category(self):
        """GET /country_region_data?country=<c>&category=<cat>"""
        country  = random.choice(COUNTRIES)
        category = random.choice(CATEGORIES)
        self.client.get(
            f"/country_region_data?country={country}&category={category}",
            name="GET /country_region_data (country+category)",
        )

    @task(3)
    def country_region_by_country_and_region(self):
        """GET /country_region_data?country=DE&region=<r>"""
        region = random.choice(REGIONS)
        self.client.get(
            f"/country_region_data?country=DE&region={region}",
            name="GET /country_region_data (country+region)",
        )

    @task(2)
    def country_region_multi_country(self):
        """GET /country_region_data?country=<c1>,<c2> — CSV multi-value filter."""
        countries = ",".join(random.sample(COUNTRIES, k=2))
        self.client.get(
            f"/country_region_data?country={countries}",
            name="GET /country_region_data (multi-country)",
        )

    @task(2)
    def country_region_with_dates(self):
        """GET /country_region_data with from_date + to_date range."""
        country = random.choice(COUNTRIES)
        from_d, to_d = random_date_range()
        self.client.get(
            f"/country_region_data?country={country}&from_date={from_d}&to_date={to_d}",
            name="GET /country_region_data (with dates)",
        )

    @task(2)
    def country_region_multi_category(self):
        """GET /country_region_data with multiple categories."""
        cats = ",".join(random.sample(CATEGORIES, k=2))
        self.client.get(
            f"/country_region_data?category={cats}",
            name="GET /country_region_data (multi-category)",
        )

    # --- /bbox_data variants (download omitted / always False) ---

    @task(2)
    def bbox_no_filter(self):
        """GET /bbox_data — spatial filter only, no category/date."""
        aoi = random.choice(ENCODED_BBOXES)
        self.client.get(
            f"/bbox_data?aoi={aoi}",
            name="GET /bbox_data (no filter)",
        )

    @task(2)
    def bbox_with_category(self):
        """GET /bbox_data?aoi=<geojson>&category=<cat>"""
        aoi      = random.choice(ENCODED_BBOXES)
        category = random.choice(CATEGORIES)
        self.client.get(
            f"/bbox_data?aoi={aoi}&category={category}",
            name="GET /bbox_data (with category)",
        )

    @task(1)
    def bbox_with_dates(self):
        """GET /bbox_data with from_date + to_date — hits date-filtered subqueries."""
        aoi = random.choice(ENCODED_BBOXES)
        from_d, to_d = random_date_range(max_days_back=365)
        self.client.get(
            f"/bbox_data?aoi={aoi}&from_date={from_d}&to_date={to_d}",
            name="GET /bbox_data (with dates)",
        )

    @task(1)
    def bbox_with_category_and_dates(self):
        """GET /bbox_data — all filters combined, most expensive spatial query."""
        aoi      = random.choice(ENCODED_BBOXES)
        category = random.choice(CATEGORIES)
        from_d, to_d = random_date_range(max_days_back=365)
        self.client.get(
            f"/bbox_data?aoi={aoi}&category={category}&from_date={from_d}&to_date={to_d}",
            name="GET /bbox_data (category+dates)",
        )


# ---------------------------------------------------------------------------
# User classes
# ---------------------------------------------------------------------------

class BrowserUser(HttpUser):
    """
    Simulates a human using the dashboard.
    70% lightweight (browsing map/lists), 30% heavy (charts/filters).
    Think time: 1–3 s (realistic pacing).
    """
    tasks     = {LightweightEndpoints: 7, HeavyEndpoints: 3}
    wait_time = between(1, 3)
    weight    = 4  # 4× more browser users than API users


class ApiUser(HttpUser):
    """
    Simulates a programmatic client (data pipeline, frontend app).
    50/50 lightweight vs heavy, minimal think time.
    """
    tasks     = {LightweightEndpoints: 5, HeavyEndpoints: 5}
    wait_time = between(0.05, 0.3)
    weight    = 1


# ---------------------------------------------------------------------------
# End-of-run summary
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    stats = environment.runner.stats.total
    fail_pct = (stats.num_failures / stats.num_requests * 100) if stats.num_requests else 0
    print(
        f"\n{'='*40}"
        f"\n  Load test complete"
        f"\n{'='*40}"
        f"\n  Total requests : {stats.num_requests:>10,}"
        f"\n  Failures       : {stats.num_failures:>10,}  ({fail_pct:.1f}%)"
        f"\n  Avg latency    : {stats.avg_response_time:>10.1f} ms"
        f"\n  Median latency : {stats.median_response_time:>10.1f} ms"
        f"\n  95th pct       : {stats.get_response_time_percentile(0.95):>10.1f} ms"
        f"\n  Peak RPS       : {stats.current_rps:>10.1f}"
        f"\n{'='*40}\n"
    )
