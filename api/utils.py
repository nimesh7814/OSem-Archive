import io
import csv
import zipfile
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import StreamingResponse


def build_download_zip(url_rows, zip_filename: str = "osem_download_package.zip") -> StreamingResponse:

    # Build urls.csv in memory
    urls_csv_buf = io.StringIO()
    writer = csv.writer(urls_csv_buf)
    writer.writerow([
        "st_id", "name", "exposure", "model",
        "latitude", "longitude", "country", "region",
        "se_id", "title", "type", "category", "unit",
        "date", "csv_url"
    ])
    for row in url_rows:
        writer.writerow(row)

    # Build ZIP in memory — all files at root, no subfolders
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("urls.csv", urls_csv_buf.getvalue())

        downloader_dir = Path("downloader")
        for filename in ["osem_downloader.py", "README.md", "requirements.txt"]:
            filepath = downloader_dir / filename
            if not filepath.exists():
                raise HTTPException(status_code=500, detail=f"Missing required file: {filepath}")
            zf.write(filepath, arcname=filename)

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
    )