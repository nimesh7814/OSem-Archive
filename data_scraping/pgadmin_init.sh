#!/bin/sh
set -eu

umask 077
cat > /var/lib/pgadmin/pgpass <<EOF
${DB_HOST}:${DB_PORT}:${DB_NAME}:${DB_USER}:${DB_PASSWORD}
EOF

exec /entrypoint.sh
