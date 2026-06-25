-- 00002_regions.sql
-- openSenseMap Archive — seed regions from the admin boundary GeoJSON

BEGIN;

WITH features AS (
    SELECT
        feature->'properties'->>'adm0_name' AS country,
        feature->'properties'->>'adm1_name' AS region,
        ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(feature->'geometry'), 4326)) AS geom
    FROM jsonb_array_elements(
        pg_read_file('/docker-entrypoint-initdb.d/admin_boundary.geojson')::jsonb -> 'features'
    ) AS feature
)
INSERT INTO regions (country, region, geometry)
SELECT country, region, ST_Multi(ST_Union(geom))::geography
FROM features
WHERE country IS NOT NULL AND region IS NOT NULL
GROUP BY country, region
ON CONFLICT (country, region) DO UPDATE SET geometry = EXCLUDED.geometry;

COMMIT;
