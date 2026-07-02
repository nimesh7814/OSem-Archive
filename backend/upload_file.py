import geopandas as gpd
import matplotlib.pyplot as plt
import geojson
import os
import time

base_dir = os.path.dirname(os.path.abspath(__file__))
GEOMETRY = ["Polygon", "MultiPolygon"]


# Get the .zip (Shapefile) as GeoDataFrame
def read_zip(zip_path):
    zip_path = os.path.join(base_dir, zip_path)
    gdf = gpd.read_file(f"zip:{zip_path}")
    return gdf


# Get a KML file as GeoDataFrame
def read_kml(kml_path):
    kml_path = os.path.join(base_dir, kml_path)
    gdf = gpd.read_file(kml_path, driver='KML')
    return gdf


# Process the GeoDataFrame correctly and return a GeoJSON object
def process_gdf(gdf, file_name):
    # Keep only polygon geometries
    gdf = gdf[gdf.geom_type.isin(GEOMETRY)]

    # Check if the GeoDataFrame is empty after filtering
    if gdf.empty:
        raise ValueError("Error: Geometry type.")

    # Handle the CRS
    if gdf.crs is None:
        raise ValueError("Error: No CRS defined.")

    if gdf.crs != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")

    # Dissolve the geometries
    gdf = gdf.dissolve()

    # Keep only the geometry column, drop all attributes
    gdf = gdf[["geometry"]]

    # Details for the GeoJSON
    gdf["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    gdf["name"] = file_name
    gdf_metric = gdf.to_crs("EPSG:6933")
    gdf["area_km2"] = gdf_metric.area / 10**6

    # Convert to GeoJSON
    return geojson.loads(gdf.to_json())


# Complete GeoJSON processing
def process_geojson(geojson_path):
    path = os.path.join(base_dir, geojson_path)

    # Check the GeoJSON file is valid
    try:
        with open(path, "r") as f:
            data = geojson.load(f)
    except Exception as e:
        raise ValueError(f"Error: Invalid GeoJSON file \n{e}")

    # Read the file into a GeoDataFrame
    gdf = gpd.read_file(path)
    file_name = os.path.splitext(os.path.basename(geojson_path))[0]
    return process_gdf(gdf, file_name)


# Complete Process of a .KML file
def process_kml(kml_path):
    gdf = read_kml(kml_path)
    file_name = os.path.splitext(os.path.basename(kml_path))[0]
    return process_gdf(gdf, file_name)


# Complete Process of a .ZIP file
def process_zip(zip_path):
    gdf = read_zip(zip_path)
    file_name = os.path.splitext(os.path.basename(zip_path))[0]
    return process_gdf(gdf, file_name)