# Agent Guild — live public API.
# Build context is this directory (live/guild). Persists to a mounted disk at
# /data so reputation, accounts and the credit ledger survive restarts.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching. `stripe` is only needed for live card
# payments; it is imported lazily, so the service runs without it.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV GUILD_DATA=/data/guild.json
ENV PORT=8000
EXPOSE 8000

# Create the data dir at runtime (the disk is mounted at /data).
CMD ["sh", "-c", "mkdir -p /data && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
