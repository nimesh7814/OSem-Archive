# backend/Dockerfile
# FastAPI Archive API image

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directory for async export files (overridable via EXPORT_DIR env var)
RUN mkdir -p /tmp/osem_exports

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
