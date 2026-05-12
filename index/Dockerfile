# =============================================================
# PostgreSQL 16 + PostGIS 3.5 + TimescaleDB
# Uses the official PostGIS image and installs TimescaleDB
# =============================================================
FROM postgis/postgis:16-3.5

# Install TimescaleDB using the official timescale install script
# which handles dependency resolution automatically
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && curl -fsSL https://install.timescale.com/timescaledb-apt-repo.gpg \
        | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg arch=$(dpkg --print-architecture)] \
        https://packagecloud.io/timescale/timescaledb/debian/ $(. /etc/os-release && echo $VERSION_CODENAME) main" \
        > /etc/apt/sources.list.d/timescaledb.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        timescaledb-2-postgresql-16 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Preload TimescaleDB (required)
RUN echo "shared_preload_libraries = 'timescaledb'" >> /usr/share/postgresql/16/postgresql.conf.sample
