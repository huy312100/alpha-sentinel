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

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user; the data volume is chowned so the lake is writable.
RUN useradd --create-home --uid 10001 sentinel \
    && mkdir -p /app/data/hot /app/data/cold \
    && chown -R sentinel:sentinel /app/data
USER sentinel

# The healthcheck asks a different question than --status: is the scan loop still
# producing cycles? It reads the newest run row only, so it stays cheap and cannot
# report healthy while the bot is alive but blind. Exit 1 -> unhealthy -> restart.
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD python -m bot.main --health

# Default: the long-running scanner loop. The dashboard overrides this command.
CMD ["python", "-m", "bot.main"]
