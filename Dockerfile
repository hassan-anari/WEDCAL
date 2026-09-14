FROM ghcr.io/astral-sh/uv:0.7.22 AS uv

FROM python:3.13-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# pip's vendored CycloneDX inventory declares msgpack/setuptools even though they are not installed
# distributions. Dependency installation is complete in the builder; omit installer code at runtime.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && python -m pip uninstall --yes pip \
    && rm -rf /root/.cache/pip /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/run \
    && chown app:app /app/run
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . .
RUN DJANGO_SETTINGS_MODULE=dexo_land.settings.test POSTGRES_HOST=localhost \
    python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["uvicorn", "dexo_land.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
