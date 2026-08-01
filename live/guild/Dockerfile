# Agent Guild — live public API.
# Build context is the REPO ROOT (render.yaml: dockerContext: .). It used to be
# this directory, which could not see docs/ — so the git-committed checkpoint
# pin, whose entire purpose is to survive a wiped disk, was never in the image
# and the canonical floor was empty in production (2.0.3 /health reported
# floor_checkpoint_index=-1, floor_sources=[]). Persists to a mounted disk at
# /data so reputation, accounts and the credit ledger survive restarts.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching. `stripe` is only needed for live card
# payments; it is imported lazily, so the service runs without it.
COPY live/guild/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY live/guild/app ./app

# THE CANONICAL FLOOR PIN. One file, copied — not duplicated: the single
# authoritative copy stays at docs/checkpoints/latest.json in the repo and is
# regenerated there. It is issuer-scoped at runtime, so it only ever constrains
# the deployment whose identity actually published it; a fork or staging stack
# with its own DID ignores it. It must land at ./docs/... (i.e. /app/docs/...)
# so the runtime lookup finds it beside the app in the image layout.
COPY docs/checkpoints/latest.json ./docs/checkpoints/latest.json

ENV GUILD_DATA=/data/guild.json
ENV PORT=8000
EXPOSE 8000

# Create the data dir at runtime (the disk is mounted at /data).
# --proxy-headers + --forwarded-allow-ips '*' make uvicorn trust the platform
# proxy's X-Forwarded-Proto header, so it knows requests are HTTPS. Without this
# uvicorn assumes http and emits http:// redirects (e.g. /mcp -> http://.../mcp/),
# a scheme downgrade that breaks MCP scanners/clients that won't follow it.
CMD ["sh", "-c", "mkdir -p /data && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
