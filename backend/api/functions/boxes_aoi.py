from fastapi import UploadFile

from .upload_aoi import load_aoi_geometry
from .boxes import BoxQueryParams, query_boxes_aggregated, respond


async def boxes_by_aoi(
    file: UploadFile | None,
    geometry: str | None,
    filters: BoxQueryParams,
):
    geom = await load_aoi_geometry(file, geometry)
    aggregate = filters.aggregate if filters.download else "monthly"

    rows = await query_boxes_aggregated(
        geometry_wkt=geom.wkt,
        country=None,
        region=None,
        exposure=filters.exposure,
        phenomenon=filters.phenomenon,
        sensor_type=filters.sensor_type,
        from_date=filters.from_date,
        to_date=filters.to_date,
        aggregate=aggregate,
    )
    return respond(rows, filters.download, filters.file_type, "boxes_aoi")
