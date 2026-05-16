# OSem Downloader

A lightweight command-line tool to download sensor observation CSVs from the OSem API, with built-in resume support and flexible export options.

---

## Requirements

- Python 3.10 or newer
- Install dependencies:

```bash
pip install -r requirements.txt
```

The `requirements.txt` includes:

```
requests==2.32.3
tqdm==4.66.4
```

---

## Quick Start

Place `urls.txt` (from the OSem API) in the same folder as the script, then run:

```bash
python osem_downloader.py
```

That's it. The script will download all files and then ask how you want to export the data.

---

## How It Works

### 1. Input — `urls.txt`

The script expects a tab-separated file with the following columns:

| Column | Description |
|---|---|
| `country` | Country code |
| `region` | Region name |
| `category` | Sensor category (e.g. `temperature`) |
| `st_id` | Station ID |
| `se_id` | Sensor ID |
| `date` | Observation date |
| `csv_url` | Direct URL to the CSV file |
| `station_name` | Human-readable station name |
| `exposure` | Station exposure type |
| `model` | Station hardware model |
| `latitude` | Station latitude (decimal degrees) |
| `longitude` | Station longitude (decimal degrees) |
| `sensor_title` | Sensor display name |
| `sensor_type` | Sensor type identifier |
| `unit` | Measurement unit |

### 2. Download

Files are saved to `.data/<country>/<region>/<category>/<st_id>/` as `<se_id>_<date>.csv`.

A resume log (`downloaded.log`) keeps track of completed downloads. Re-running the script skips already-downloaded files automatically — safe to interrupt and restart at any time.

A `README.md` is also fetched from the configured endpoint and saved into the download folder alongside the data.

### 3. Export

After downloading, choose an export format:

| Option | Output file | Description |
|---|---|---|
| `[1]` JSON | `osem_data.json` | Array of observation records with `latitude` and `longitude` fields |
| `[2]` CSV | `osem_data.csv` | Flat table with `latitude` and `longitude` columns |
| `[3]` GeoJSON | `osem_data.geojson` | GeoJSON FeatureCollection — each observation is a `Point` feature, ready for GIS tools and mapping libraries |
| `[4]` All formats | All three files | Saves JSON, CSV, and GeoJSON at once |
| `[5]` Skip | — | Exit without exporting |

All formats include full station and sensor metadata per observation row.

---

## Command-Line Options

| Flag | Default | Description |
|---|---|---|
| `--urls PATH` | `urls.txt` | Path to the input urls file |
| `--output DIR` | `.data` | Folder where CSVs are downloaded |
| `--log FILE` | `downloaded.log` | Resume log file path |
| `--no-download` | off | Skip downloading; process files already in `--output` |
| `--no-export` | off | Skip the export step entirely |

### Examples

```bash
# Default run — reads urls.txt, downloads to .data/, then asks about export
python osem_downloader.py

# Use a different urls file and output folder
python osem_downloader.py --urls my_urls.txt --output my_data

# Skip download, just re-export from already-downloaded files
python osem_downloader.py --no-download

# Download only, no export prompt
python osem_downloader.py --no-export
```

---

## Output Structure

```
.data/
└── Germany/
    └── NRW/
        └── temperature/
            └── station_123/
                ├── sensor_456_2024-01-01.csv
                └── sensor_456_2024-01-02.csv
        README.md

downloaded.log
osem_data.json       ← if exported
osem_data.csv        ← if exported
osem_data.geojson    ← if exported
```

---

## GeoJSON Notes

The GeoJSON export follows the [RFC 7946](https://datatracker.ietf.org/doc/html/rfc7946) standard:

- Each observation is a `Feature` with `geometry.type = "Point"`
- Coordinates are `[longitude, latitude]` (GeoJSON convention)
- All metadata and sensor values are stored in `properties`
- Observations with missing or invalid coordinates get `"geometry": null`

The `.geojson` file can be loaded directly into QGIS, Kepler.gl, Mapbox, Leaflet, or any GIS tool.

---

## Configuring the README Download

To enable automatic README downloading, open `osem_downloader.py` and update the `README_URL` constant near the top:

```python
README_URL = "https://raw.githubusercontent.com/YOUR_ORG/YOUR_REPO/main/README.md"
```

Replace with the actual raw URL of the file you want to download. If left unconfigured, this step is silently skipped.

---

## Troubleshooting

**`[ERROR] File not found: urls.txt`**
Make sure `urls.txt` is in the same directory as the script, or pass its path with `--urls`.

**`[WARN] Failed <url>: ...`**
Network or server errors. The file is removed and excluded from the log. Simply re-run — the script retries any file not in the log.

**`[WARN] N files had no matching metadata entry`**
A downloaded CSV filename didn't match any entry in `urls.txt`. This can happen if you switch `urls.txt` files between runs. Use `--urls` to point to the correct file.

**No observations after processing**
Check that files actually downloaded (look inside `.data/`). If the folder is empty, check your network connection or the contents of `urls.txt`.
