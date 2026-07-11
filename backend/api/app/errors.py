from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    from .aoi import AoiValidationError
    from .bucket import BucketError
    from .cache import CacheUnavailableError
    from .celery_app import ExportQueueError
    from .db import DatabaseBusyError, DatabaseQueryError, DatabaseConnectionLostError, DatabaseInputError
except ImportError:
    from aoi import AoiValidationError
    from bucket import BucketError
    from cache import CacheUnavailableError
    from celery_app import ExportQueueError
    from db import DatabaseBusyError, DatabaseQueryError, DatabaseConnectionLostError, DatabaseInputError

AVAILABLE_ENDPOINTS = [
    "GET /stats",
    "GET /boxes",
    "GET /countries",
    "GET /tags",
    "GET /phenomena",
    "GET /exposure",
    "POST /aoi/validate",
    "POST /aoi/measurements",
    "POST /aoi/measurements/exports",
    "GET /regions/{country}/{region}/measurements",
    "POST /regions/{country}/{region}/measurements/exports?format=csv&aggregate=daily",
    "POST /regions/{country}/{region}/measurements/exports?format=geojson&aggregate=daily",
    "GET /exports/{job_id}",
    "GET /exports/{job_id}/download",
]


def error_response(status_code: int, message: str, code: int | None = None, details=None, headers=None):
    error = {
        "code": code or status_code,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def format_validation_errors(errors):
    formatted_errors = []
    for error in errors:
        location = [
            str(part)
            for part in error.get("loc", [])
            if part not in ("query", "path", "body")
        ]
        message = error.get("msg", "Invalid value.")
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        formatted_errors.append({
            "field": ".".join(location) if location else "request",
            "message": message,
            "type": error.get("type", "value_error"),
        })
    return formatted_errors


def register_exception_handlers(app):
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        details = None

        # Starlette's own routing 404 (no matching path), as opposed to a 404 an endpoint raised deliberately.
        if exc.status_code == 404 and detail == "Not Found":
            message = f"No endpoint exists for {request.method} {request.url.path}."
            details = {
                "hint": "Use GET /countries to find available countries and regions, or GET /regions/{country}/{region}/measurements for measurements.",
                "availableEndpoints": AVAILABLE_ENDPOINTS,
            }
        elif isinstance(detail, dict):
            message = detail.get("message", "The request could not be completed.")
            details = {key: value for key, value in detail.items() if key != "message"}
            if not details:
                details = None
        else:
            message = str(detail)

        return error_response(exc.status_code, message, details=details, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        return error_response(
            status_code=422,
            message="One or more request parameters are invalid.",
            details=format_validation_errors(exc.errors()),
        )

    @app.exception_handler(DatabaseBusyError)
    async def database_busy_handler(request: Request, exc: DatabaseBusyError):
        return error_response(status_code=503, message="The database is busy. Please try again shortly.")

    @app.exception_handler(DatabaseConnectionLostError)
    async def database_connection_lost_handler(request: Request, exc: DatabaseConnectionLostError):
        return error_response(status_code=503, message="The database connection was interrupted. Please try again.")

    @app.exception_handler(DatabaseQueryError)
    async def database_query_error_handler(request: Request, exc: DatabaseQueryError):
        return error_response(status_code=500, message="The server could not complete the database query.")

    @app.exception_handler(DatabaseInputError)
    async def database_input_error_handler(request: Request, exc: DatabaseInputError):
        return error_response(status_code=400, message="One or more request values contain characters that are not allowed.")

    @app.exception_handler(BucketError)
    async def bucket_error_handler(request: Request, exc: BucketError):
        return error_response(status_code=503, message=str(exc))

    @app.exception_handler(CacheUnavailableError)
    async def cache_unavailable_error_handler(request: Request, exc: CacheUnavailableError):
        return error_response(status_code=503, message=str(exc))

    @app.exception_handler(ExportQueueError)
    async def export_queue_error_handler(request: Request, exc: ExportQueueError):
        return error_response(status_code=503, message=str(exc))

    @app.exception_handler(AoiValidationError)
    async def aoi_validation_error_handler(request: Request, exc: AoiValidationError):
        return error_response(status_code=400, message=str(exc))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return error_response(status_code=500, message="An unexpected server error occurred.")
