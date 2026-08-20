# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM node:26.4.0-bookworm-slim AS console-build
WORKDIR /source/console
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.29 AS uv

FROM cgr.dev/chainguard/python:latest-dev@sha256:cd42e3e78f19faffe161fccf60af83503ee3851dd12efdae7d2488148e2fcd49 AS python-build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM cgr.dev/chainguard/python:latest@sha256:53757bfb153c99eb7005963b7e4ea3a8ba488badceab8487d3ba982ad54f2047 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY --from=python-build --chown=65532:65532 /app/.venv ./.venv
COPY --chown=65532:65532 services/ ./services/
COPY --chown=65532:65532 scenarios/ ./scenarios/
USER 65532:65532
EXPOSE 8080
ENTRYPOINT []

FROM runtime AS data-plane
CMD ["/app/.venv/bin/uvicorn", "services.data_plane:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM runtime AS provider-simulator
CMD ["/app/.venv/bin/uvicorn", "services.provider_simulator:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM runtime AS console
COPY --from=console-build --chown=65532:65532 /source/console/dist ./console/dist
CMD ["/app/.venv/bin/uvicorn", "services.control_plane:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
