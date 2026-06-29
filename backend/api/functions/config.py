import os

from dotenv import load_dotenv


# Load backend/.env once before shared clients are created.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST")
DB_PORT = os.getenv("POSTGRES_PORT")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

EXPORT_DIR = os.getenv("EXPORT_DIR", "/mnt/exports")
EXPORT_JOB_CACHE_TTL = int(os.getenv("EXPORT_JOB_CACHE_TTL_SECONDS", "86400"))

os.makedirs(EXPORT_DIR, exist_ok=True)

