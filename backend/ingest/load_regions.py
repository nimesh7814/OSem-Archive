import os
from pathlib import Path
import geopandas as gpd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from shapely.geometry import MultiPolygon, Polygon


# Load environment variables from backend/.env
env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=env_path)


# Database connection parameters
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")


# Create SQLAlchemy engine
engine = create_engine(f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

# Load GeoJSON (path resolved relative to this file, not the working directory)
file_path = Path(__file__).resolve().parents[1] / "data" / "admin_boundary.geojson"
gdf = gpd.read_file(file_path)


# Check for required columns
required_columns = ["adm0_name", "adm1_name"]

missing = [c for c in required_columns if c not in gdf.columns]
if missing:
    raise ValueError(f"GeoJSON is missing required properties: {missing}")


# Fix invalid polygons and ensure consistent MultiPolygon type
gdf["geometry"] = gdf["geometry"].make_valid()
gdf["geometry"] = gdf["geometry"].apply(
    lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
)

# Select and rename columns to match the database schema (regions.country, regions.region)
gdf = gdf[["adm0_name", "adm1_name", "geometry"]].rename(
    columns={"adm0_name": "country", "adm1_name": "region"}
)


# features sharing the same country/region — merge those into one geometry
gdf = gdf.dissolve(by=["country", "region"], as_index=False)
gdf["geometry"] = gdf["geometry"].apply(
    lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
)

rows = [
    {"country": country, "region": region, "geom_wkt": geometry.wkt}
    for country, region, geometry in zip(gdf["country"], gdf["region"], gdf["geometry"])
]

with engine.begin() as conn:
    conn.execute(
        text(
            "INSERT INTO regions (country, region, geometry) "
            "VALUES (:country, :region, ST_GeomFromText(:geom_wkt, 4326)) "
            "ON CONFLICT (country, region) DO UPDATE SET geometry = EXCLUDED.geometry"
        ),
        rows,
    )

print(f"Loaded {len(rows)} regions into PostGIS")