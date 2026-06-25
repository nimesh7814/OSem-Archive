from fastapi import HTTPException

from .boxes_export import _respond, query_boxes_aggregated


async def list_boxes_by_region(
    country: str | None,
    region: str | None,
    exposure: str | None,
    phenomenon: str | None,
    sensor_type: str | None,
    from_date: str | None,
    to_date: str | None,
    download: bool,
    file_type: str,
    aggregate: str,
):
    if not country and not region:
        raise HTTPException(400, "Provide at least one of 'country' or 'region'")
    if to_date and not from_date:
        raise HTTPException(400, "from_date is required when to_date is provided")
    if download and file_type not in ("geojson", "csv"):
        raise HTTPException(400, "file_type must be 'geojson' or 'csv'")
    if aggregate not in ("raw", "date", "month", "year"):
        raise HTTPException(400, "aggregate must be one of: raw, date, month, year")

    rows = await query_boxes_aggregated(
        geometry_wkt=None,
        country=country,
        region=region,
        exposure=exposure,
        phenomenon=phenomenon,
        sensor_type=sensor_type,
        from_date=from_date,
        to_date=to_date,
        aggregate=aggregate,
    )
    return _respond(rows, download, file_type, "boxes_region")
