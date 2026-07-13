import csv
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import redis
import shapefile
from fastapi.testclient import TestClient

TEST_ROOT = Path(__file__).resolve().parents[1]
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from app import app as app_module
from app import aoi as aoi_module
from app import bucket as bucket_module
from app import cache as cache_module
from app import db as db_module
from app import export as export_module
from app import measurements as measurements_module

client = TestClient(app_module.app)


def _geojson_polygon_bytes(crs=None, geometry_type="Polygon"):
    payload = {
        "type": "Feature",
        "geometry": {
            "type": geometry_type,
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]] if geometry_type == "Polygon" else [0, 0],
        },
        "properties": {"name": "sample"},
    }
    if crs is not None:
        payload["crs"] = crs
    return json.dumps(payload).encode("utf-8")


def _geojson_point_bytes():
    return json.dumps({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {"name": "sample"},
    }).encode("utf-8")


def _invalid_geojson_bytes():
    return json.dumps({"foo": "bar"}).encode("utf-8")


def _corrupted_kml_bytes():
    return b"<kml><Document><Placemark><Polygon></Placemark></Document>"


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _valid_shapefile_zip_bytes():
    with tempfile.TemporaryDirectory() as temp_dir:
        base_path = Path(temp_dir) / "area"
        with shapefile.Writer(str(base_path), shapeType=shapefile.POLYGON) as writer:
            writer.field("name", "C")
            writer.record("demo")
            writer.poly([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]])

        base_path.with_suffix(".prj").write_text(
            'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
            encoding="utf-8",
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for suffix in (".shp", ".shx", ".dbf", ".prj"):
                archive.write(base_path.with_suffix(suffix), arcname=f"area{suffix}")
        return buffer.getvalue()


def _measurement_row():
    return {
        "aggregate": "daily",
        "box_id": 1,
        "name": "Station A",
        "exposure": "outdoor",
        "model": "custom",
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
        "last_measurement_at": datetime(2024, 1, 3, tzinfo=timezone.utc),
        "country": "Germany",
        "region": "Muenster",
        "longitude": 7.63,
        "latitude": 51.96,
        "sensor_id": 10,
        "box_id": 1,
        "sensor_type": "temperature",
        "title": "Air Temperature",
        "unit": "C",
        "bucket": datetime(2024, 1, 4, tzinfo=timezone.utc),
        "avg_value": 12.5,
        "min_value": 10.0,
        "max_value": 15.0,
        "rdgs_count": 4,
        "value": None,
    }


def _base_degraded_checks(status="unavailable"):
    return {
        "redis": {"status": "ok"},
        "minio": {"status": status},
        "database": {"status": "ok"},
        "celeryWorker": {"status": "ok"},
    }


class EndpointScenarioTests(TestCase):
    def setUp(self):
        self.degraded_patcher = patch("app.app.is_service_degraded", return_value=False)
        self.degraded_patcher.start()
        self.addCleanup(self.degraded_patcher.stop)

    def test_root_reports_degraded_state(self):
        with patch.object(app_module, "get_dependency_checks_cached", return_value=_base_degraded_checks()):
            response = client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "degraded, further information available at /health")

    def test_health_returns_dependency_details(self):
        with patch.object(app_module, "get_dependency_checks_cached", return_value=_base_degraded_checks()):
            response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "degraded")
        self.assertIn("dependencies", body)
        self.assertEqual(body["dependencies"]["minio"]["status"], "unavailable")

    def test_stats_returns_summary(self):
        summary = app_module.Summary(stations=1, sensors=2, readings=3, countries=4, updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        with patch.object(app_module, "get_cached", return_value=None), \
             patch.object(app_module, "set_cached"), \
             patch.object(app_module, "run_query", return_value=[summary]):
            response = client.get("/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"][0]["stations"], 1)
        self.assertEqual(response.json()["source"], "database")

    def test_stats_reports_database_busy(self):
        with patch.object(app_module, "get_cached", return_value=None), \
             patch.object(app_module, "run_query", side_effect=db_module.DatabaseBusyError("busy")):
            response = client.get("/stats")
        self.assertEqual(response.status_code, 503)
        self.assertIn("database is busy", response.json()["error"]["message"].lower())

    def test_countries_groups_regions(self):
        rows = [
            {"country": "Germany", "region": "Muenster"},
            {"country": "Germany", "region": "Steinfurt"},
            {"country": "Netherlands", "region": "Twente"},
        ]
        with patch.object(app_module, "get_cached", return_value=None), \
             patch.object(app_module, "set_cached"), \
             patch.object(app_module, "run_query", return_value=rows):
            response = client.get("/countries")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["countries"][0]["country"], "Germany")
        self.assertEqual(response.json()["countries"][0]["regions"], ["Muenster", "Steinfurt"])

    def test_country_returns_404_when_unknown(self):
        with patch.object(app_module, "get_cached", return_value=None), \
             patch.object(app_module, "run_query", return_value=[]):
            response = client.get("/countries/Nowhere")
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["error"]["message"].lower())

    def test_tags_phenomena_and_exposure_endpoints(self):
        with patch.object(app_module.filters, "get_tags", return_value=(["temperature", "humidity"], "database")), \
             patch.object(app_module.filters, "get_phenomena", return_value=(["Air Temperature"], "database")), \
             patch.object(app_module.filters, "get_exposure", return_value=(["outdoor"], "database")):
            self.assertEqual(client.get("/tags").json()["tags"], ["temperature", "humidity"])
            self.assertEqual(client.get("/phenomena").json()["phenomena"], ["Air Temperature"])
            self.assertEqual(client.get("/exposure").json()["exposure"], ["outdoor"])

    def test_region_measurements_route_returns_payload(self):
        payload = {"input": "region", "aoi": {"country": "Germany", "region": "Muenster"}, "boxes": []}
        with patch.object(app_module.measurements, "get_region_measurements_response", return_value=payload):
            response = client.get("/regions/Germany/Muenster/measurements?from=2024-01-01&to=2024-01-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["input"], "region")

    def test_region_measurements_export_queues_job(self):
        task = SimpleNamespace(id="job-123")
        with patch.object(app_module.measurements, "get_validated_measurement_filters", return_value=(None, None, None)), \
             patch.object(app_module.export_jobs, "ensure_export_dependencies_available"), \
             patch.object(app_module, "send_export_task", return_value=task), \
             patch.object(app_module.export_jobs, "mark_export_job_submitted"):
            response = client.post("/regions/Germany/Muenster/measurements/exports?from=2024-01-01&to=2024-01-31&format=csv")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["jobId"], "job-123")
        self.assertEqual(response.json()["status"], "queued")

    def test_aoi_measurements_route_returns_payload(self):
        payload = {"input": "aoi", "aoi": {"name": "sample"}, "boxes": []}
        with patch.object(app_module.measurements, "get_aoi_measurements_response", return_value=payload):
            response = client.post(
                "/aoi/measurements",
                files={"file": ("area.geojson", _geojson_polygon_bytes(), "application/geo+json")},
                data={"from": "2024-01-01", "to": "2024-01-31"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["input"], "aoi")

    def test_aoi_measurements_export_queues_job(self):
        task = SimpleNamespace(id="job-456")
        with patch.object(app_module.export_jobs, "ensure_export_dependencies_available"), \
             patch.object(app_module, "send_export_task", return_value=task), \
             patch.object(app_module.export_jobs, "mark_export_job_submitted"):
            response = client.post(
                "/aoi/measurements/exports",
                files={"file": ("area.geojson", _geojson_polygon_bytes(), "application/geo+json")},
                data={"from": "2024-01-01", "to": "2024-01-31", "format": "csv"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["jobId"], "job-456")

    def test_export_job_status_and_download(self):
        export_result = {"objectName": "exports/job-1/file.csv", "filename": "file.csv"}
        with patch.object(app_module.export_jobs, "export_job_payload", return_value=(200, {"jobId": "job-1", "status": "completed"})):
            response = client.get("/exports/job-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")

        result = SimpleNamespace(state="SUCCESS", result=export_result)
        with patch.object(app_module, "get_export_result", return_value=result), \
             patch.object(app_module, "get_presigned_download_url", return_value="https://example.com/download"):
            download_response = client.get("/exports/job-1/download", follow_redirects=False)
        self.assertEqual(download_response.status_code, 307)
        self.assertEqual(download_response.headers["location"], "https://example.com/download")


class AoiValidationEndpointTests(TestCase):
    def setUp(self):
        self.degraded_patcher = patch("app.app.is_service_degraded", return_value=False)
        self.degraded_patcher.start()
        self.addCleanup(self.degraded_patcher.stop)

    def test_rejects_wrong_crs_geojson(self):
        geojson_payload = _geojson_polygon_bytes(crs={"type": "name", "properties": {"name": "EPSG:3857"}})
        response = client.post("/aoi/validate", files={"file": ("area.geojson", geojson_payload, "application/geo+json")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Coordinate System is wrong", response.json()["error"]["message"])

    def test_rejects_corrupted_kml(self):
        response = client.post("/aoi/validate", files={"file": ("area.kml", _corrupted_kml_bytes(), "application/vnd.google-earth.kml+xml")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("File is not valid", response.json()["error"]["message"])

    def test_rejects_zip_missing_shp(self):
        response = client.post("/aoi/validate", files={"file": ("area.zip", _zip_bytes([("readme.txt", b"no shapefile")]), "application/zip")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("File is not valid", response.json()["error"]["message"])

    def test_rejects_zip_missing_dbf(self):
        response = client.post(
            "/aoi/validate",
            files={
                "file": (
                    "area.zip",
                    _zip_bytes([
                        ("area.shp", b""),
                        ("area.shx", b""),
                        ("area.prj", b'GEOGCS["WGS 84"]'),
                    ]),
                    "application/zip",
                )
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("File is not valid", response.json()["error"]["message"])

    def test_rejects_invalid_geojson_structure(self):
        response = client.post("/aoi/validate", files={"file": ("area.geojson", _invalid_geojson_bytes(), "application/geo+json")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("File is not valid", response.json()["error"]["message"])

    def test_rejects_non_polygon_geometry(self):
        response = client.post("/aoi/validate", files={"file": ("area.geojson", _geojson_point_bytes(), "application/geo+json")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("No valid Geometry", response.json()["error"]["message"])

    def test_accepts_valid_polygon_zip(self):
        response = client.post("/aoi/validate", files={"file": ("area.zip", _valid_shapefile_zip_bytes(), "application/zip")})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "Feature")
        self.assertIn("geometry", body)
        self.assertIn("area_sqkm", body["properties"])


class ProcessingAndIntegrationTests(TestCase):
    def setUp(self):
        self.degraded_patcher = patch("app.app.is_service_degraded", return_value=False)
        self.degraded_patcher.start()
        self.addCleanup(self.degraded_patcher.stop)

    def test_region_measurement_response_builds_expected_payload(self):
        row = _measurement_row()
        filters = SimpleNamespace(
            from_date=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
            to_date=datetime(2024, 1, 31, tzinfo=timezone.utc).date(),
            exposure=None,
            tags=None,
            phenomena=None,
            aggregate="daily",
            model_dump=lambda mode="json": {"from": "2024-01-01", "to": "2024-01-31", "aggregate": "daily"},
            model_copy=lambda update=None: filters,
        )
        aoi = {"country": "Germany", "region": "Muenster", "geometry": {"type": "Polygon", "coordinates": []}, "area_sqkm": 1.0}
        with patch.object(measurements_module, "get_validated_measurement_filters", return_value=(None, None, None)), \
             patch.object(measurements_module, "get_cached_measurements", return_value=None), \
               patch.object(measurements_module, "set_cached_measurements"), \
             patch.object(measurements_module, "iter_region_measurement_rows", return_value=iter([row])), \
             patch.object(measurements_module, "get_region_aoi", return_value=aoi), \
             patch.object(measurements_module, "count_region_measurement_rows", return_value={"aggregate": "daily", "count": 1, "limit": 1000000, "exceedsLimit": False}):
            payload = measurements_module.get_region_measurements_response("Germany", "Muenster", filters, count_aggregate="daily")
        self.assertEqual(payload["source"], "database")
        self.assertEqual(payload["aoi"]["country"], "Germany")
        self.assertEqual(payload["recordCount"]["count"], 1)
        self.assertEqual(payload["boxes"][0]["sensors"][0]["measurements"][0]["avgValue"], 12.5)

    def test_aoi_measurement_response_builds_expected_payload(self):
        row = _measurement_row()
        filters = SimpleNamespace(
            from_date=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
            to_date=datetime(2024, 1, 31, tzinfo=timezone.utc).date(),
            exposure=None,
            tags=None,
            phenomena=None,
            aggregate="daily",
            model_copy=lambda update=None: filters,
            model_dump=lambda mode="json": {"from": "2024-01-01", "to": "2024-01-31", "aggregate": "daily"},
        )
        aoi = {"properties": {"input": "area.geojson", "area_sqkm": 2.5}, "geometry": {"type": "Polygon", "coordinates": []}}
        with patch.object(measurements_module, "validate_measurement_filters", return_value=(None, None, None)), \
             patch.object(measurements_module, "get_cached_measurements", return_value=None), \
               patch.object(measurements_module, "set_cached_measurements"), \
             patch.object(measurements_module, "iter_aoi_measurement_rows", return_value=iter([row])), \
             patch.object(measurements_module, "count_aoi_measurement_rows", return_value={"aggregate": "daily", "count": 1, "limit": 1000000, "exceedsLimit": False}):
            payload = measurements_module.get_aoi_measurements_response(aoi, filters, count_aggregate="daily")
        self.assertEqual(payload["source"], "database")
        self.assertEqual(payload["aoi"]["name"], "area.geojson")
        self.assertEqual(payload["boxes"][0]["sensors"][0]["measurements"][0]["readings"], 4)

    def test_csv_export_builder_generates_valid_csv(self):
        row = _measurement_row()
        csv_text = export_module.build_measurement_export([row], "csv")
        parsed = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["box_name"], "Station A")
        self.assertEqual(parsed[0]["readings"], "4")
        self.assertEqual(parsed[0]["avg_value"], "12.5")

    def test_geojson_export_builder_generates_valid_geojson(self):
        row = _measurement_row()
        geojson_text = export_module.build_measurement_export([row], "geojson")
        payload = json.loads(geojson_text)
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(payload["features"][0]["properties"]["sensors"][0]["measurements"][0]["readings"], 4)

    def test_presigned_url_uses_configured_ttl(self):
        captured = {}

        class FakeClient:
            def presigned_get_object(self, bucket, object_name, expires):
                captured["bucket"] = bucket
                captured["object_name"] = object_name
                captured["expires"] = expires
                return "https://example.com/download"

        with patch.object(bucket_module, "MINIO_TTL_SECONDS", 3600), \
             patch.object(bucket_module, "check_object_storage_connection"), \
             patch.object(bucket_module, "bucket_name", return_value="exports"), \
             patch.object(bucket_module, "minio_client", return_value=FakeClient()):
            url = bucket_module.get_presigned_download_url("files/export.csv")
        self.assertEqual(url, "https://example.com/download")
        self.assertEqual(captured["bucket"], "exports")
        self.assertEqual(captured["object_name"], "files/export.csv")
        self.assertEqual(captured["expires"], timedelta(seconds=3600))

    def test_redis_connection_failure_is_handled(self):
        with patch.object(cache_module.redis_client, "ping", side_effect=redis.RedisError("boom")):
            with self.assertRaises(cache_module.CacheUnavailableError):
                cache_module.check_cache_connection()

    def test_minio_connection_failure_is_handled(self):
        class FailingClient:
            def bucket_exists(self, *_args, **_kwargs):
                raise RuntimeError("down")

        with patch.object(bucket_module, "minio_client", return_value=FailingClient()), \
             patch.object(bucket_module, "bucket_name", return_value="exports"):
            with self.assertRaises(bucket_module.BucketError):
                bucket_module.check_object_storage_connection()

    def test_degraded_status_marker_is_added_to_json_responses(self):
        summary = app_module.Summary(stations=1, sensors=2, readings=3, countries=4, updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        with patch.object(app_module, "is_service_degraded", return_value=True), \
             patch.object(app_module, "get_cached", return_value=None), \
             patch.object(app_module, "set_cached"), \
             patch.object(app_module, "run_query", return_value=[summary]):
            response = client.get("/stats")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "degraded, further information available at /health"})
