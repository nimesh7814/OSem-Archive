#!/usr/bin/env python3
"""
load_boundaries.py
------------------
Loads data/admin_boundary.geojson into the `boundaries` table.

Reads admin-0 and admin-1 properties from each GeoJSON feature and
upserts them using ST_GeomFromGeoJSON so PostGIS handles the projection.

Skips automatically if the boundaries table already contains rows — safe
to call repeatedly without duplicating data.

Usage:
    python load_boundaries.py [--geojson PATH] [--dsn DSN] [--batch-size N]

Defaults:
    --geojson   data/admin_boundary.geojson
    --dsn       postgresql://postgres:postgres@localhost:5432/postgres
    --batch-size 500
"""

import argparse
import json
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--geojson",    default="data/admin_boundary.geojson",
                   help="Path to the GeoJSON file (default: data/admin_boundary.geojson)")
    p.add_argument("--dsn",        default="postgresql://postgres:postgres@localhost:5432/postgres",
                   help="PostgreSQL connection string")
    p.add_argument("--batch-size", type=int, default=500,
                   help="Number of features to upsert per transaction (default: 500)")
    p.add_argument("--dry-run",    action="store_true",
                   help="Parse and validate the file without writing to the DB")
    p.add_argument("--force",      action="store_true",
                   help="Load even if boundaries table already has rows")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Already-loaded guard
# ---------------------------------------------------------------------------

def already_loaded(dsn: str) -> bool:
    """Return True if the boundaries table already contains rows."""
    try:
        conn = psycopg2.connect(dsn)
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM boundaries")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count > 0
    except Exception as e:
        print(f"[WARN] Could not check boundaries table: {e}", file=sys.stderr)
        return False

# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO boundaries (region_code, region_name, country_code, country_name, geometry)
VALUES (
    %(region_code)s,
    %(region_name)s,
    %(country_code)s,
    %(country_name)s,
    ST_SetSRID(ST_GeomFromGeoJSON(%(geom_json)s), 4326)
)
ON CONFLICT (region_code) DO UPDATE SET
    region_name  = EXCLUDED.region_name,
    country_code = EXCLUDED.country_code,
    country_name = EXCLUDED.country_name,
    geometry     = EXCLUDED.geometry;
"""


def feature_to_row(feature: dict) -> dict:
    """Extract a flat row dict from a GeoJSON feature."""
    props = feature.get("properties") or {}

    region_code  = props.get("adm1_code")
    country_code = props.get("adm0_code")
    region_name  = props.get("adm1_name")
    country_name = props.get("adm0_name")

    if not region_code:
        raise ValueError(f"Feature missing adm1_code: {props}")

    geom = feature.get("geometry")
    if geom is None:
        raise ValueError(f"Feature {region_code!r} has no geometry")

    # Ensure it is MultiPolygon — the schema column is GEOMETRY(MultiPolygon, 4326)
    if geom["type"] == "Polygon":
        geom = {"type": "MultiPolygon", "coordinates": [geom["coordinates"]]}
    elif geom["type"] != "MultiPolygon":
        raise ValueError(f"Feature {region_code!r} has unsupported geometry type: {geom['type']}")

    return {
        "region_code":  region_code,
        "region_name":  region_name,
        "country_code": country_code,
        "country_name": country_name,
        "geom_json":    json.dumps(geom),
    }


def load(geojson_path: Path, dsn: str, batch_size: int, dry_run: bool) -> None:
    print(f"Reading {geojson_path} …")
    t0 = time.perf_counter()

    with geojson_path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    features = data.get("features", [])
    total = len(features)
    print(f"  {total:,} features found")

    if dry_run:
        errors = 0
        for i, feat in enumerate(features, 1):
            try:
                feature_to_row(feat)
            except ValueError as e:
                print(f"  [WARN] Feature {i}: {e}", file=sys.stderr)
                errors += 1
        print(f"Dry-run complete — {total - errors:,} valid, {errors} invalid")
        return

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur  = conn.cursor()

    inserted = 0
    errors   = 0

    for batch_start in range(0, total, batch_size):
        batch = features[batch_start : batch_start + batch_size]
        rows  = []
        for feat in batch:
            try:
                rows.append(feature_to_row(feat))
            except ValueError as e:
                print(f"  [WARN] {e}", file=sys.stderr)
                errors += 1

        if rows:
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=batch_size)
            conn.commit()
            inserted += len(rows)

        pct = min(batch_start + batch_size, total) / total * 100
        print(f"  {min(batch_start + batch_size, total):,}/{total:,}  ({pct:.0f}%)", end="\r")

    cur.close()
    conn.close()

    elapsed = time.perf_counter() - t0
    print(f"\nDone — {inserted:,} rows upserted, {errors} skipped  ({elapsed:.1f}s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    path = Path(args.geojson)

    if not path.exists():
        sys.exit(f"ERROR: GeoJSON file not found: {path}")

    # Skip if already loaded (unless --force is passed)
    if not args.force and not args.dry_run and already_loaded(args.dsn):
        print("Boundaries table already contains data — skipping load.")
        print("Use --force to reload anyway.")
        sys.exit(0)

    load(path, args.dsn, args.batch_size, args.dry_run)


if __name__ == "__main__":
    main()
