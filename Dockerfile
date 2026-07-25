FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8200 \
    SPREADBOARD_REFRESH_SECONDS=300 \
    SPREADBOARD_DATA_DIR=/app/runtime \
    SPREADBOARD_PUBLIC_MODE=1 \
    SPREADBOARD_LIGHTWEIGHT_MODE=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY spreadboard ./spreadboard
COPY src ./src
COPY scripts ./scripts
COPY data/api_discovery_watchlist.json data/api_discovery_identity_registry.json data/api_discovery_executor_attestations.json ./data/

RUN uv sync --frozen --no-dev \
    && groupadd --system spreadboard \
    && useradd --system --gid spreadboard --home-dir /app --shell /usr/sbin/nologin spreadboard \
    && mkdir -p /app/runtime \
    && chown -R spreadboard:spreadboard /app/runtime

EXPOSE 8200
USER spreadboard
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8200')}/api/health\", timeout=4)"]
CMD ["/app/.venv/bin/python", "scripts/run_spreadboard_service.py"]
