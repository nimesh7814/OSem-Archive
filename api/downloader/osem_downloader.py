"""
OSem Downloader for CSV
-----------------------
Usage:
    python osem_downloader.py
    python osem_downloader.py --urls urls.csv
    python osem_downloader.py --output my_folder
    python osem_downloader.py --no-download
    python osem_downloader.py --no-export

Downloads all CSVs listed in urls.csv into .data/<country>/<region>/<category>/<st_id>/
Keeps a resume log (downloaded.log) — only files not in the log are downloaded.
Then lets you convert the downloaded data to .json, .csv, or .geojson.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import requests
from tqdm import tqdm

LOG_FILE   = "downloaded.log"
URLS_FILE  = "urls.csv" 


# ------------------------------------------------------------------ #
# Resume log helpers
# ------------------------------------------------------------------ #

def load_log(log_path: Path) -> set:
    if not log_path.exists():
        return set()
    with open(log_path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_log(log_path: Path, key: str):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def make_key(record: dict) -> str:
    return f"{record['se_id'].strip()}_{record['date'].strip()}"


# ------------------------------------------------------------------ #
# Parse urls.csv
# ------------------------------------------------------------------ #

def parse_urls_file(urls_file: str) -> list[dict]:
    records = []
    path = Path(urls_file)
    if not path.exists():
        print(f"[ERROR] File not found: {urls_file}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)  # CSV format
        for row in reader:
            records.append(row)

    print(f"[INFO] Found {len(records)} total files in {urls_file}.")
    return records





# ------------------------------------------------------------------ #
# Downloading
# ------------------------------------------------------------------ #

def download_file(record: dict, base_dir: Path, log_path: Path, pbar: tqdm):
    country  = record["country"].strip()
    region   = record["region"].strip()
    category = record["category"].strip()
    st_id    = record["st_id"].strip()
    se_id    = record["se_id"].strip()
    date     = record["date"].strip()
    url      = record["csv_url"].strip()

    folder    = base_dir / country / region / category / st_id
    file_name = f"{se_id}_{date}.csv"
    file_path = folder / file_name
    key       = make_key(record)

    folder.mkdir(parents=True, exist_ok=True)

    try:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(file_path, "wb") as fd:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                fd.write(chunk)

        append_log(log_path, key)
        pbar.update(1)
        return True, file_path

    except Exception as e:
        pbar.write(f"[WARN] Failed {url}: {e}")
        pbar.update(1)
        if file_path.exists():
            file_path.unlink()
        return False, None


def download_all(records: list[dict], base_dir: Path, log_path: Path):
    already_done = load_log(log_path)
    pending = [r for r in records if make_key(r) not in already_done]

    skipped = len(records) - len(pending)
    if skipped:
        print(f"[INFO] Skipping {skipped} already-downloaded files (found in {log_path}).")
    print(f"[INFO] Downloading {len(pending)} remaining files...")

    if not pending:
        print("[INFO] Nothing to download — all files already complete.")
        return

    failed = []
    with tqdm(total=len(pending), desc="Downloading", unit="file") as pbar:
        for rec in pending:
            success, _ = download_file(rec, base_dir, log_path, pbar)
            if not success:
                failed.append(rec)

    total_done = len(records) - len(failed)
    print(f"\n[INFO] Download complete. {total_done}/{len(records)} files successful.")
    if failed:
        print(f"[WARN] {len(failed)} files failed. Re-run the script to retry them.")


# ------------------------------------------------------------------ #
# Build dataset from downloaded CSVs
# ------------------------------------------------------------------ #

def _safe_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def build_dataset(records: list[dict], base_dir: Path) -> list[dict]:
    meta_lookup = {(rec["se_id"].strip(), rec["date"].strip()): rec for rec in records}

    dataset = []
    all_files = list(base_dir.rglob("*.csv"))
    print(f"\n[INFO] Reading {len(all_files)} CSV files...")

    for file_path in tqdm(all_files, desc="Processing", unit="file"):
        stem  = file_path.stem
        parts = stem.split("_", 1)
        if len(parts) != 2:
            continue

        se_id, date = parts
        meta = meta_lookup.get((se_id, date))
        if not meta:
            continue

        lat = _safe_float(meta.get("latitude", ""))
        lon = _safe_float(meta.get("longitude", ""))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    obs = {
                        "station_id":       meta["st_id"].strip(),
                        "station_name":     meta["name"].strip(),
                        "station_exposure": meta["exposure"].strip(),
                        "station_model":    meta["model"].strip(),
                        "latitude":         lat,
                        "longitude":        lon,
                        "country":          meta["country"].strip(),
                        "region":           meta["region"].strip(),
                        "sensor_id":        meta["se_id"].strip(),
                        "sensor_title":     meta["title"].strip(),
                        "sensor_type":      meta["type"].strip(),
                        "sensor_category":  meta["category"].strip(),
                        "sensor_unit":      meta["unit"].strip(),
                        "sensor_time":      row.get("createdAt", row.get("time", row.get("timestamp", ""))).strip(),
                        "sensor_value":     row.get("value", "").strip(),
                    }
                    dataset.append(obs)
        except Exception as e:
            print(f"[WARN] Could not read {file_path}: {e}")

    print(f"[INFO] Total observations loaded: {len(dataset)}")
    return dataset


# ------------------------------------------------------------------ #
# Export helpers
# ------------------------------------------------------------------ #

def export_json(dataset: list[dict], out_path: Path):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved JSON -> {out_path}  ({len(dataset)} records)")


def export_csv(dataset: list[dict], out_path: Path):
    if not dataset:
        print("[WARN] No data to export.")
        return
    fields = list(dataset[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(dataset)
    print(f"[OK] Saved CSV -> {out_path}  ({len(dataset)} records)")


def export_geojson(dataset: list[dict], out_path: Path):
    features = []
    for obs in dataset:
        lat = obs.get("latitude")
        lon = obs.get("longitude")
        geometry = {"type": "Point", "coordinates": [lon, lat]} if lat and lon else None
        props = {k: v for k, v in obs.items() if k not in ("latitude", "longitude")}
        props["latitude"] = lat
        props["longitude"] = lon
        features.append({"type": "Feature", "geometry": geometry, "properties": props})

    geojson = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved GeoJSON -> {out_path}  ({len(features)} features)")


def export_menu(dataset: list[dict], output_dir: Path):
    print("\n" + "=" * 50)
    print("  Export Options")
    print("=" * 50)
    print("  [1] Save as JSON")
    print("  [2] Save as CSV")
    print("  [3] Save as GeoJSON")
    print("  [4] Save all formats")
    print("  [5] Skip export")
    print("=" * 50)

    choice = input("Choose [1/2/3/4/5]: ").strip()
    if choice in ("1", "4"):
        export_json(dataset, output_dir / "osem_data.json")
    if choice in ("2", "4"):
        export_csv(dataset, output_dir / "osem_data.csv")
    if choice in ("3", "4"):
        export_geojson(dataset, output_dir / "osem_data.geojson")
    if choice == "5":
        print("[INFO] Export skipped.")


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="OSem CSV Downloader & Exporter")
    parser.add_argument("--urls",        type=str, default=URLS_FILE, help=f"Path to urls CSV file (default: {URLS_FILE})")
    parser.add_argument("--output",      type=str, default=".data",   help="Download folder (default: .data)")
    parser.add_argument("--log",         type=str, default=LOG_FILE,  help=f"Resume log file (default: {LOG_FILE})")
    parser.add_argument("--no-download", action="store_true",         help="Skip download, just process existing files")
    parser.add_argument("--no-export",   action="store_true",         help="Skip export step")
    args = parser.parse_args()

    base_dir   = Path(args.output)
    log_path   = Path(args.log)
    output_dir = Path(".")

    records = parse_urls_file(args.urls)

    if not args.no_download:
        print(f"\n[INFO] Downloading to: {base_dir}/")
        print(f"[INFO] Resume log:      {log_path}")
        download_all(records, base_dir, log_path)
    else:
        print(f"[INFO] Skipping download, reading from {base_dir}/")

    if not args.no_export:
        print("\n[INFO] Building dataset from downloaded files...")
        dataset = build_dataset(records, base_dir)
        if dataset:
            export_menu(dataset, output_dir)
        else:
            print("[WARN] No observations found. Check that files downloaded correctly.")


if __name__ == "__main__":
    main()