import logging
import os
import sys
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s:%(message)s:%(pathname)s:%(funcName)s:%(lineno)d"
LOG_FORMAT_DEBUG = "%(asctime)s:%(levelname)s:%(name)s:%(message)s:%(pathname)s:%(funcName)s:%(lineno)d"


class LogLevels(StrEnum):
    info = "INFO"
    warn = "WARN"
    warning = "WARNING"
    error = "ERROR"
    debug = "DEBUG"


def _coerce_level(log_level: str) -> int:
    normalized = str(log_level).upper()
    if normalized == LogLevels.warn:
        normalized = LogLevels.warning
    return getattr(logging, normalized, logging.ERROR)


def configure_logging(log_level: str | None = None, log_file: str | None = None) -> None:
    level_name = log_level or os.getenv("LOG_LEVEL", LogLevels.info)
    level = _coerce_level(level_name)
    formatter = logging.Formatter(LOG_FORMAT_DEBUG if level == logging.DEBUG else LOG_FORMAT)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file_path = Path(log_file or os.getenv("LOG_FILE", "/app/logs/api.log"))
    try:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file_path,
                maxBytes=int(os.getenv("LOG_MAX_BYTES", "10485760")),
                backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
                encoding="utf-8",
            )
        )
    except OSError as exc:
        logging.basicConfig(level=level)
        logging.getLogger(__name__).warning("Could not configure file logging at %s: %s", log_file_path, exc)
        return

    for handler in handlers:
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)
