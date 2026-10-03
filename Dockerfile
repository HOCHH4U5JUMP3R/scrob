# ── Stage 1: Build frontend ───────────────────────────────────────────────────
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# ── Stage 2: Runtime (Python + Node + supervisord) ────────────────────────────
FROM python:3.12-slim

# Keep locally built images on the same release line as the fork base.  The
# release workflow overrides this with the newly published semantic version.
ARG APP_VERSION=2.17.0
ENV APP_VERSION=${APP_VERSION}
ENV TZ=UTC

# Install Node.js 22, supervisord, gosu, curl and tzdata
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    supervisor \
    tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ── Backend ───────────────────────────────────────────────────────────────────
WORKDIR /app/backend

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-cache
# PSNAWP is used by the optional PlayStation integration. Keep it installed in
# the same virtualenv used by the backend runtime; backend/requirements.txt is
# not used by the production image build.
RUN uv pip install --python /app/backend/.venv/bin/python psnawp==3.0.3
RUN uv pip install --python /app/backend/.venv/bin/python xbox-webapi==2.1.0

COPY backend/ .

# ── Frontend ──────────────────────────────────────────────────────────────────
WORKDIR /app/frontend

COPY --from=frontend-builder /app/frontend/dist ./dist
COPY --from=frontend-builder /app/frontend/node_modules ./node_modules
COPY --from=frontend-builder /app/frontend/package.json ./

# ── Entrypoint & supervisor config ────────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
COPY supervisord.conf /etc/supervisor/conf.d/scrob.conf
RUN chmod +x /entrypoint.sh

EXPOSE 7330

# Requires Docker Engine 25+ for --start-interval
HEALTHCHECK --interval=2m --timeout=2s --start-period=20s --start-interval=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${BACKEND_PORT:-7331}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
