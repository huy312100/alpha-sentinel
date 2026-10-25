# syntax=docker/dockerfile:1
# =============================================================================
# AlphaSentinel - one image, two entrypoints (bot + dashboard).
# =============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    HOT_DB_PATH=/app/data/hot/alpha_sentinel.db \
    PARQUET_ROOT=/app/data/cold

WORKDIR /app

# curl is only here for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user; the data volume is chowned so the lake is writable.
RUN useradd --create-home --uid 10001 sentinel \
    && mkdir -p /app/data/hot /app/data/cold \
    && chown -R sentinel:sentinel /app/data
USER sentinel

HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD python -m bot.main --status > /dev/null || exit 1

# Default: the long-running scanner loop. The dashboard overrides this command.
CMD ["python", "-m", "bot.main"]
