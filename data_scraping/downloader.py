#!/usr/bin/env python3
"""
OpenSenseMap Full Archive Downloader
======================================
Downloads the complete archive from https://archive.opensensemap.org/
into a local ./archive_data/ directory, mirroring the remote structure.

Archive structure:
  {date}/
    {box-id}-{box-name}/
      {sensor-id}-{date}.csv     <- per-channel sensor readings
      {box-name}-{date}.json     <- senseBox metadata

Requirements:
  pip install aiohttp aiofiles

Usage:
  # Full archive from 2014 to today (default)
  python download_opensensemap.py

  # Specific date range
  python download_opensensemap.py --start 2024-01-01 --end 2024-12-31

  # Retry previously failed downloads
  python download_opensensemap.py --retry-failed

  # Dry run
  python download_opensensemap.py --start 2024-01-01 --end 2024-01-01 --dry-run

  # Only metadata JSON / only sensor CSV
  python download_opensensemap.py --json-only
  python download_opensensemap.py --csv-only

  # Resume from last clean stop (default behaviour when no --start is given)
  python download_opensensemap.py
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Required dependencies
# ---------------------------------------------------------------------------
try:
    import aiohttp
    import aiofiles
except ImportError:
    print("Missing required packages. Install with:\n  pip install aiohttp aiofiles")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL        = "https://archive.opensensemap.org/"
OUTPUT_DIR      = Path("archive_data")
FAILED_LOG      = Path("failed.log")
COMPLETED_LOG   = Path("completed_days.log")   # one YYYY-MM-DD per line
ARCHIVE_START   = date(2014, 6, 3)
DEFAULT_WORKERS = 8
CHUNK_SIZE      = 131_072   # 128 KiB
MAX_RETRIES     = 3
RETRY_DELAY     = 5         # seconds

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def load_completed_days() -> set:
    """Return the set of date strings (YYYY-MM-DD) already fully downloaded."""
    if not COMPLETED_LOG.exists():
        return set()
    lines = COMPLETED_LOG.read_text(encoding="utf-8").splitlines()
    return {l.strip() for l in lines if l.strip()}


def resume_start_date(end: date) -> date:
    """
    Return the first date that has NOT been completed yet.

    Walk completed_days.log to find the highest consecutive run from
    ARCHIVE_START; the day after that is where we resume.  If nothing
    is completed we start from ARCHIVE_START.
    """
    completed = load_completed_days()
    if not completed:
        return ARCHIVE_START

    cur = ARCHIVE_START
    while cur <= end:
        if cur.strftime("%Y-%m-%d") not in completed:
            return cur
        cur += timedelta(days=1)
    return cur   # everything already done → returns end+1 (nothing to do)


def clean_tmp_files(output_dir: Path) -> int:
    """
    Delete any *.tmp files left behind by a previous interrupted run.
    Returns the number of files removed.
    """
    removed = 0
    if not output_dir.exists():
        return 0
    for tmp in output_dir.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError as e:
            log.warning("Could not remove tmp file %s: %s", tmp, e)
    return removed


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _bar(filled: int, total: int, width: int = 20) -> str:
    if total == 0:
        pct = 0.0
        n   = 0
    else:
        pct = filled / total
        n   = int(pct * width)
    bar = "█" * n + "░" * (width - n)
    return f"[{bar}] {pct:5.1%}"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def _fmt_time(sec: float) -> str:
    if sec < 0 or sec > 86400 * 30:
        return "--:--:--"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_rate(bps: float) -> str:
    return _fmt_bytes(bps) + "/s"


# ---------------------------------------------------------------------------
# Live progress display  (pure stdlib, no tqdm)
# ---------------------------------------------------------------------------

class ProgressDisplay:
    """
    Renders a live multi-worker display:

      Overall  [████████░░░░░░░░░░░░]  42.0%  day 1322/4102  ETA 14:41:10  elapsed 06:59:02
      ↓ 8.3 MB/s   total 42.1 GB   ✔ 1,788,533   ⏭ 0   ✘ 0
      ── active workers ──────────────────────────────────────────
      2018-09-20   1188 stations   [████████████░░░░░░░░]  61%   842 files
      2018-09-21    943 stations   [██████░░░░░░░░░░░░░░]  32%   301 files
      ...
    """

    def __init__(self, total_days: int, max_workers: int = 8):
        self.total_days    = total_days
        self.max_workers   = max_workers
        self.days_done     = 0

        # per-day tracking:  day_str -> {"total": int, "done": int}
        self._active_days  = {}

        self.downloaded    = 0
        self.skipped       = 0
        self.failed        = 0
        self.bytes_total   = 0

        self._lock          = asyncio.Lock()
        self._start         = time.monotonic()
        self._last_bytes    = 0
        self._last_ts       = self._start
        self._rate_bps      = 0.0

        self._lines_written = 0
        self._stop          = False
        self._task          = None

    # -- mutations -----------------------------------------------------------

    async def set_day(self, day_str: str, num_stations: int):
        async with self._lock:
            self._active_days[day_str] = {"total": num_stations, "done": 0}

    async def finish_day(self):
        async with self._lock:
            self.days_done += 1

    async def day_file_done(self, day_str: str):
        """Call each time a file inside a day finishes (download or skip)."""
        async with self._lock:
            if day_str in self._active_days:
                self._active_days[day_str]["done"] += 1

    async def remove_day(self, day_str: str):
        async with self._lock:
            self._active_days.pop(day_str, None)

    async def record(self, downloaded=0, skipped=0, failed=0, nbytes=0):
        async with self._lock:
            self.downloaded  += downloaded
            self.skipped     += skipped
            self.failed      += failed
            self.bytes_total += nbytes

    # -- rendering -----------------------------------------------------------

    def _compute_rate(self) -> float:
        now = time.monotonic()
        dt  = now - self._last_ts
        if dt >= 1.0:
            self._rate_bps   = (self.bytes_total - self._last_bytes) / dt
            self._last_bytes = self.bytes_total
            self._last_ts    = now
        return self._rate_bps

    def _render(self) -> list:
        elapsed = time.monotonic() - self._start
        rate    = self._compute_rate()
        w       = _term_width()

        # -- overall bar --
        if self.days_done > 0 and self.total_days > 0:
            secs_per_day = elapsed / self.days_done
            remaining    = (self.total_days - self.days_done) * secs_per_day
            eta          = _fmt_time(remaining)
        else:
            eta = "--:--:--"

        bar   = _bar(self.days_done, self.total_days, width=25)
        line1 = (
            f"  Overall  {bar}  "
            f"day {self.days_done}/{self.total_days}  "
            f"ETA {eta}  elapsed {_fmt_time(elapsed)}"
        )

        # -- stats row --
        line2 = (
            f"  ↓ {_fmt_rate(rate):>12s}   "
            f"total {_fmt_bytes(self.bytes_total):>10s}   "
            f"✔ {self.downloaded:,}   "
            f"⏭ {self.skipped:,}   "
            f"✘ {self.failed}"
        )

        # -- per-worker rows --
        lines = [line1[:w], line2[:w]]
        # Show only days with actual download activity, most progressed first
        active_all = sorted(self._active_days.items())   # chronological
        active = [(d, i) for d, i in active_all if i["done"] > 0]
        waiting = len(active_all) - len(active)
        if active_all:
            lines.append("  " + "─" * min(60, w - 4))
            for day_str, info in active[:self.max_workers]:
                total = info["total"]
                done  = info["done"]
                mini  = _bar(done, total, width=16)
                pct   = (done / total * 100) if total else 0
                row   = (
                    f"  {day_str}  {total:>5d} stn  "
                    f"{mini}  {pct:4.0f}%  "
                    f"{done:>6d} files"
                )
                lines.append(row[:w])
            if waiting > 0:
                lines.append(f"  ... {waiting} day(s) queued (listing fetched, awaiting download slots)"[:w])

        return lines

    def _erase_lines(self, n: int):
        if n <= 0:
            return
        sys.stderr.write(f"\x1b[{n}A\x1b[J")

    def _draw(self):
        lines = self._render()
        self._erase_lines(self._lines_written)
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()
        self._lines_written = len(lines)

    async def _loop(self):
        while not self._stop:
            async with self._lock:
                self._draw()
            await asyncio.sleep(0.5)

    def start(self):
        sys.stderr.write("\n")
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self):
        self._stop = True
        if self._task:
            await self._task
        async with self._lock:
            self._draw()
        sys.stderr.write("\n")

    def final_summary(self) -> str:
        elapsed = time.monotonic() - self._start
        mb      = self.bytes_total / 1_048_576
        rate    = (mb / elapsed) if elapsed > 0 else 0
        return (
            f"\n{'─'*60}\n"
            f"  Downloaded : {self.downloaded:,}\n"
            f"  Skipped    : {self.skipped:,}  (already on disk)\n"
            f"  Failed     : {self.failed:,}  (see failed.log)\n"
            f"  Total data : {mb:.1f} MB\n"
            f"  Avg rate   : {rate:.2f} MB/s\n"
            f"  Elapsed    : {_fmt_time(elapsed)}\n"
            f"{'─'*60}"
        )


# ---------------------------------------------------------------------------
# HTML link extractor
# ---------------------------------------------------------------------------

def extract_links(html: str, base_url: str) -> list:
    urls = []
    for m in re.finditer(r'href="([^"?#]+)"', html):
        href = m.group(1).strip()
        if href in ("/", "../", "Go up", "") or href.startswith("?"):
            continue
        full = urljoin(base_url, href)
        if full.startswith(base_url) and full != base_url:
            urls.append(full)
    return urls


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class Downloader:
    def __init__(
        self,
        output_dir,
        workers        = DEFAULT_WORKERS,
        csv_only       = False,
        json_only      = False,
        dry_run        = False,
        timeout        = 120,
        completed_days = None,          # set of already-done day strings
    ):
        self.output_dir     = Path(output_dir)
        self.workers        = workers
        self.csv_only       = csv_only
        self.json_only      = json_only
        self.dry_run        = dry_run
        self.timeout        = aiohttp.ClientTimeout(total=timeout, connect=30)
        self._completed     = completed_days or set()

        self._sem           = None   # file downloads
        self._list_sem      = None   # HTML listing requests
        self._session       = None
        self._progress      = None
        self._failed_urls   = []
        self._failed_lock   = asyncio.Lock()

        # async-safe writer for completed_days.log
        self._completed_lock = asyncio.Lock()

    # ------------------------------------------------------------------ HTTP

    async def _get_html(self, url: str):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._list_sem:
                    async with self._session.get(url) as r:
                        # 404 = date simply not in the archive; no point retrying.
                        if r.status == 404:
                            log.debug("No archive entry (404): %s", url)
                            return None
                        r.raise_for_status()
                        return await r.text()
            except aiohttp.ClientResponseError as exc:
                if attempt == MAX_RETRIES:
                    log.warning("Give up listing %s: HTTP %s", url, exc.status)
                    return None
                await asyncio.sleep(RETRY_DELAY * attempt)
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    log.warning("Give up listing %s: %s", url, exc)
                    return None
                await asyncio.sleep(RETRY_DELAY * attempt)

    async def _download_file(self, url: str, dest: Path, day_str: str = "") -> bool:
        p = self._progress

        if dest.exists():
            if p:
                await p.record(skipped=1)
                await p.day_file_done(day_str)
            return True

        if self.dry_run:
            print(f"[DRY-RUN] {url}")
            return True

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._sem:
                    async with self._session.get(url) as r:
                        r.raise_for_status()
                        nbytes = 0
                        async with aiofiles.open(tmp, "wb") as f:
                            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                                await f.write(chunk)
                                nbytes += len(chunk)
                tmp.rename(dest)
                if p:
                    await p.record(downloaded=1, nbytes=nbytes)
                    await p.day_file_done(day_str)
                return True
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                if attempt == MAX_RETRIES:
                    log.warning("Give up download %s: %s", url, exc)
                    if p:
                        await p.record(failed=1)
                    async with self._failed_lock:
                        self._failed_urls.append(url)
                    return False
                await asyncio.sleep(RETRY_DELAY * attempt)
        return False

    # --------------------------------------------------------------- filters

    def _want(self, filename: str) -> bool:
        if self.csv_only:
            return filename.endswith(".csv")
        if self.json_only:
            return filename.endswith(".json")
        return filename.endswith(".csv") or filename.endswith(".json")

    # --------------------------------------------------------------- station

    async def _process_station(self, station_url: str, day_str: str):
        html = await self._get_html(station_url)
        if not html:
            return
        station_name = station_url.rstrip("/").rsplit("/", 1)[-1]
        station_dir  = self.output_dir / day_str / station_name

        tasks = []
        for link in extract_links(html, station_url):
            if link.endswith("/"):
                continue
            fname = link.rsplit("/", 1)[-1]
            if self._want(fname):
                tasks.append(self._download_file(link, station_dir / fname, day_str))
        if tasks:
            await asyncio.gather(*tasks)

    # ------------------------------------------------------------------- day

    async def _process_day(self, day: date):
        day_str = day.strftime("%Y-%m-%d")

        # ── Resume: skip days we already completed ──────────────────────────
        if day_str in self._completed:
            if self._progress:
                await self._progress.finish_day()
            return

        day_url = urljoin(BASE_URL, day_str + "/")

        html = await self._get_html(day_url)
        if html is None:
            log.debug("No listing for %s — skipping", day_str)
        else:
            stations = [u for u in extract_links(html, day_url) if u.endswith("/")]
            if self._progress:
                await self._progress.set_day(day_str, len(stations))
            await asyncio.gather(*[self._process_station(u, day_str) for u in stations])

        # Remove from active workers display
        if self._progress:
            await self._progress.remove_day(day_str)

        # Mark day as complete (append to log + in-memory set)
        if not self.dry_run:
            async with self._completed_lock:
                self._completed.add(day_str)
                with COMPLETED_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(day_str + "\n")

        if self._progress:
            await self._progress.finish_day()

    # ------------------------------------------------------------------- run

    async def _run(self, days: list):
        p = self._progress
        if p:
            p.start()

        # Wider batch window keeps workers saturated between batches.
        BATCH = max(self.workers * 4, 32)
        for i in range(0, len(days), BATCH):
            batch = days[i : i + BATCH]
            await asyncio.gather(*[self._process_day(d) for d in batch])

        if p:
            await p.stop()

    async def _main(self, days: list):
        connector = aiohttp.TCPConnector(
            # Single-host archive: per-host IS the binding limit.
            limit          = self.workers * 8,
            limit_per_host = self.workers,
            ttl_dns_cache  = 300,
            enable_cleanup_closed = True,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=self.timeout,
            headers={"User-Agent": "opensensemap-archiver/1.0"},
        ) as session:
            self._session = session
            await self._run(days)

        if self._failed_urls:
            with FAILED_LOG.open("a") as fh:
                for u in self._failed_urls:
                    fh.write(u + "\n")
            print(f"\n⚠  {len(self._failed_urls)} failures written to {FAILED_LOG}")

    def run(self, start: date, end: date):
        days = []
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)

        already_done = sum(
            1 for d in days if d.strftime("%Y-%m-%d") in self._completed
        )
        remaining = len(days) - already_done

        print(
            f"OpenSenseMap Downloader\n"
            f"  Date range  : {start} → {end}  ({len(days)} days)\n"
            f"  Already done: {already_done} days  (from {COMPLETED_LOG})\n"
            f"  Remaining   : {remaining} days\n"
            f"  Output      : {self.output_dir.resolve()}\n"
            f"  Workers     : {self.workers}  (tip: try --workers 32 or --workers 64 for small-file archives)\n"
            f"  Filters     : {'csv only' if self.csv_only else 'json only' if self.json_only else 'csv + json'}\n"
            f"  Dry run     : {self.dry_run}\n"
        )

        if remaining == 0:
            print("Nothing to do — all days already completed.")
            return

        self._sem      = asyncio.Semaphore(self.workers)
        self._list_sem = asyncio.Semaphore(max(self.workers // 2, 4))
        self._progress = ProgressDisplay(len(days), self.workers) if not self.dry_run else None
        asyncio.run(self._main(days))

        if self._progress:
            print(self._progress.final_summary())

    def run_retry(self):
        if not FAILED_LOG.exists():
            print(f"No {FAILED_LOG} found — nothing to retry.")
            return

        urls = [l.strip() for l in FAILED_LOG.read_text().splitlines() if l.strip()]
        if not urls:
            print(f"{FAILED_LOG} is empty — nothing to retry.")
            return

        print(f"Retrying {len(urls):,} failed URLs …")
        FAILED_LOG.unlink()
        self._failed_urls.clear()

        async def _retry_main():
            connector = aiohttp.TCPConnector(limit=self.workers * 4)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=self.timeout,
                headers={"User-Agent": "opensensemap-archiver/1.0"},
            ) as session:
                self._session = session
                tasks = []
                for url in urls:
                    rel  = url.replace(BASE_URL, "")
                    dest = self.output_dir / rel
                    tasks.append(self._download_file(url, dest))
                await asyncio.gather(*tasks)

            if self._failed_urls:
                with FAILED_LOG.open("a") as fh:
                    for u in self._failed_urls:
                        fh.write(u + "\n")
                print(f"⚠  {len(self._failed_urls)} still failing — see {FAILED_LOG}")

        self._sem      = asyncio.Semaphore(self.workers)
        self._list_sem = asyncio.Semaphore(max(self.workers // 2, 4))
        self._progress = ProgressDisplay(len(urls))
        asyncio.run(_retry_main())
        if self._progress:
            print(self._progress.final_summary())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_date_arg(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date '{s}' — use YYYY-MM-DD.")


def main():
    today = date.today()

    p = argparse.ArgumentParser(
        description="Download the full OpenSenseMap archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", type=parse_date_arg, default=None,
                   metavar="YYYY-MM-DD",
                   help=(
                       "First date to download. "
                       "Defaults to auto-resume: the first incomplete day "
                       f"after the last entry in {COMPLETED_LOG} "
                       f"(or {ARCHIVE_START} if the log does not exist)."
                   ))
    p.add_argument("--end", type=parse_date_arg, default=str(today),
                   metavar="YYYY-MM-DD",
                   help=f"Last date inclusive (default: today, {today}).")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR), metavar="DIR",
                   help=f"Root output directory (default: {OUTPUT_DIR}).")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
                   help=f"Parallel download workers (default: {DEFAULT_WORKERS}).")
    p.add_argument("--csv-only",     action="store_true", help="Download only CSV sensor files.")
    p.add_argument("--json-only",    action="store_true", help="Download only JSON metadata.")
    p.add_argument("--dry-run",      action="store_true", help="List URLs without downloading.")
    p.add_argument("--retry-failed", action="store_true", help=f"Retry URLs in {FAILED_LOG}.")
    p.add_argument("--no-resume",    action="store_true",
                   help=f"Ignore {COMPLETED_LOG} and start fresh from --start / {ARCHIVE_START}.")
    p.add_argument("--timeout", type=int, default=120, metavar="SEC",
                   help="Per-request timeout seconds (default: 120).")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")

    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.csv_only and args.json_only:
        p.error("--csv-only and --json-only are mutually exclusive.")

    end = args.end

    # ── Load completed days (unless --no-resume) ─────────────────────────
    completed = set() if args.no_resume else load_completed_days()

    # ── Determine start date ─────────────────────────────────────────────
    if args.start is not None:
        start = args.start
    elif args.no_resume:
        start = ARCHIVE_START
    else:
        start = resume_start_date(end)
        if start > end:
            print(f"All days up to {end} are already in {COMPLETED_LOG}. Nothing to do.")
            return
        if start > ARCHIVE_START:
            print(f"Resuming from {start}  (last completed day: {start - timedelta(days=1)})")

    if start > end:
        p.error("--start must not be after --end.")

    # ── Clean up any leftover .tmp files from a previous crash ───────────
    output_dir = Path(args.output_dir)
    removed = clean_tmp_files(output_dir)
    if removed:
        print(f"Cleaned up {removed} partial .tmp file(s) from previous run.")

    dl = Downloader(
        output_dir     = args.output_dir,
        workers        = args.workers,
        csv_only       = args.csv_only,
        json_only      = args.json_only,
        dry_run        = args.dry_run,
        timeout        = args.timeout,
        completed_days = completed,
    )

    if args.retry_failed:
        dl.run_retry()
    else:
        dl.run(start, end)


if __name__ == "__main__":
    main()
