import io
import csv
import json
import zipfile
import hashlib
import requests
import logging
from pathlib import Path
from datetime import date, datetime
from typing import Literal, List
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, FileResponse

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(".downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

OutputType = Literal["csv", "geojson", "json"]


def _safe_filename(url: str) -> str:
    """Derive a stable filename from a URL using a hash."""
    name = url.split("/")[-1].split("?")[0]
    if not name.endswith(".csv"):
        name = hashlib.md5(url.encode()).hexdigest() + ".csv"
    return name


def _ensure_downloaded(csv_url: str) -> Path:
    dest = DOWNLOAD_DIR / _safe_filename(csv_url)
    if dest.exists():
        logger.debug("Cache hit: %s", dest)
        return dest

    logger.info("Downloading %s → %s", csv_url, dest)
    try:
        resp = requests.get(csv_url, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to download {csv_url}: {exc}"
        )
    return dest


def _build_csv_output(url_rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)

    header_written = False
    index_header = [
        "st_id", "name", "exposure", "model",
        "latitude", "longitude", "country", "region",
        "se_id", "title", "type", "category", "unit",
    ]

    for row in url_rows:
        csv_url = row[14]
        local_path = _ensure_downloaded(csv_url)

        with open(local_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            try:
                data_header = next(reader)
            except StopIteration:
                continue                         

            if not header_written:
                data_header = ["time" if h == "createdAt" else h for h in data_header]
                writer.writerow(index_header + data_header)
                header_written = True

            meta = list(row[:13])   # exclude date (index 13)
            for data_row in reader:
                writer.writerow(meta + data_row)

    return buf.getvalue().encode("utf-8")


def _build_geojson_output(url_rows) -> bytes:
    """
    Build a GeoJSON FeatureCollection where each feature is one sensor reading row.
    """
    features = []
    for row in url_rows:
        (st_id, name, exposure, model, lat, lon,
         country, region, se_id, title, s_type, category, unit,
         row_date, csv_url) = row

        local_path = _ensure_downloaded(csv_url)

        with open(local_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for data_row in reader:
                data_row = {("time" if k == "createdAt" else k): v for k, v in data_row.items()}
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(lon) if lon else 0,
                                        float(lat) if lat else 0]
                    },
                    "properties": {
                        "st_id": st_id, "name": name, "exposure": exposure,
                        "model": model, "country": country, "region": region,
                        "se_id": se_id, "title": title, "type": s_type,
                        "category": category, "unit": unit,
                        **data_row
                    }
                })

    fc = {"type": "FeatureCollection", "features": features}
    return json.dumps(fc, default=str).encode("utf-8")


def _build_json_output(url_rows) -> bytes:
    """
    Build a JSON array of objects, one per row of sensor data.
    """
    records = []
    for row in url_rows:
        (st_id, name, exposure, model, lat, lon,
         country, region, se_id, title, s_type, category, unit,
         row_date, csv_url) = row

        local_path = _ensure_downloaded(csv_url)

        with open(local_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for data_row in reader:
                data_row = {("time" if k == "createdAt" else k): v for k, v in data_row.items()}
                records.append({
                    "st_id": st_id, "name": name, "exposure": exposure,
                    "model": model, "latitude": float(lat) if lat else None,
                    "longitude": float(lon) if lon else None,
                    "country": country, "region": region,
                    "se_id": se_id, "title": title, "type": s_type,
                    "category": category, "unit": unit,
                    **data_row
                })

    return json.dumps(records, default=str).encode("utf-8")


# Public API

MEDIA_TYPES = {
    "csv":     "text/csv",
    "geojson": "application/geo+json",
    "json":    "application/json",
}

EXTENSIONS = {
    "csv":     ".csv",
    "geojson": ".geojson",
    "json":    ".json",
}


def build_output_file(
    url_rows,
    output_types: List[OutputType],
    base_filename: str,
) -> StreamingResponse:

    if not output_types:
        raise HTTPException(status_code=400, detail="At least one output type is required.")

    # single type: stream the file directly
    if len(output_types) == 1:
        otype = output_types[0]
        builders = {
            "csv":     _build_csv_output,
            "geojson": _build_geojson_output,
            "json":    _build_json_output,
        }
        data = builders[otype](url_rows)
        filename = base_filename + EXTENSIONS[otype]
        return StreamingResponse(
            io.BytesIO(data),
            media_type=MEDIA_TYPES[otype],
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    # multiple types: zip them together
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if "csv" in output_types:
            zf.writestr(base_filename + ".csv",     _build_csv_output(url_rows))
        if "geojson" in output_types:
            zf.writestr(base_filename + ".geojson", _build_geojson_output(url_rows))
        if "json" in output_types:
            zf.writestr(base_filename + ".json",    _build_json_output(url_rows))

        # always include helper scripts if present
        downloader_dir = Path("downloader")
        for helper in ["osem_downloader.py", "README.md", "requirements.txt"]:
            fp = downloader_dir / helper
            if fp.exists():
                zf.write(fp, arcname=helper)

    zip_buf.seek(0)
    zip_filename = base_filename + ".zip"
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
    )