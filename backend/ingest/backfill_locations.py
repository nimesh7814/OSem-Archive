from datetime import date

import load_archive as la


def find_box_dir(day: date, box_id: str) -> str | None:
    day_url = f"{la.URL}/{day.isoformat()}/"
    for name in la.list_dirs(day_url):
        if name.split("-", 1)[0] == box_id:
            return name
    return None


def backfill() -> None:
    with la.conn.cursor() as cur:
        cur.execute("SELECT id FROM boxes WHERE location IS NULL")
        box_ids = [row[0] for row in cur.fetchall()]

    print(f"Backfilling location for {len(box_ids)} boxes")
    updated = 0

    for box_id in box_ids:
        with la.conn.cursor() as cur:
            cur.execute(
                """
                SELECT archive_date FROM ingest_log
                WHERE box_id = %s AND status = 'done'
                ORDER BY archive_date LIMIT 1
                """,
                (box_id,),
            )
            row = cur.fetchone()
        if not row:
            continue
        day = row[0]

        dir_name = find_box_dir(day, box_id)
        if not dir_name:
            continue

        box_url = f"{la.URL}/{day.isoformat()}/{dir_name}/"
        files = la.list_files(box_url)
        if not files["json"]:
            continue

        box_json = la.session.get(files["json"], timeout=30).json()
        coordinates = ((box_json.get("loc") or {}).get("geometry") or {}).get("coordinates")
        if not coordinates or len(coordinates) < 2:
            continue

        lon, lat = coordinates[0], coordinates[1]
        with la.conn.cursor() as cur:
            cur.execute(
                "UPDATE boxes SET location = ST_GeomFromText(%s, 4326) WHERE id = %s",
                (f"POINT({lon} {lat})", box_id),
            )
        la.assign_region(box_id)
        la.conn.commit()
        updated += 1

    print(f"Updated location for {updated} of {len(box_ids)} boxes")


if __name__ == "__main__":
    backfill()
    la.conn.close()
