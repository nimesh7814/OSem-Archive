@app.get("/country_region_data")
def country_region_data(
    download: Optional[bool] = Query(False, description="Set true to download ZIP"),
    from_date: Optional[date] = Query(date(2014, 6, 3), description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query("all", description="Sensor category (single or comma-separated)"),
    country: Optional[str] = Query(None, description="Country filter (single or comma-separated)"),
    region: Optional[str] = Query(None, description="Region filter (single or comma-separated)")
):
    
    conn = get_db_connection()
    cur = conn.cursor()

    # Remove unnecessary joins for summary query
    sf_filters = []
    sf_params = []
    
    if from_date:
        sf_filters.append("date >= %s")
        sf_params.append(from_date)
    if to_date:
        sf_filters.append("date <= %s")
        sf_params.append(to_date)

    sf_where = ("WHERE " + " AND ".join(sf_filters)) if sf_filters else ""

    # Build the main query
    query = f'''
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_id) AS total_stations,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(sf_agg.size_mb), 0) AS estimated_size
        FROM stations st
        LEFT JOIN sensors se ON st.st_id = se.st_id
        LEFT JOIN (
            SELECT se_id, SUM(size_mb) AS size_mb
            FROM sensor_files
            {sf_where}
            GROUP BY se_id
        ) sf_agg ON se.se_id = sf_agg.se_id
        WHERE st.country IS NOT NULL
    '''

    params = sf_params[:]
    
    # Apply category, country, and region filters
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        query += f" AND se.category IN ({placeholders})"
        params.extend(categories)
        
    if country:
        countries = [c.strip() for c in country.split(",")]
        placeholders = ",".join(["%s"] * len(countries))
        query += f" AND st.country IN ({placeholders})"
        params.extend(countries)
        
    if region:
        regions = [r.strip() for r in region.split(",")]
        placeholders = ",".join(["%s"] * len(regions))
        query += f" AND st.region IN ({placeholders})"
        params.extend(regions)

    query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    
    # Build the result dictionary
    result = []
    for row_country, row_region, total_stations, total_sensors, estimated_size in rows:
        result.append({
            "country": row_country,
            "region": row_region,
            "total_stations": total_stations,
            "total_sensors": total_sensors,
            "estimated_size_mb": float(estimated_size)
        })

    if not download:
        cur.close()
        conn.close()
        return result
    
    # URLs query for the download
    url_query = '''
        SELECT
            st.st_id,
            st.name,
            st.exposure,
            st.model,
            ST_Y(st.location::geometry) AS latitude,
            ST_X(st.location::geometry) AS longitude,
            st.country,
            st.region,
            se.se_id,
            se.title,
            se.type,
            se.category,
            se.unit,
            sf.date,
            sf.csv_url
        FROM stations st
        JOIN sensors se ON st.st_id = se.st_id
        JOIN sensor_files sf ON se.se_id = sf.se_id
        WHERE st.country IS NOT NULL
    '''
    
    url_params = []

    # Apply the same filters to the URL query
    if from_date:
        url_query += " AND sf.date >= %s"
        url_params.append(from_date)
    if to_date:
        url_query += " AND sf.date <= %s"
        url_params.append(to_date)
        
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        url_query += f" AND se.category IN ({placeholders})"
        url_params.extend(categories)
        
    if country:
        countries = [c.strip() for c in country.split(",")]
        placeholders = ",".join(["%s"] * len(countries))
        url_query += f" AND st.country IN ({placeholders})"
        url_params.extend(countries)
    if region:
        regions = [r.strip() for r in region.split(",")]
        placeholders = ",".join(["%s"] * len(regions))
        url_query += f" AND st.region IN ({placeholders})"
        url_params.extend(regions)
        
    url_query += " ORDER BY sf.date"
    cur.execute(url_query, tuple(url_params))
    url_rows = cur.fetchall()

    cur.close()
    conn.close()

    # Build zip filename
    country_str = country.replace(",", "-").replace(" ", "_") if country else "all"
    region_str = region.replace(",", "-").replace(" ", "_")  if region  else "all"
    from_str = from_date.strftime("%Y%m%d") if from_date else "start"
    to_str = to_date.strftime("%Y%m%d") if to_date else date.today().strftime("%Y%m%d")
    
    num_days = (to_date - from_date).days if (to_date and from_date) else (date.today() - from_date).days
   
    # Filename format
    zip_filename = f"{country_str}_{region_str}_{from_str}_{to_str}_{num_days}d.zip"

    return build_download_zip(url_rows, zip_filename=zip_filename)