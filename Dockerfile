# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app


# Local development: all dependency groups, editable install, source mounted by Compose.
FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --locked
EXPOSE 8000
CMD ["uvicorn", "--factory", "linuxlab.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "65536", "--reload", "--reload-dir", "src"]


FROM base AS build
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.12-slim AS prod
ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN useradd --system --uid 10001 --no-create-home linuxlab
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
USER linuxlab
EXPOSE 8000
CMD ["uvicorn", "--factory", "linuxlab.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "65536"]
