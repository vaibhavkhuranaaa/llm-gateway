# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM node:26.4.0-bookworm-slim AS console-build
WORKDIR /source/console
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.29 AS uv

FROM python:3.12.13-alpine3.23 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN apk upgrade --no-cache \
    && uv sync --frozen --no-dev --no-install-project \
    && rm /bin/uv /bin/uvx \
    && apk del --no-cache .python-rundeps \
    && apk add --no-cache gdbm libbz2 libcrypto3 libffi libncursesw libnsl libpanelw libssl3 libtirpc libuuid ncurses-terminfo-base readline xz-libs zlib \
    && rm -f /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so \
    && addgroup -S -g 65532 app \
    && adduser -S -D -H -u 65532 -G app app
COPY --chown=65532:65532 services/ ./services/
COPY --chown=65532:65532 scenarios/ ./scenarios/
USER 65532:65532
EXPOSE 8080

FROM runtime AS data-plane
CMD ["uvicorn", "services.data_plane:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM runtime AS provider-simulator
CMD ["uvicorn", "services.provider_simulator:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM runtime AS console
COPY --from=console-build --chown=65532:65532 /source/console/dist ./console/dist
CMD ["uvicorn", "services.control_plane:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
