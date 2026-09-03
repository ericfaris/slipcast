FROM python:3.12-slim

# Node 22+ is required by yt-dlp's EJS challenge solver (the older Debian
# nodejs package is too old and yt-dlp reports it as "unsupported").
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV DATA_DIR=/data

EXPOSE 8000

# curl is installed above (kept in the final image — no separate build stage
# to prune it from) so it's fine to use directly here rather than reaching
# for a python -c/urllib one-liner. /health/live returns non-2xx (503) when
# degraded, which curl -f turns into a failing exit code.
# Deliberately /health/live, not /health: /health/live reports only conditions
# a restart could plausibly fix. The full /health also fails on expired cookies
# and low disk, which no restart repairs — pointing the healthcheck there would
# mark the container unhealthy for weeks at a time and, with an external
# restarter watching that status (see ops/README.md), restart-loop it.
HEALTHCHECK --interval=1m --timeout=10s --start-period=2m --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
