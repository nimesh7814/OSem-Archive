import geopandas as gpd
import geojson
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
GEOMETRY = ["Polygon", "MultiPolygon"]


# Get the .zip (Shapefile) as a GeoDataFrame
def read_zip(zip_path):
    zip_path = os.path.join(base_dir, zip_path)
    try:
        return gpd.read_file(f"zip:{zip_path}")
    except Exception as e:
        raise ValueError(f"Invalid or unreadable shapefile zip: {e}")


# Get a KML file as a GeoDataFrame
def read_kml(kml_path):
    kml_path = os.path.join(base_dir, kml_path)
    try:
        return gpd.read_file(kml_path, driver="KML")
    except Exception as e:
        raise ValueError(f"Invalid or unreadable KML file: {e}")


# Reduces a GeoDataFrame to a single EPSG:4326 (Polygon/MultiPolygon) GeoJSON geometry.
def process_gdf(gdf):
    gdf = gdf[gdf.geom_type.isin(GEOMETRY)]
    if gdf.empty:
        raise ValueError("AOI file has no Polygon/MultiPolygon geometry to use as an area filter.")

    if gdf.crs is None:
        raise ValueError("AOI file has no coordinate reference system (CRS) defined.")

    if gdf.crs != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")

    dissolved = gdf.dissolve()[["geometry"]]
    return geojson.loads(dissolved.to_json())


# Complete GeoJSON processing
def process_geojson(geojson_path):
    path = os.path.join(base_dir, geojson_path)

    # Cheap syntactic check before the heavier geopandas/GDAL read below.
    try:
        with open(path, "r") as f:
            geojson.load(f)
    except Exception as e:
        raise ValueError(f"Invalid GeoJSON file: {e}")

    try:
        gdf = gpd.read_file(path)
    except Exception as e:
        raise ValueError(f"Invalid GeoJSON file: {e}")

    return process_gdf(gdf)


# Complete process of a .KML file
def process_kml(kml_path):
    return process_gdf(read_kml(kml_path))


# Complete process of a .ZIP file
def process_zip(zip_path):
    return process_gdf(read_zip(zip_path))