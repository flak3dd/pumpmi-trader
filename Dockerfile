# Build the root Vite frontend with the package-manager version declared by package.json.
FROM node:22-bookworm-slim AS frontend
WORKDIR /app

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
RUN corepack enable && corepack prepare pnpm@11.25.0 --activate \
    && pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# Runtime database credentials are supplied by hosting. No DSN is baked into this image.
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUMPMI_ENV=production \
    PUMPMI_OPTIMIZER_ENABLED=1 \
    PUMPMI_WORKER_ENABLED=0 \
    HOST=0.0.0.0 \
    PORT=3000

COPY pump-board-engine/requirements.txt /tmp/pump-board-requirements.txt
RUN pip install --no-cache-dir -r /tmp/pump-board-requirements.txt

COPY --from=frontend /app/dist ./dist
COPY scripts ./scripts
COPY public ./public
COPY pump-board-engine ./pump-board-engine

EXPOSE 3000
CMD ["python3", "scripts/serve.py"]
