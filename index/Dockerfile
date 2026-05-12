FROM python:3.12-slim

# ── System dependencies ──────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────
# Copy requirements first to leverage Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────
COPY schema.sql .
COPY indexer.py .

# ── Non-root user for security ───────────────────────────────
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# ── Healthcheck ──────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import asyncpg, asyncio, os; asyncio.run(asyncpg.connect(os.getenv('DATABASE_URL')))" \
    || exit 1

# ── Default command: run as persistent scheduler ─────────────
CMD ["python", "indexer.py", "--mode", "scheduler"]
