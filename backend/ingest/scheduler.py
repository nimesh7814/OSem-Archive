import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def schedule_time():
    raw_value = os.getenv("INGEST_SCHEDULE_TIME", "00:00")
    try:
        hour_text, minute_text = raw_value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError("INGEST_SCHEDULE_TIME must use HH:MM format.") from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("INGEST_SCHEDULE_TIME must be a valid 24-hour time.")
    return hour, minute


def next_run_at(now):
    hour, minute = schedule_time()
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return run_at


def run_ingest():
    app_dir = Path(__file__).resolve().parents[1]
    command = [sys.executable, "ingest/load_archive.py"]
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Starting scheduled ingest.", flush=True)
    result = subprocess.run(command, cwd=app_dir)
    if result.returncode == 0:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Scheduled ingest completed.", flush=True)
    else:
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] Scheduled ingest failed with exit code {result.returncode}.",
            flush=True,
        )
    return result.returncode


def main():
    timezone = ZoneInfo(os.getenv("INGEST_SCHEDULE_TIMEZONE", "Europe/Berlin"))
    if env_bool("INGEST_SCHEDULER_RUN_ON_START", False):
        run_ingest()

    while True:
        now = datetime.now(timezone)
        run_at = next_run_at(now)
        sleep_seconds = max(1, (run_at - now).total_seconds())
        print(
            f"[{now.isoformat(timespec='seconds')}] Next ingest run at {run_at.isoformat(timespec='seconds')}.",
            flush=True,
        )
        time.sleep(sleep_seconds)
        run_ingest()


if __name__ == "__main__":
    main()
