import io
import csv
import json
import zipfile
import logging
from pathlib import Path
from datetime import date, datetime
from typing import Literal, List
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

OutputType = Literal["csv", "geojson", "json"]
AggregateType = Literal["raw", "day", "month", "year"]

_CSV_HEADER = [
    "st_id", "exposure", "model",
    "latitude", "longitude", "country", "region",
    "se_id", "title", "type", "unit",
    "time", "value",
]


def _row_to_dict(row) -> dict:
    return {
        "st_id": row[0],
        "exposure": row[1],
        "model": row[2],
        "latitude": row[3],
        "longitude": row[4],
        "country": row[5],
        "region": row[6],
        "se_id": row[7],
        "title": row[8],
        "type": row[9],
        "unit": row[10],
        "time": str(row[11]) if row[11] is not None else None,
        "value": row[12],
    }


def _build_csv_output(rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADER)
    for row in rows:
        writer.writerow([
            row[0], row[1], row[2],
            row[3], row[4], row[5], row[6],
            row[7], row[8], row[9], row[10],
            str(row[11]) if row[11] is not None else "",
            row[12],
        ])
    return buf.getvalue().encode("utf-8")


def _build_geojson_output(rows) -> bytes:
    features = []
    for row in rows:
        d = _row_to_dict(row)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    float(d["longitude"]) if d["longitude"] is not None else 0,
                    float(d["latitude"]) if d["latitude"]  is not None else 0,
                ],
            },
            "properties": {k: v for k, v in d.items()
                           if k not in ("latitude", "longitude")},
        })
    fc = {"type": "FeatureCollection", "features": features}
    return json.dumps(fc, default=str).encode("utf-8")


def _build_json_output(rows) -> bytes:
    records = [_row_to_dict(row) for row in rows]
    return json.dumps(records, default=str).encode("utf-8")


MEDIA_TYPES = {
    "csv": "text/csv",
    "geojson": "application/geo+json",
    "json": "application/json",
}

EXTENSIONS = {
    "csv": ".csv",
    "geojson": ".geojson",
    "json": ".json",
}

_BUILDERS = {
    "csv":     _build_csv_output,
    "geojson": _build_geojson_output,
    "json":    _build_json_output,
}


def build_output_file(
    url_rows,
    output_types: List[OutputType],
    base_filename: str,
) -> StreamingResponse:

    if not output_types:
        raise HTTPException(status_code=400, detail="At least one output type is required.")

    rows = list(url_rows)

    if len(output_types) == 1:
        otype = output_types[0]
        data = _BUILDERS[otype](rows)
        filename = base_filename + EXTENSIONS[otype]
        return StreamingResponse(
            io.BytesIO(data),
            media_type=MEDIA_TYPES[otype],
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for otype in output_types:
            zf.writestr(base_filename + EXTENSIONS[otype], _BUILDERS[otype](rows))

        downloader_dir = Path("downloader")
        for helper in ["osem_downloader.py", "README.md", "requirements.txt"]:
            fp = downloader_dir / helper
            if fp.exists():
                zf.write(fp, arcname=helper)

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={base_filename}.zip"},
    )