"""
Locust load test for OpenSenseMap Index API
============================================
Covers all endpoints:
  GET /
  GET /summary
  GET /countries
  GET /regions
  GET /sensor_categories
  GET /get_stations
  GET /get_stations?station_id=<id>
  GET /station_readings
  GET /country_region_data
  GET /bbox_data

Usage
-----
1. Install:
       pip install locust

2. Run the web UI (recommended for 1M-call simulation):
       locust -f locustfile.py --host http://localhost:8000

   Then open http://localhost:8089 and set:
     - Number of users  : e.g. 500
     - Spawn rate       : e.g. 50/s
     - Run time         : e.g. 30m

3. Headless (CI / scripted):
       locust -f locustfile.py \
         --host http://localhost:8000 \
         --headless \
         --users 500 \
         --spawn-rate 50 \
         --run-time 30m \
         --html report.html \
         --csv results

4. Distributed (to push toward 1 M total requests):
   Master:
       locust -f locustfile.py --master --host http://localhost:8000
   Workers (run on as many machines/cores as you like):
       locust -f locustfile.py --worker --master-host <master-ip>

   With 10 workers × 500 users each you can easily generate
   millions of requests in a short window.

Environment variables
---------------------
  TARGET_HOST  – overrides --host if set  (default: http://localhost:8000)
  STATION_IDS  – comma-separated list of real station IDs to use in tests
                 (default: uses the placeholder list below)

Tips for 1 M calls
------------------
- Keep --run-time long enough; at 5 000 req/s that is ~200 s.
- Use --html + --csv to get a full report after the run.
- Watch Postgres connection pool: each Locust user holds 0 persistent
  connections (connections are opened/closed per request in your API),
  but 500 concurrent users can spike to ~500 simultaneous PG connections.
  Consider pgBouncer in front of Postgres for realistic scale tests.
"""

import json
import os
import random
from urllib.parse import quote

from locust import HttpUser, TaskSet, between, task, events

# ---------------------------------------------------------------------------
# Configurable test data — replace with IDs that actually exist in your DB
# ---------------------------------------------------------------------------
STATION_IDS: list[str] = os.environ.get(
    "STATION_IDS",
    "station-001,station-002,station-003,station-004,station-005",
).split(",")

COUNTRIES = ["DE", "FR", "US", "GB", "NL", "ES", "IT"]
REGIONS   = ["Bayern", "Hessen", "Berlin", "NRW", "Hamburg"]
CATEGORIES = ["temperature", "humidity", "pressure", "PM2.5", "PM10"]
YEARS  = [2023, 2024, 2025]
MONTHS = list(range(1, 13))

# A small bounding box over central Europe (used for /bbox_data)
SAMPLE_BBOX_POLYGON = {
    "type": "Polygon",
    "coordinates": [[
        [6.0, 47.0],
        [15.0, 47.0],
        [15.0, 55.0],
        [6.0, 55.0],
        [6.0, 47.0],
    ]]
}
ENCODED_BBOX = quote(json.dumps(SAMPLE_BBOX_POLYGON))


# ---------------------------------------------------------------------------
# Task sets — group by endpoint "weight" so heavier endpoints are called less
# ---------------------------------------------------------------------------

class LightweightEndpoints(TaskSet):
    """Fast, read-only endpoints with no heavy DB aggregations."""

    @task(5)
    def status(self):
        self.client.get("/", name="GET /")

    @task(3)
    def countries(self):
        self.client.get("/countries", name="GET /countries")

    @task(3)
    def sensor_categories(self):
        self.client.get("/sensor_categories", name="GET /sensor_categories")

    @task(2)
    def regions(self):
        country = random.choice(COUNTRIES)
        self.client.get(f"/regions?country={country}", name="GET /regions")

    @task(4)
    def get_stations_all(self):
        """Map-marker list — no station_id filter."""
        self.client.get("/get_stations", name="GET /get_stations (all)")

    @task(2)
    def get_stations_single(self):
        """Detailed station view — with station_id."""
        st_id = random.choice(STATION_IDS)
        self.client.get(
            f"/get_stations?station_id={st_id}",
            name="GET /get_stations?station_id=<id>",
        )


class HeavyEndpoints(TaskSet):
    """Endpoints that trigger multi-join aggregate queries."""

    @task(2)
    def summary(self):
        self.client.get("/summary", name="GET /summary")

    @task(3)
    def station_readings(self):
        st_id = random.choice(STATION_IDS)
        month = random.choice(MONTHS)
        year  = random.choice(YEARS)
        self.client.get(
            f"/station_readings?st_id={st_id}&month={month}&year={year}",
            name="GET /station_readings",
        )

    @task(2)
    def country_region_data(self):
        country  = random.choice(COUNTRIES)
        category = random.choice(CATEGORIES)
        self.client.get(
            f"/country_region_data?country={country}&category={category}",
            name="GET /country_region_data",
        )

    @task(1)
    def country_region_data_all(self):
        """No filters — most expensive variant."""
        self.client.get("/country_region_data", name="GET /country_region_data (all)")

    @task(1)
    def bbox_data(self):
        self.client.get(
            f"/bbox_data?aoi={ENCODED_BBOX}",
            name="GET /bbox_data",
        )


# ---------------------------------------------------------------------------
# User classes — different think-time profiles
# ---------------------------------------------------------------------------

class BrowserUser(HttpUser):
    """
    Simulates a human navigating the dashboard.
    Calls lightweight endpoints most of the time.
    wait_time: 1–3 s between requests (realistic browser pacing).
    """
    tasks = {LightweightEndpoints: 7, HeavyEndpoints: 3}
    wait_time = between(1, 3)
    weight = 3   # 3× more browser users than API users


class ApiUser(HttpUser):
    """
    Simulates a programmatic API client (no think time).
    Hammers all endpoints at full speed.
    """
    tasks = {LightweightEndpoints: 5, HeavyEndpoints: 5}
    wait_time = between(0.05, 0.2)   # 50–200 ms between calls
    weight = 1


# ---------------------------------------------------------------------------
# Optional: print a summary line to stdout at the end of a test run
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    stats = environment.runner.stats.total
    print(
        f"\n=== Load test complete ==="
        f"\n  Requests : {stats.num_requests:,}"
        f"\n  Failures : {stats.num_failures:,}"
        f"\n  Avg (ms) : {stats.avg_response_time:.1f}"
        f"\n  RPS      : {stats.current_rps:.1f}"
        f"\n=========================="
    )
