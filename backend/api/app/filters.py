from typing import Annotated

from fastapi import HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

try:
    from .cache import get_cached, set_cached
    from .db import run_query
    from .queries import TAGS_QUERY, PHENOMENA_QUERY, EXPOSURE_QUERY
    from .schema import CommonMeasurementFilters, MeasurementAggregate, Tag, Phenomenon, Exposure
except ImportError:
    from cache import get_cached, set_cached
    from db import run_query
    from queries import TAGS_QUERY, PHENOMENA_QUERY, EXPOSURE_QUERY
    from schema import CommonMeasurementFilters, MeasurementAggregate, Tag, Phenomenon, Exposure

TAGS_CACHE_KEY = "tags:details"
PHENOMENA_CACHE_KEY = "phenomena:details"
EXPOSURE_CACHE_KEY = "exposure:details"

FILTER_LABELS = {
    "tags": "tag",
    "phenomena": "phenomenon",
    "exposure": "exposure",
}


def get_cached_list(cache_key, query, schema, field_name):
    cached_result = get_cached(cache_key)
    if cached_result is not None:
        return cached_result, "cache"

    results = run_query(query, schema=schema)
    values = [getattr(row, field_name) for row in results]
    set_cached(cache_key, values)
    return values, "database"


def get_tags():
    return get_cached_list(TAGS_CACHE_KEY, TAGS_QUERY, Tag, "sensor_type")


def get_phenomena():
    return get_cached_list(PHENOMENA_CACHE_KEY, PHENOMENA_QUERY, Phenomenon, "title")


def get_exposure():
    return get_cached_list(EXPOSURE_CACHE_KEY, EXPOSURE_QUERY, Exposure, "exposure")


def normalize_filter(values):
    # None means "no filter": empty input, or an explicit tags=all, disables filtering on this field.
    if not values or "all" in values:
        return None

    normalized = []
    for value in values:
        normalized.extend(part.strip() for part in value.split(",") if part.strip())

    if not normalized or "all" in normalized:
        return None
    return normalized


def validate_known_filter_values(filter_name, requested_values, available_values):
    if requested_values is None:
        return

    filter_label = FILTER_LABELS.get(filter_name, filter_name)
    available_values_set = set(available_values)
    invalid_values = list(dict.fromkeys(
        value
        for value in requested_values
        if value not in available_values_set
    ))

    if not invalid_values:
        return

    raise HTTPException(
        status_code=400,
        detail={
            "message": f"Unknown {filter_label} filter value.",
            "invalidValues": invalid_values,
            "hint": f"Use GET /{filter_name} to see the accepted values, or pass {filter_name}=all to disable this filter.",
        },
    )


def build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate):
    try:
        return CommonMeasurementFilters(**{
            "from": from_date,
            "to": to_date,
            "tags": tags,
            "phenomena": phenomena,
            "exposure": exposure,
            "aggregate": aggregate,
        })
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def measurement_filter_params(
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    tags: Annotated[list[str] | None, Query()] = None,
    phenomena: Annotated[list[str] | None, Query()] = None,
    exposure: Annotated[list[str] | None, Query()] = None,
    aggregate: Annotated[MeasurementAggregate, Query()] = "daily",
):
    return build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate)


def daily_measurement_filter_params(
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    tags: Annotated[list[str] | None, Query()] = None,
    phenomena: Annotated[list[str] | None, Query()] = None,
    exposure: Annotated[list[str] | None, Query()] = None,
):
    # No aggregate parameter on purpose: this endpoint always returns daily data, not user-selectable.
    return build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, "daily")


def validate_measurement_filters(filters):
    tag_filter = normalize_filter(filters.tags)
    phenomenon_filter = normalize_filter(filters.phenomena)
    exposure_filter = normalize_filter(filters.exposure)

    if tag_filter is not None:
        tag_values, _ = get_tags()
        validate_known_filter_values("tags", tag_filter, tag_values)

    if phenomenon_filter is not None:
        phenomenon_values, _ = get_phenomena()
        validate_known_filter_values("phenomena", phenomenon_filter, phenomenon_values)

    if exposure_filter is not None:
        exposure_values, _ = get_exposure()
        validate_known_filter_values("exposure", exposure_filter, exposure_values)

    return tag_filter, phenomenon_filter, exposure_filter


def serialize_measurement_filters(filters):
    return {
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "tags": filters.tags,
        "phenomena": filters.phenomena,
        "exposure": filters.exposure,
        "aggregate": filters.aggregate,
    }
