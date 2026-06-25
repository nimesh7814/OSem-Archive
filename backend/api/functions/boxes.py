from .boxes_export import query_boxes_aggregated


async def list_boxes(box_id: str | None) -> list[dict]:
    return await query_boxes_aggregated(
        geometry_wkt=None,
        box_id=box_id,
        aggregate="raw",
    )
