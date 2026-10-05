# One image for every Recorder service (`nova-rec:dev`): migrate, recorder, API and checks.
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM python:3.12.14-slim
COPY --from=uv /uv /uvx /usr/local/bin/

# The venv lives outside /repo so the rec-check bind mount replaces the code, not the venv.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    MYPY_CACHE_DIR=/tmp/mypy-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    PATH=/opt/venv/bin:$PATH

RUN useradd --create-home --uid 10001 rec \
    && mkdir -p /opt/venv /repo/backend \
    && chown rec:rec /opt/venv /repo/backend

WORKDIR /repo/backend
COPY --chown=rec:rec . .
USER rec
RUN uv sync --frozen && rm -rf /tmp/uv-cache

EXPOSE 8000
CMD ["python", "-m", "nova_rec", "recorder"]
