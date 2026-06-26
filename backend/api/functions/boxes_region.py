from fastapi import HTTPException

from .boxes import BoxQueryParams, query_boxes_for_aggregate, respond


async def list_boxes_by_region(
    country: str | None,
    region: str | None,
    filters: BoxQueryParams,
):
    if not country and not region:
        raise HTTPException(400, "Provide at least one of 'country' or 'region'")

    aggregate = filters.aggregate if filters.download else "monthly"

    rows = await query_boxes_for_aggregate(
        aggregate=aggregate,
        geometry_wkt=None,
        country=country,
        region=region,
        exposure=filters.exposure,
        phenomenon=filters.phenomenon,
        sensor_type=filters.sensor_type,
        from_date=filters.from_date,
        to_date=filters.to_date,
    )
    return respond(rows, filters.download, filters.file_type, "boxes_region")
