"""
OpenSenseMap — Station Map Visualizer — http://localhost:5052
Serves an interactive Leaflet map of all stations stored in the DB,
with per-sensor reading stats shown on click.
"""

import os
import json
from flask import Flask, jsonify, render_template_string

import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "opensensemap"),
    "user":     os.getenv("DB_USER",     "osm"),
    "password": os.getenv("DB_PASSWORD", "changeme"),
}

app = Flask(__name__)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stations/geo")
def stations_geo():
    """
    GeoJSON FeatureCollection of all stations with coordinates.
    Properties include summary stats.
    """
    try:
        conn = psycopg2.connect(connect_timeout=5, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            WITH sensor_stats AS (
              SELECT
                sensor_id,
                SUM(reading_count)::BIGINT AS reading_count,
                MIN(bucket)                AS earliest,
                MAX(bucket) + INTERVAL '1 hour' AS latest
              FROM sensor_data_hourly
              GROUP BY sensor_id
            )
                SELECT
                    st.station_id,
                    st.name,
                    st.box_type,
                    st.exposure,
                    ST_X(st.geometry)           AS longitude,
                    ST_Y(st.geometry)           AS latitude,
                    COUNT(DISTINCT s.sensor_id) AS sensor_count,
              COALESCE(SUM(ss.reading_count), 0)::BIGINT AS reading_count,
              MIN(ss.earliest)            AS earliest,
              MAX(ss.latest)              AS latest
                FROM stations st
            LEFT JOIN sensors s      ON s.station_id = st.station_id
            LEFT JOIN sensor_stats ss ON ss.sensor_id = s.sensor_id
                WHERE st.geometry IS NOT NULL
                GROUP BY st.station_id, st.name, st.box_type, st.exposure,
                         st.geometry
                ORDER BY reading_count DESC
                LIMIT 5000
            """)
            rows = cur.fetchall()
        conn.close()

        features = []
        for row in rows:
            lon = row["longitude"]
            lat = row["latitude"]
            if lon is None or lat is None:
                continue
            props = {
                "station_id":   row["station_id"],
                "name":         row["name"] or row["station_id"],
                "box_type":     row["box_type"] or "—",
                "exposure":     row["exposure"] or "—",
                "sensor_count": row["sensor_count"],
                "reading_count":row["reading_count"],
              "longitude":    lon,
              "latitude":     lat,
                "earliest":     row["earliest"].isoformat() if row["earliest"] else None,
                "latest":       row["latest"].isoformat()   if row["latest"]   else None,
            }
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            })

        return jsonify({"type": "FeatureCollection", "features": features})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/station/<station_id>/sensors")
def station_sensors(station_id):
    """Sensors and recent stats for a given station."""
    try:
        conn = psycopg2.connect(connect_timeout=5, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            WITH sensor_stats AS (
              SELECT
                sensor_id,
                SUM(reading_count)::BIGINT AS reading_count,
                MIN(min_value)             AS min_val,
                MAX(max_value)             AS max_val,
                CASE
                  WHEN SUM(reading_count) > 0
                    THEN SUM(avg_value * reading_count) / SUM(reading_count)
                  ELSE NULL
                END                        AS avg_val,
                MAX(bucket) + INTERVAL '1 hour' AS latest_at
              FROM sensor_data_hourly
              GROUP BY sensor_id
            )
                SELECT
                    s.sensor_id,
                    s.title,
                    s.sensor_type,
                    s.unit,
              COALESCE(ss.reading_count, 0)::BIGINT AS reading_count,
              ss.min_val,
              ss.max_val,
              ss.avg_val,
              ss.latest_at
                FROM sensors s
            LEFT JOIN sensor_stats ss ON ss.sensor_id = s.sensor_id
                WHERE s.station_id = %s
                ORDER BY reading_count DESC
            """, (station_id,))
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        # serialise datetimes
        for r in rows:
            if r.get("latest_at"):
                r["latest_at"] = r["latest_at"].isoformat()
            for k in ("min_val", "max_val", "avg_val"):
                if r.get(k) is not None:
                    r[k] = round(float(r[k]), 3)
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary")
def api_summary():
    try:
        conn = psycopg2.connect(connect_timeout=3, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                        AS station_count,
                    SUM(CASE WHEN geometry IS NOT NULL THEN 1 END) AS mapped_count
                FROM stations
            """)
            row = dict(cur.fetchone())
        conn.close()
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── HTML ──────────────────────────────────────────────────────────────────────

MAP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSenseMap — Station Map</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&family=Unbounded:wght@400;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">

<style>
  :root {
    --bg:      #07080d;
    --panel:   #0d0f18;
    --border:  #1a1d2e;
    --accent:  #7ef7c8;
    --accent2: #5a9cf8;
    --warn:    #ffb547;
    --text:    #dde4f0;
    --muted:   #4a5270;
    --mono:    'DM Mono', monospace;
    --display: 'Unbounded', sans-serif;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }
  body::before {
    content: "";
    position: fixed;
    inset: -25% -10% auto;
    height: 55vh;
    pointer-events: none;
    background:
      radial-gradient(circle at 12% 18%, rgba(126,247,200,.18), transparent 40%),
      radial-gradient(circle at 88% 4%, rgba(90,156,248,.20), transparent 38%);
    filter: blur(18px);
    z-index: 0;
  }
  body::after {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background-image:
      linear-gradient(rgba(255,255,255,.02) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.02) 1px, transparent 1px);
    background-size: 44px 44px;
    mask-image: linear-gradient(to bottom, rgba(0,0,0,.35), transparent 65%);
    z-index: 0;
  }

  /* ── Header ── */
  header {
    height: 58px;
    flex-shrink: 0;
    background: rgba(13,15,24,.78);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(126,247,200,.14);
    display: flex; align-items: center;
    padding: 0 20px;
    gap: 18px;
    z-index: 1000;
    position: relative;
  }
  .logo {
    font-family: var(--display);
    font-size: .8rem;
    font-weight: 900;
    letter-spacing: .06em;
    color: var(--accent);
    white-space: nowrap;
  }
  .logo span { color: var(--text); }
  .hdr-stat {
    font-size: .65rem;
    color: var(--muted);
  }
  .hdr-stat strong { color: var(--text); font-weight: 500; }
  .hdr-right {
    margin-left: auto;
    display: flex; gap: 10px; align-items: center;
    flex-wrap: wrap;
  }
  .filter-group {
    display: flex; gap: 6px; align-items: center;
  }
  .filter-group label { font-size: .62rem; color: var(--muted); }
  select {
    background: rgba(7,8,13,.72);
    border: 1px solid #2a2f45;
    color: var(--text);
    font-family: var(--mono);
    font-size: .65rem;
    padding: 3px 8px;
    border-radius: 4px;
    cursor: pointer;
    outline: none;
  }
  select:focus { border-color: var(--accent); }

  /* ── Body split ── */
  .body {
    flex: 1;
    display: flex;
    overflow: hidden;
    position: relative;
    z-index: 1;
  }

  /* ── Map ── */
  #map {
    flex: 1;
    z-index: 1;
    background: #0a0c14;
  }

  /* Leaflet dark override */
  .leaflet-tile { filter: brightness(0.85) saturate(0.5) hue-rotate(185deg); }
  .leaflet-container { background: #0a0c14; }
  .leaflet-control-zoom a {
    background: var(--panel) !important;
    color: var(--text) !important;
    border-color: var(--border) !important;
  }
  .leaflet-control-zoom a:hover { background: var(--border) !important; }
  .leaflet-popup-content-wrapper {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 8px 32px rgba(0,0,0,.6);
    font-family: var(--mono);
    font-size: .75rem;
  }
  .leaflet-popup-tip { background: var(--border); }
  .leaflet-popup-close-button { color: var(--muted) !important; font-size: 16px !important; }

  /* ── Side panel ── */
  #side-panel {
    width: 0;
    flex-shrink: 0;
    background: rgba(13,15,24,.95);
    border-left: 1px solid rgba(126,247,200,.14);
    overflow: hidden;
    transition: width .3s ease;
    display: flex; flex-direction: column;
    z-index: 2;
  }
  #side-panel.open { width: 390px; }
  .panel-header {
    padding: 16px 18px 12px;
    border-bottom: 1px solid rgba(126,247,200,.12);
    flex-shrink: 0;
    position: relative;
  }
  .panel-header h2 {
    font-family: var(--display);
    font-size: .75rem;
    font-weight: 700;
    letter-spacing: .05em;
    color: var(--accent);
    margin-bottom: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .panel-header .meta {
    font-size: .65rem;
    color: var(--muted);
    display: flex; gap: 10px; flex-wrap: wrap;
  }
  .meta-item { display: flex; gap: 4px; }
  .meta-item .key { color: var(--muted); }
  .meta-item .val { color: var(--text); }
  .panel-close {
    position: absolute;
    top: 10px; right: 12px;
    background: none; border: none;
    color: var(--muted); cursor: pointer;
    font-size: 1.1rem; line-height: 1;
  }
  .panel-close:hover { color: var(--text); }
  .panel-body { flex: 1; overflow-y: auto; padding: 14px 18px; }

  .overview-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 12px;
  }
  .overview-card {
    background: rgba(7,8,13,.65);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 9px 10px;
  }
  .overview-card .label {
    color: var(--muted);
    font-size: .56rem;
    letter-spacing: .08em;
    text-transform: uppercase;
    margin-bottom: 4px;
  }
  .overview-card .value {
    color: var(--text);
    font-size: .72rem;
    line-height: 1.2;
    word-break: break-word;
  }

  .section-title {
    color: var(--muted);
    font-size: .6rem;
    letter-spacing: .1em;
    text-transform: uppercase;
    margin: 4px 0 8px;
  }

  /* Sensor rows */
  .sensor-row {
    border: 1px solid #272d44;
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 8px;
    transition: border-color .15s, transform .15s ease;
    background: rgba(6,7,11,.52);
  }
  .sensor-row:hover {
    border-color: rgba(126,247,200,.35);
    transform: translateY(-1px);
  }
  .sensor-title {
    font-size: .72rem;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 6px;
  }
  .sensor-title .badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: .6rem;
    background: rgba(90,156,248,.15);
    color: var(--accent2);
    margin-left: 6px;
  }
  .sensor-meter {
    height: 4px;
    border-radius: 99px;
    background: rgba(74,82,112,.35);
    margin: 6px 0 10px;
    overflow: hidden;
  }
  .sensor-meter .fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent2), var(--accent));
    width: 0;
  }
  .sensor-stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 4px;
  }
  .stat-cell { text-align: center; }
  .stat-cell .slabel {
    font-size: .58rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--muted);
  }
  .stat-cell .sval {
    font-size: .7rem;
    color: var(--accent);
    font-weight: 500;
  }
  .stat-cell .sval.blue { color: var(--accent2); }
  .stat-cell .sval.warn { color: var(--warn); }

  /* No-data */
  .no-data {
    padding: 30px 0;
    text-align: center;
    color: var(--muted);
    font-size: .72rem;
  }

  /* ── Loading overlay ── */
  #loading-overlay {
    position: absolute;
    inset: 0;
    background: rgba(7,8,13,.85);
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    z-index: 9999;
    gap: 16px;
    transition: opacity .4s;
  }
  #loading-overlay.hidden { opacity: 0; pointer-events: none; }
  .loading-title {
    font-family: var(--display);
    font-size: 1rem;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: .1em;
  }
  .loading-sub { font-size: .7rem; color: var(--muted); }
  .spinner-lg {
    width: 36px; height: 36px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Legend ── */
  .legend {
    position: absolute;
    bottom: 24px; left: 12px;
    z-index: 500;
    background: rgba(13,15,24,.9);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px;
    font-size: .65rem;
    backdrop-filter: blur(8px);
  }
  .legend-title { color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 6px; }
  .legend-row { display: flex; gap: 8px; align-items: center; margin-bottom: 3px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }

  @media (max-width: 980px) {
    header {
      height: auto;
      min-height: 58px;
      padding: 8px 12px;
      gap: 12px;
      flex-wrap: wrap;
    }
    .hdr-right {
      margin-left: 0;
      width: 100%;
      justify-content: space-between;
    }
    #side-panel.open {
      width: min(100vw, 420px);
    }
    .legend {
      left: 8px;
      bottom: 12px;
      padding: 8px 10px;
    }
  }
</style>
</head>
<body>

<div id="loading-overlay">
  <div class="spinner-lg"></div>
  <div class="loading-title">Loading Stations</div>
  <div class="loading-sub" id="loading-sub">Fetching data…</div>
</div>

<header>
  <div class="logo">SENSE<span>MAP</span></div>
  <div class="hdr-stat">Stations: <strong id="hdr-stations">—</strong></div>
  <div class="hdr-stat">Mapped: <strong id="hdr-mapped">—</strong></div>
  <div class="hdr-stat">Shown: <strong id="hdr-shown">—</strong></div>
  <div class="hdr-right">
    <div class="filter-group">
      <label>Exposure</label>
      <select id="filter-exposure">
        <option value="">All</option>
        <option value="outdoor">outdoor</option>
        <option value="indoor">indoor</option>
        <option value="mobile">mobile</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Box Type</label>
      <select id="filter-boxtype">
        <option value="">All</option>
        <option value="fixed">fixed</option>
        <option value="mobile">mobile</option>
      </select>
    </div>
  </div>
</header>

<div class="body">
  <div id="map"></div>
  <div id="side-panel">
    <div class="panel-header">
      <button class="panel-close" onclick="closePanel()">✕</button>
      <h2 id="panel-name">—</h2>
      <div class="meta" id="panel-meta"></div>
    </div>
    <div class="panel-body" id="panel-body">
      <div class="no-data">Select a station</div>
    </div>
  </div>
</div>

<div class="legend">
  <div class="legend-title">Readings</div>
  <div class="legend-row"><div class="legend-dot" style="background:#5a9cf8"></div> &lt; 100</div>
  <div class="legend-row"><div class="legend-dot" style="background:#7ef7c8"></div> 100–1k</div>
  <div class="legend-row"><div class="legend-dot" style="background:#ffb547"></div> 1k–10k</div>
  <div class="legend-row"><div class="legend-dot" style="background:#ff6b6b"></div> &gt; 10k</div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const fmt  = n => n == null ? '—' : Number(n).toLocaleString();
const fmtN = (n, d=2) => n == null ? '—' : parseFloat(n).toFixed(d);
const fmtDate = s => s ? new Date(s).toLocaleDateString() : '—';

// ── Map init ──────────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true, preferCanvas: true })
             .setView([20, 10], 2);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap',
  maxZoom: 18,
}).addTo(map);

// ── Marker colour by reading count ───────────────────────────────────────────
function markerColor(count) {
  if (count > 10000) return '#ff6b6b';
  if (count > 1000)  return '#ffb547';
  if (count > 100)   return '#7ef7c8';
  return '#5a9cf8';
}

function makeCircle(feature) {
  const p = feature.properties;
  const radius = Math.min(10, 4 + Math.log10((p.reading_count || 0) + 1));
  return L.circleMarker(
    [feature.geometry.coordinates[1], feature.geometry.coordinates[0]],
    {
      radius,
      fillColor:   markerColor(p.reading_count),
      fillOpacity: 0.82,
      color:       'rgba(0,0,0,.3)',
      weight:      1,
    }
  );
}

// ── Data + layers ─────────────────────────────────────────────────────────────
let allFeatures = [];
let geoLayer    = null;

function buildLayer(features) {
  if (geoLayer) map.removeLayer(geoLayer);
  document.getElementById('hdr-shown').textContent = fmt(features.length);

  geoLayer = L.geoJSON({ type: 'FeatureCollection', features }, {
    pointToLayer: (f) => makeCircle(f),
    onEachFeature: (f, layer) => {
      const p = f.properties;
      layer.bindTooltip(
        `<strong>${p.name}</strong><br>${fmt(p.reading_count)} readings`,
        { direction: 'top', offset: [0, -4] }
      );
      layer.on('click', () => openPanel(p.station_id, p));
    }
  }).addTo(map);
}

function applyFilters() {
  const exp = document.getElementById('filter-exposure').value.toLowerCase();
  const box = document.getElementById('filter-boxtype').value.toLowerCase();
  const filtered = allFeatures.filter(f => {
    const p = f.properties;
    if (exp && (p.exposure || '').toLowerCase() !== exp) return false;
    if (box && (p.box_type || '').toLowerCase() !== box) return false;
    return true;
  });
  buildLayer(filtered);
}

document.getElementById('filter-exposure').addEventListener('change', applyFilters);
document.getElementById('filter-boxtype').addEventListener('change', applyFilters);

// ── Side panel ────────────────────────────────────────────────────────────────
function closePanel() {
  document.getElementById('side-panel').classList.remove('open');
}

async function openPanel(stationId, props) {
  const panel = document.getElementById('side-panel');
  panel.classList.add('open');

  document.getElementById('panel-name').textContent = props.name;
  document.getElementById('panel-meta').innerHTML = `
    <span class="meta-item"><span class="key">id</span><span class="val">${props.station_id}</span></span>
    <span class="meta-item"><span class="key">type</span><span class="val">${props.box_type}</span></span>
    <span class="meta-item"><span class="key">exposure</span><span class="val">${props.exposure}</span></span>
    <span class="meta-item"><span class="key">coverage</span><span class="val">${fmtDate(props.earliest)} → ${fmtDate(props.latest)}</span></span>
  `;
  document.getElementById('panel-body').innerHTML =
    '<div class="no-data"><div class="spinner-lg" style="margin:auto"></div></div>';

  try {
    const r   = await fetch(`/api/station/${stationId}/sensors`);
    const sensors = await r.json();

    if (!sensors.length) {
      document.getElementById('panel-body').innerHTML =
        '<div class="no-data">No sensor data yet</div>';
      return;
    }

    const maxReading = sensors.reduce(
      (max, s) => Math.max(max, Number(s.reading_count || 0)),
      1
    );

    document.getElementById('panel-body').innerHTML = `
      <div class="section-title">Station Overview</div>
      <div class="overview-grid">
        <div class="overview-card">
          <div class="label">Sensors</div>
          <div class="value">${fmt(props.sensor_count)}</div>
        </div>
        <div class="overview-card">
          <div class="label">Readings</div>
          <div class="value">${fmt(props.reading_count)}</div>
        </div>
        <div class="overview-card">
          <div class="label">First Reading</div>
          <div class="value">${fmtDate(props.earliest)}</div>
        </div>
        <div class="overview-card">
          <div class="label">Last Reading</div>
          <div class="value">${fmtDate(props.latest)}</div>
        </div>
        <div class="overview-card" style="grid-column: 1 / -1;">
          <div class="label">Coordinates</div>
          <div class="value">${fmtN(props.latitude, 4)}, ${fmtN(props.longitude, 4)}</div>
        </div>
      </div>
      <div class="section-title">Sensors</div>
      ${sensors.map(s => {
        const pct = Math.max(2, Math.round((Number(s.reading_count || 0) / maxReading) * 100));
        return `
      <div class="sensor-row">
        <div class="sensor-title">
          ${s.title || '—'}
          <span class="badge">${s.sensor_type || '—'}</span>
          ${s.unit ? `<span style="color:var(--muted);font-size:.62rem">${s.unit}</span>` : ''}
        </div>
        <div class="sensor-meter"><div class="fill" style="width:${pct}%"></div></div>
        <div class="sensor-stats">
          <div class="stat-cell">
            <div class="slabel">Min</div>
            <div class="sval">${fmtN(s.min_val)}</div>
          </div>
          <div class="stat-cell">
            <div class="slabel">Avg</div>
            <div class="sval">${fmtN(s.avg_val)}</div>
          </div>
          <div class="stat-cell">
            <div class="slabel">Max</div>
            <div class="sval warn">${fmtN(s.max_val)}</div>
          </div>
          <div class="stat-cell">
            <div class="slabel">Readings</div>
            <div class="sval blue">${fmt(s.reading_count)}</div>
          </div>
        </div>
      </div>
    `;
      }).join('')}
    `;
  } catch(e) {
    document.getElementById('panel-body').innerHTML =
      `<div class="no-data">Error: ${e.message}</div>`;
  }
}

// ── Load data ─────────────────────────────────────────────────────────────────
(async () => {
  try {
    const [sumR, geoR] = await Promise.all([
      fetch('/api/summary'),
      fetch('/api/stations/geo'),
    ]);
    const summary = await sumR.json();
    const geo     = await geoR.json();

    document.getElementById('hdr-stations').textContent = fmt(summary.station_count);
    document.getElementById('hdr-mapped').textContent   = fmt(summary.mapped_count);
    document.getElementById('loading-sub').textContent  =
      `Rendering ${geo.features?.length || 0} stations…`;

    allFeatures = geo.features || [];
    buildLayer(allFeatures);

    if (allFeatures.length > 0) {
      try { map.fitBounds(geoLayer.getBounds(), { padding: [20, 20] }); }
      catch(_) {}
    }

    document.getElementById('loading-overlay').classList.add('hidden');

  } catch(e) {
    document.getElementById('loading-sub').textContent = 'Error: ' + e.message;
  }
})();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(MAP_HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5052, debug=False)
