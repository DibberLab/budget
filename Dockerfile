# Multi-stage build: install deps in a builder layer, then copy a slim runtime.
# This keeps the final image small and free of build tooling.

FROM python:3.12-slim AS builder
WORKDIR /build

# Install build deps that some Python wheels still need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --prefix is the easiest way to capture installed packages for the next stage.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime
WORKDIR /app

# Runtime needs sqlite3 (for /backup.sh's `.backup` command) and curl (healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 budget

COPY --from=builder /install /usr/local
COPY --chown=budget:budget . /app

# /data is mounted from a Docker volume on the host; SQLite lives there.
RUN mkdir -p /data /app/instance \
    && chown -R budget:budget /data /app

USER budget

ENV DATABASE_URL=sqlite:////data/budget.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=app.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# 2 sync workers is plenty for a 2-user household. Bind to all interfaces on
# 8000 so Caddy can reach us over the Docker network.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--forwarded-allow-ips", "*", \
     "app:app"]
