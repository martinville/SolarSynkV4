FROM ghcr.io/home-assistant/base-python:3.12-alpine3.24
LABEL io.hass.version="0.1.15" io.hass.type="app" io.hass.arch="amd64|aarch64"
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY app /app/app
CMD ["gunicorn", "--bind", "0.0.0.0:8099", "--workers", "1", "--threads", "4", "--timeout", "90", "app.main:app"]
