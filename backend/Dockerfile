# backend/Dockerfile
# FastAPI Archive API image

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directory for async export files (overridable via EXPORT_DIR env var)
RUN mkdir -p /tmp/osem_exports

# api/main.py does `from functions.xxx import ...` (needs api/ on the path so
# "functions" resolves as a package), while the modules inside functions/
# import each other as bare top-level names, e.g. `from connection import
# ...` in functions/db_query.py (needs functions/ itself on the path too).
ENV PYTHONPATH=/app/api:/app/api/functions

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
