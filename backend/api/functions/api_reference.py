API_REFERENCE_TEXT = """\
openSenseMap Archive API
Interactive docs: /docs   |   OpenAPI schema: /openapi.json

Legend: [req] required, [opt=x] optional with default x

Response caching: GET responses are cached in Redis for 24h (X-Cache: HIT or
MISS on every response). /exports and its sub-routes are never cached, since
job status has to stay live.


GET    /
  This overview.


GET    /stats
  Archive-wide totals: stations, sensors, readings, countries.

  Example
    GET /stats
    200 OK
    {"stations": 2276, "sensors": 10870, "readings": 1294479319, "countries": 23}


GET    /countries
  All countries mapped to their regions.

  Example
    GET /countries
    200 OK
    {"Austria": ["Burgenland", "Wien", "..."], "Belgium": ["..."], "..."}


GET    /countries/{country}
  Regions in one country.

  Params
    country       path        [req]           country name, e.g. "Austria"

  Example
    GET /countries/Austria
    200 OK
    {"Austria": ["Burgenland", "Karnten", "Niederosterreich", "Oberosterreich",
                 "Salzburg", "Steiermark", "Tirol", "Vorarlberg", "Wien"]}

  Example -- unknown country is not an error, just an empty result
    GET /countries/Narnia
    200 OK
    {}


GET    /exposures
  Distinct exposure values in use.

  Example
    GET /exposures
    200 OK
    {"exposures": ["indoor", "mobile", "outdoor"]}


GET    /phenomena
  Distinct sensor phenomenon (title) values in use, capitalized for display.

  Example
    GET /phenomena
    200 OK
    {"phenomena": ["Air Temperature", "Humidity", "PM10", "..."]}


GET    /tags
  Distinct sensor type values in use, capitalized for display.

  Example
    GET /tags
    200 OK
    {"tags": ["BME280", "DHT22", "SDS 011", "..."]}


GET    /boxes
  All boxes with their sensors and last measurements.

  Example
    GET /boxes
    200 OK
    [{"_id": "538da4d6a834155415765eae", "name": "Ctronix",
      "sensors": [{"_id": "...", "title": "Helligkeit", "lastMeasurement": {"value": 0.0, "updatedAt": "..."}}],
      "exposure": "outdoor", "currentLocation": {"coordinates": [16.53, 47.84], "type": "Point"}}, "..."]


GET    /boxes/{box_id}
  One box by id.

  Params
    box_id        path        [req]           24-character hex box id (Mongo ObjectId)

  Example
    GET /boxes/538da4d6a834155415765eae
    200 OK
    {"_id": "538da4d6a834155415765eae", "name": "Ctronix", "sensors": ["..."]}

  Errors
    404   {"detail": "Box '000000000000000000000000' not found"}
          box_id is well-formed (24 hex chars) but no box has that id
    404   {"detail": "Not Found"}
          box_id is not 24 hex characters -- rejected by the router before this handler runs


POST   /boxes/aoi
  Boxes located inside an uploaded area-of-interest file.

  Params
    from_date     query       [req]           date, YYYY-MM-DD
    to_date       query       [req]           date, YYYY-MM-DD
    file          form-data   [req]           .zip, .kml, or .geojson
    exposure      form-data   [opt=all]       indoor, mobile, or outdoor
    tags          form-data   [opt=all]       sensor type, e.g. "SDS 011"
    phenomenon    form-data   [opt=all]       sensor title, e.g. "PM10"

  Example
    curl -X POST "/boxes/aoi?from_date=2024-01-01&to_date=2024-01-31" -F "file=@area.geojson"
    200 OK
    {"boxes": ["..."], "geometry": {"type": "Polygon", "coordinates": ["..."]},
     "response_time_ms": 84.21}

  Errors
    400   {"detail": "Unsupported AOI file type '.txt'. Use .zip, .kml, or .geojson."}
    400   {"detail": "to_date must be on or after from_date."}
    400   {"detail": "Invalid GeoJSON file: ..."}
          the AOI file itself is malformed or unreadable (also for .zip/.kml)
    422   {"detail": [{"loc": ["query", "from_date"], "msg": "Value error, Date must be in YYYY-MM-DD format."}]}
    422   {"detail": [{"loc": ["form", "exposure"], "msg": "Input should be 'all', 'indoor', 'mobile' or 'outdoor'"}]}
          same shape for tags/phenomenon, against the actual DB values


GET    /boxes/region
  Boxes located inside a named region.

  Params
    region        query       [req]           region name, e.g. "Burgenland"
    from_date     query       [req]           date, YYYY-MM-DD
    to_date       query       [req]           date, YYYY-MM-DD
    exposure      query       [opt=all]       indoor, mobile, or outdoor
    tags          query       [opt=all]       sensor type
    phenomenon    query       [opt=all]       sensor title

  Example
    GET /boxes/region?region=Burgenland&from_date=2017-05-01&to_date=2017-05-05
    200 OK
    {"region": "Burgenland", "createdAt": "2026-07-03T00:34:23+00:00", "response_time_ms": 117.41,
     "geometry": {"type": "Polygon", "coordinates": ["..."]}, "boxes": ["..."]}

  Errors
    404   {"detail": "Region 'Narnia' not found"}
    400   {"detail": "to_date must be on or after from_date."}
    422   bad date format, or exposure/tags/phenomenon not a recognized value (same shape as /boxes/aoi)


POST   /exports
  Starts an async export job (Celery + Redis). Give region OR an AOI file, never both.

  Params
    from_date     query       [req]                date, YYYY-MM-DD
    to_date       query       [req]                date, YYYY-MM-DD
    region        query       [req unless file]    region name
    file          form-data   [req unless region]  .zip, .kml, or .geojson
    aggregate     query       [opt=hourly]          raw, hourly, daily, monthly, or yearly
    format        query       [opt=geojson]         csv or geojson
    exposure      query       [opt=all]             indoor, mobile, or outdoor
    tags          query       [opt=all]             sensor type
    phenomenon    query       [opt=all]             sensor title

  Example -- by region
    POST /exports?region=Burgenland&from_date=2017-05-01&to_date=2017-05-31&aggregate=raw&format=csv
    202 Accepted
    {"job_id": "5da51b94-9b18-446f-825c-dce28f7bddcf", "status": "pending", "task_id": "..."}

  Example -- by AOI file
    curl -X POST "/exports?from_date=2024-01-01&to_date=2024-01-31&aggregate=daily" -F "file=@area.geojson"
    202 Accepted
    {"job_id": "...", "status": "pending", "task_id": "..."}

  Notes
    - aggregate=raw or hourly -> result is a .zip with one file per calendar month.
    - aggregate=daily/monthly/yearly -> result is a single .csv or .geojson file.
    - Download filename is "{region-or-uploaded-filename}_{timestamp}.{zip|csv|geojson}".
    - Every row/feature includes the box's country and region.
    - Timestamps are split into a "date" (YYYY-MM-DD) and a 24-hour "time" (HH:MM:SS) field.
    - Aggregated tiers (daily/monthly/yearly) add avg_value, min_value, max_value, rdgs_count.
    - The finished file and its job record are auto-deleted 24h after completion (see expires_at below).

  Errors
    400   {"detail": "Provide either region or an AOI file, not both."}
    400   {"detail": "Either region or an AOI file must be provided."}
    400   {"detail": "Unsupported AOI file type '.txt'. Use .zip, .kml, or .geojson."}
    400   {"detail": "to_date must be on or after from_date."}
    404   {"detail": "Region 'Narnia' not found"}
    422   bad date format, or aggregate/format/exposure/tags/phenomenon not a recognized value


GET    /exports
  Lists all export jobs, most recent first.

  Example
    GET /exports
    200 OK
    [{"id": "c889fb69-7655-4b5f-892b-f08a5a2a2a4b", "format": "csv", "aggregate": "raw",
      "status": "done", "row_count": 1284, "file_url": "/app/jobs/c889fb69-....zip",
      "error_message": null, "created_at": "...", "completed_at": "...",
      "expires_at": "2026-07-05T00:23:57+02:00"}, "..."]


GET    /exports/{job_id}
  One export job's status and metadata.

  Params
    job_id        path        [req]           UUID

  Example
    GET /exports/c889fb69-7655-4b5f-892b-f08a5a2a2a4b
    200 OK
    {"id": "c889fb69-...", "format": "csv", "aggregate": "raw", "status": "done",
     "row_count": 1284, "file_url": "/app/jobs/c889fb69-....zip", "error_message": null,
     "created_at": "2026-07-04T00:23:57+02:00", "completed_at": "2026-07-04T00:23:57+02:00",
     "expires_at": "2026-07-05T00:23:57+02:00"}

  Errors
    422   {"detail": [{"loc": ["path", "job_id"], "msg": "Input should be a valid UUID, invalid character..."}]}
    404   {"detail": "Export job '00000000-0000-0000-0000-000000000000' not found"}


GET    /exports/{job_id}/download
  Downloads a completed export job's file. Response headers include X-Expires-At
  (ISO 8601 timestamp) and X-Timezone (its UTC offset, e.g. "+02:00") -- the file
  and job record are both auto-deleted at that moment, 24h after completion.

  Params
    job_id        path        [req]           UUID

  Example
    GET /exports/c889fb69-7655-4b5f-892b-f08a5a2a2a4b/download
    200 OK
    Content-Type: application/zip
    Content-Disposition: attachment; filename="Burgenland_20260704002357.zip"
    X-Expires-At: 2026-07-05T00:23:57.265775+02:00
    X-Timezone: +02:00

  Errors
    422   job_id is not a valid UUID (same shape as /exports/{job_id})
    404   {"detail": "Export job '...' not found"}
    409   {"detail": "Export job is 'pending', not ready yet"}
          also returned for 'running' and 'failed'
    410   {"detail": "Export file is missing or has expired"}
          the job record says "done" but the file itself is gone from disk


DELETE /exports/{job_id}
  Deletes an export job and its file.

  Params
    job_id        path        [req]           UUID

  Example
    DELETE /exports/c889fb69-7655-4b5f-892b-f08a5a2a2a4b
    204 No Content

  Errors
    422   job_id is not a valid UUID (same shape as /exports/{job_id})
    404   {"detail": "Export job '...' not found"}
"""
