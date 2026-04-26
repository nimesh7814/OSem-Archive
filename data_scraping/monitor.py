"""
Scraper Progress Monitor — http://localhost:5051
Reads progress.json and queries the DB for live stats.
"""

import os
import json
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string

import psycopg2
import psycopg2.extras

PROGRESS_FILE = os.getenv("PROGRESS_FILE", "/data/progress.json")
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "opensensemap"),
    "user":     os.getenv("DB_USER",     "osm"),
    "password": os.getenv("DB_PASSWORD", "changeme"),
}

app = Flask(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def db_stats() -> dict:
    try:
        conn = psycopg2.connect(connect_timeout=3, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*)  FROM stations)            AS station_count,
                    (SELECT COUNT(*)  FROM sensors)             AS sensor_count,
          (SELECT COALESCE(SUM(reading_count), 0) FROM sensor_data_hourly)
                             AS reading_count,
                    (SELECT COUNT(*)  FROM _scraper_processed_dates) AS dates_done,
          (SELECT MIN(bucket) FROM sensor_data_hourly) AS earliest,
          (SELECT MAX(bucket) + INTERVAL '1 hour'
           FROM sensor_data_hourly)                    AS latest
            """)
            row = cur.fetchone()
        conn.close()
        result = dict(row)
        # serialise datetimes
        for k in ("earliest", "latest"):
            if result.get(k):
                result[k] = result[k].isoformat()
        result["db_ok"] = True
        return result
    except Exception as e:
        return {"db_ok": False, "error": str(e)}


def sensor_breakdown() -> list:
    """Top sensor types by count."""
    try:
        conn = psycopg2.connect(connect_timeout=3, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT title, sensor_type, unit, COUNT(*) AS sensor_count
                FROM sensors
                GROUP BY title, sensor_type, unit
                ORDER BY sensor_count DESC
                LIMIT 30
            """)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def station_breakdown() -> list:
    """Stations with reading counts."""
    try:
        conn = psycopg2.connect(connect_timeout=3, **DB_CONFIG)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
        WITH sensor_stats AS (
          SELECT
            sensor_id,
            SUM(reading_count)::BIGINT AS reading_count
          FROM sensor_data_hourly
          GROUP BY sensor_id
        )
                SELECT
                    st.station_id,
                    st.name,
                    st.box_type,
                    st.exposure,
                    COUNT(DISTINCT s.sensor_id) AS sensor_count,
          COALESCE(SUM(ss.reading_count), 0)::BIGINT AS reading_count
                FROM stations st
        LEFT JOIN sensors s      ON s.station_id  = st.station_id
        LEFT JOIN sensor_stats ss ON ss.sensor_id = s.sensor_id
                GROUP BY st.station_id, st.name, st.box_type, st.exposure
                ORDER BY reading_count DESC
                LIMIT 100
            """)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    progress = load_progress()
    stats    = db_stats()
    return jsonify({"progress": progress, "db": stats})


@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_breakdown())


@app.route("/api/stations")
def api_stations():
    return jsonify(station_breakdown())


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSenseMap — Scraper Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Azeret+Mono:wght@300;400;600;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:      #0a0c10;
    --surface: #111318;
    --border:  #1e2230;
    --accent:  #00e5a0;
    --accent2: #3d8ef8;
    --warn:    #f5a623;
    --danger:  #ff4d6d;
    --text:    #e2e8f0;
    --muted:   #5a6480;
    --mono:    'Azeret Mono', monospace;
    --sans:    'Syne', sans-serif;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { font-size: 14px; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    padding: 0 0 60px;
  }

  /* ── Top bar ── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(10,12,16,0.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 0 28px;
    height: 56px;
    display: flex; align-items: center; gap: 16px;
  }
  header .logo {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 1.1rem;
    letter-spacing: -0.02em;
    color: var(--accent);
    white-space: nowrap;
  }
  header .logo span { color: var(--text); }
  #status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
    box-shadow: 0 0 0 0 var(--muted);
    transition: background .3s;
  }
  #status-dot.running {
    background: var(--accent);
    animation: pulse 1.6s ease-in-out infinite;
  }
  #status-dot.completed { background: var(--accent2); }
  #status-dot.stopped   { background: var(--warn); }
  #status-dot.error     { background: var(--danger); }
  @keyframes pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(0,229,160,.5); }
    50%      { box-shadow: 0 0 0 6px rgba(0,229,160,0); }
  }
  #status-label {
    font-size: .75rem;
    font-weight: 600;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  header .updated {
    margin-left: auto;
    font-size: .7rem;
    color: var(--muted);
  }

  /* ── Layout ── */
  main { padding: 24px 28px 0; max-width: 1400px; }

  /* ── KPI grid ── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    display: flex; flex-direction: column; gap: 4px;
  }
  .kpi .label {
    font-size: .65rem;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
  }
  .kpi .value {
    font-size: 1.7rem;
    font-weight: 700;
    font-family: var(--sans);
    line-height: 1;
    color: var(--accent);
  }
  .kpi .sub { font-size: .68rem; color: var(--muted); margin-top: 2px; }
  .kpi.blue .value  { color: var(--accent2); }
  .kpi.warn .value  { color: var(--warn); }
  .kpi.plain .value { color: var(--text); }

  /* ── Progress bar ── */
  .progress-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .progress-section h2 {
    font-family: var(--sans);
    font-size: .8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .bar-row {
    display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
  }
  .bar-label { width: 90px; font-size: .7rem; color: var(--muted); flex-shrink: 0; }
  .bar-track {
    flex: 1; height: 8px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 4px;
    background: var(--accent);
    transition: width .5s ease;
    min-width: 2px;
  }
  .bar-fill.blue  { background: var(--accent2); }
  .bar-fill.warn  { background: var(--warn); }
  .bar-pct { width: 44px; font-size: .7rem; text-align: right; color: var(--text); }

  /* ── Message ── */
  #message-box {
    font-size: .78rem;
    padding: 10px 14px;
    background: rgba(0,229,160,.06);
    border-left: 3px solid var(--accent);
    border-radius: 0 4px 4px 0;
    color: var(--text);
    margin-top: 12px;
    min-height: 36px;
  }

  /* ── Two-col layout for tables ── */
  .two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }
  @media(max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

  /* ── Tables ── */
  .table-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .table-card h2 {
    font-family: var(--sans);
    font-size: .8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
    padding: 16px 18px 12px;
    border-bottom: 1px solid var(--border);
  }
  .table-wrap { overflow-x: auto; max-height: 400px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; }
  th {
    position: sticky; top: 0;
    background: var(--surface);
    font-size: .65rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--muted);
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 7px 12px;
    font-size: .72rem;
    border-bottom: 1px solid rgba(30,34,48,.5);
    white-space: nowrap;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,.03); }
  .num { text-align: right; color: var(--accent); font-weight: 600; }
  .num.blue { color: var(--accent2); }
  .badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 3px;
    font-size: .62rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    background: rgba(0,229,160,.12);
    color: var(--accent);
  }
  .badge.blue { background: rgba(61,142,248,.12); color: var(--accent2); }
  .badge.warn { background: rgba(245,166,35,.12); color: var(--warn); }

  /* ── Spinner ── */
  .loading {
    display: flex; align-items: center; justify-content: center;
    padding: 40px;
    color: var(--muted);
    font-size: .75rem;
    gap: 10px;
  }
  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <div class="logo">OpenSense<span>Map</span></div>
  <div id="status-dot"></div>
  <div id="status-label">connecting…</div>
  <div class="updated" id="updated-label"></div>
</header>

<main>

  <!-- KPIs -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="loading"><div class="spinner"></div> Loading…</div>
  </div>

  <!-- Progress bars -->
  <div class="progress-section">
    <h2>Scraper Progress</h2>
    <div class="bar-row">
      <span class="bar-label">Dates</span>
      <div class="bar-track"><div class="bar-fill" id="bar-dates" style="width:0%"></div></div>
      <span class="bar-pct" id="pct-dates">—</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">Stations</span>
      <div class="bar-track"><div class="bar-fill blue" id="bar-stations" style="width:0%"></div></div>
      <span class="bar-pct" id="pct-stations">—</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">Sensors</span>
      <div class="bar-track"><div class="bar-fill warn" id="bar-sensors" style="width:0%"></div></div>
      <span class="bar-pct" id="pct-sensors">—</span>
    </div>
    <div id="message-box">—</div>
  </div>

  <!-- Tables -->
  <div class="two-col">
    <div class="table-card">
      <h2>Top Sensor Types</h2>
      <div class="table-wrap">
        <table id="sensor-table">
          <thead><tr><th>Title</th><th>Type</th><th>Unit</th><th class="num">Count</th></tr></thead>
          <tbody><tr><td colspan="4" class="loading"><div class="spinner"></div></td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="table-card">
      <h2>Top Stations by Readings</h2>
      <div class="table-wrap">
        <table id="station-table">
          <thead><tr><th>Station</th><th>Type</th><th>Exposure</th><th class="num">Sensors</th><th class="num blue">Readings</th></tr></thead>
          <tbody><tr><td colspan="5" class="loading"><div class="spinner"></div></td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

</main>

<script>
const fmt = n => n == null ? '—' : Number(n).toLocaleString();
const pct = (a, b) => b > 0 ? ((a / b) * 100).toFixed(1) + '%' : '—';

function setBar(id, pctId, done, total) {
  const p = total > 0 ? Math.min((done / total) * 100, 100) : 0;
  document.getElementById(id).style.width = p + '%';
  document.getElementById(pctId).textContent = total > 0 ? p.toFixed(1) + '%' : '—';
}

function statusClass(s) {
  if (!s) return '';
  if (s === 'running')   return 'running';
  if (s === 'completed') return 'completed';
  if (s === 'stopped')   return 'stopped';
  return 'error';
}

async function refreshStatus() {
  const r = await fetch('/api/status');
  const { progress: p, db } = await r.json();

  // Status dot
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  const sc    = statusClass(p.status);
  dot.className   = sc;
  label.textContent = p.status || 'unknown';
  document.getElementById('updated-label').textContent =
    'updated ' + new Date().toLocaleTimeString();

  // KPIs
  const stats = p.stats || {};
  const kpis = [
    { label: 'Dates Done',      value: fmt(stats.dates_done),        sub: `of ${fmt(stats.dates_total)}`,           cls: '' },
    { label: 'Dates Skipped',   value: fmt(stats.dates_skipped),     sub: 'already processed',                      cls: 'plain' },
    { label: 'Stations Seen',   value: fmt(stats.stations_total_seen), sub: `${fmt(stats.stations_done)} committed`, cls: 'blue' },
    { label: 'Sensors Done',    value: fmt(stats.sensors_done),      sub: `of ${fmt(stats.sensors_total)}`,         cls: 'warn' },
    { label: 'Readings Inserted', value: fmt(stats.readings_inserted), sub: `${fmt(stats.readings_valid)} valid`,   cls: '' },
    { label: 'CSVs Downloaded', value: fmt(stats.csv_downloaded),    sub: `${fmt(stats.csv_missing)} missing`,      cls: 'plain' },
    { label: 'DB Stations',     value: fmt(db.station_count),        sub: 'in database',                            cls: 'blue' },
    { label: 'DB Sensors',      value: fmt(db.sensor_count),         sub: 'in database',                            cls: 'warn' },
    { label: 'DB Readings',     value: fmt(db.reading_count),        sub: 'total rows',                             cls: '' },
  ];
  document.getElementById('kpi-grid').innerHTML = kpis.map(k => `
    <div class="kpi ${k.cls}">
      <span class="label">${k.label}</span>
      <span class="value">${k.value}</span>
      <span class="sub">${k.sub}</span>
    </div>`).join('');

  // Progress bars
  setBar('bar-dates',    'pct-dates',    stats.dates_done    || 0, stats.dates_total         || 0);
  setBar('bar-stations', 'pct-stations', stats.stations_done || 0, stats.stations_total_seen || 0);
  setBar('bar-sensors',  'pct-sensors',  stats.sensors_done  || 0, stats.sensors_total       || 0);

  document.getElementById('message-box').textContent = p.message || '—';
}

async function refreshSensors() {
  const r    = await fetch('/api/sensors');
  const rows = await r.json();
  const tbody = document.querySelector('#sensor-table tbody');
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.title || '—'}</td>
      <td><span class="badge">${r.sensor_type || '—'}</span></td>
      <td>${r.unit || '—'}</td>
      <td class="num">${fmt(r.sensor_count)}</td>
    </tr>`).join('') || '<tr><td colspan="4" style="color:var(--muted);padding:20px">No data yet</td></tr>';
}

async function refreshStations() {
  const r    = await fetch('/api/stations');
  const rows = await r.json();
  const tbody = document.querySelector('#station-table tbody');
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td title="${r.station_id}">${r.name || r.station_id}</td>
      <td><span class="badge blue">${r.box_type || '—'}</span></td>
      <td>${r.exposure || '—'}</td>
      <td class="num">${fmt(r.sensor_count)}</td>
      <td class="num blue">${fmt(r.reading_count)}</td>
    </tr>`).join('') || '<tr><td colspan="5" style="color:var(--muted);padding:20px">No data yet</td></tr>';
}

async function refresh() {
  try { await refreshStatus();  } catch(e) { console.warn('status',  e); }
  try { await refreshSensors(); } catch(e) { console.warn('sensors', e); }
  try { await refreshStations();} catch(e) { console.warn('stations',e); }
}

refresh();
setInterval(refreshStatus,  5000);
setInterval(refreshSensors, 30000);
setInterval(refreshStations,30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051, debug=False)
