TAGS_QUERY = '''
    SELECT DISTINCT s.sensor_type
    FROM sensors s
    JOIN boxes b ON b.id = s.box_id
    WHERE s.last_measurement IS NOT NULL
      AND b.region_id IS NOT NULL
      AND s.sensor_type IS NOT NULL
    ORDER BY s.sensor_type
'''

PHENOMENA_QUERY = '''
    SELECT DISTINCT s.title
    FROM sensors s
    JOIN boxes b ON b.id = s.box_id
    WHERE s.last_measurement IS NOT NULL
      AND b.region_id IS NOT NULL
    ORDER BY s.title
'''

EXPOSURE_QUERY = '''
    SELECT DISTINCT b.exposure
    FROM boxes b
    JOIN sensors s ON s.box_id = b.id
    WHERE s.last_measurement IS NOT NULL
      AND b.region_id IS NOT NULL
      AND b.exposure IS NOT NULL
    ORDER BY b.exposure
'''

MEASUREMENT_AGGREGATES = {
    "raw": {
        "table": "measurements",
        "time_column": "time",
        "value_columns": '''
            d.time AS bucket,
            d.value,
            NULL::double precision AS avg_value,
            NULL::double precision AS min_value,
            NULL::double precision AS max_value,
            NULL::integer AS rdgs_count
        ''',
    },
    "hourly": {
        "table": "reading_hourly",
        "time_column": "bucket",
        "value_columns": '''
            d.bucket,
            NULL::double precision AS value,
            d.avg_value,
            d.min_value,
            d.max_value,
            d.rdgs_count
        ''',
    },
    "daily": {
        "table": "reading_daily",
        "time_column": "bucket",
        "value_columns": '''
            d.bucket,
            NULL::double precision AS value,
            d.avg_value,
            d.min_value,
            d.max_value,
            d.rdgs_count
        ''',
    },
    "monthly": {
        "table": "reading_monthly",
        "time_column": "bucket",
        "value_columns": '''
            d.bucket,
            NULL::double precision AS value,
            d.avg_value,
            d.min_value,
            d.max_value,
            d.rdgs_count
        ''',
    },
    "yearly": {
        "table": "reading_yearly",
        "time_column": "bucket",
        "value_columns": '''
            d.bucket,
            NULL::double precision AS value,
            d.avg_value,
            d.min_value,
            d.max_value,
            d.rdgs_count
        ''',
    },
}
