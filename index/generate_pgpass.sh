#!/bin/bash
# Generate pgpass file from environment variables
# This ensures pgAdmin password file stays in sync with .env credentials

set -e

# Use /var/lib/pgadmin (pgAdmin's data directory with guaranteed write access)
PGPASS_DIR="/var/lib/pgadmin"
PGPASS_FILE="${PGPASS_DIR}/.pgpass"
PGPASS_MODE=0600

# Default values (these come from .env via docker-compose)
POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-osem_db}"
POSTGRES_USER="${POSTGRES_USER:-osem_user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-osem_db_lock}"

# Ensure directory exists
mkdir -p "${PGPASS_DIR}"

# Generate pgpass
echo "Generating pgpass at ${PGPASS_FILE}..."
echo "${POSTGRES_HOST}:${POSTGRES_PORT}:${POSTGRES_DB}:${POSTGRES_USER}:${POSTGRES_PASSWORD}" > "${PGPASS_FILE}"
chmod "${PGPASS_MODE}" "${PGPASS_FILE}"

echo "✓ pgpass file created at ${PGPASS_FILE} with permissions ${PGPASS_MODE}"
echo "  Credentials: ${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
