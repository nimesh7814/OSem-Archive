FROM python:3.12-slim

# ── Working directory ────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────
# PyPI is reachable from inside the container (only apt mirrors
# are blocked). Install directly — no apt-get needed since all
# packages ship as pure-Python or manylinux binary wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# ── Application code ─────────────────────────────────────────
COPY schema.sql .
COPY sensor_types.py .
COPY indexer.py .

# ── Non-root user for security ───────────────────────────────
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# ── Healthcheck ──────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import asyncpg, asyncio, os; asyncio.run(asyncpg.connect(os.getenv('DATABASE_URL')))" \
    || exit 1

# ── Default command ──────────────────────────────────────────
CMD ["python", "indexer.py", "--mode", "scheduler"]
