# Supports being run both as "api.main:app" (repo root on the path) and "app.app:app" (backend/api on the path).
try:
    from api.app.logger import configure_logging
except ModuleNotFoundError as exc:
    if exc.name != "api":
        raise
    from app.logger import configure_logging

configure_logging()

try:
    from api.app.app import app
except ModuleNotFoundError as exc:
    if exc.name != "api":
        raise
    from app.app import app

__all__ = ["app"]
