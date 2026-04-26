FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
COPY monitor.py .
COPY map_viewer.py .

# /data is a named volume — holds progress.json across restarts
VOLUME ["/data"]

# Default command runs the scraper.
# The dashboard services are separate compose targets with their own CMD.
CMD ["python", "-u", "scraper.py"]
