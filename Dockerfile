# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# --- Build the virtualenv with uv (locked dependencies) -------------------------------
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra browser --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra browser --no-editable

# --- Runtime ---------------------------------------------------------------------------
FROM base AS runtime
# Headless Chromium for interactive logins and authenticated pages (~0.8 GB).
# Build with --build-arg INSTALL_BROWSER=false for a slim image without sessions' browser.
ARG INSTALL_BROWSER=true
COPY --from=builder /opt/venv /opt/venv
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        playwright install --with-deps --only-shell chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

USER app
WORKDIR /home/app
ENV HOST=0.0.0.0 \
    PORT=8000 \
    SESSIONS_DB_PATH=/data/sessions.db \
    BROWSER_ENABLED=${INSTALL_BROWSER}
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["search-extract-api"]
