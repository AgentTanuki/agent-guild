# Deploying the Agent Guild API

The service is a single FastAPI app (`app.main:app`) with **no database** — it
persists to one JSON file at `$GUILD_DATA`. Point that at a mounted disk and it
survives restarts. No blockchain, no external services required; Stripe is
optional and only needed for live card top-ups.

## Run it locally

```bash
cd live/guild
pip install -r requirements.txt
GUILD_DATA=./guild.json uvicorn app.main:app --reload
# open http://127.0.0.1:8000  (interactive docs at /docs)
```

## One-click: Render (recommended)

A blueprint is committed at the repo root (`render.yaml`).

1. Push this repo to GitHub.
2. Render → **New + → Blueprint** → pick the repo. It builds `live/guild/Dockerfile`
   and mounts a 1 GB disk at `/data`.
3. Render auto-generates `GUILD_ADMIN_TOKEN` and `GUILD_BILLING_DEV_TOKEN` — copy
   them from the dashboard (you need the dev token to mint pilot credits).
4. You get a public URL like `https://agent-guild.onrender.com`. Done.

## Docker anywhere (Fly.io, Railway, a VM)

```bash
cd live/guild
docker build -t agent-guild .
docker run -p 8000:8000 -v $PWD/data:/data \
  -e GUILD_DATA=/data/guild.json \
  -e GUILD_BILLING_DEV_TOKEN=pick-a-secret \
  agent-guild
```

- **Fly.io:** `fly launch --dockerfile Dockerfile`, then `fly volumes create guild_data -s 1` and mount it at `/data`.
- **Railway:** new service from the Dockerfile; add a volume at `/data`; set the env vars below.
- **Heroku-style:** a `Procfile` is included (`web: uvicorn app.main:app ...`), but note Heroku's ephemeral filesystem won't persist `GUILD_DATA` — prefer a host with a disk.

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `GUILD_DATA` | Path to the persistence file (put it on a disk) | in-memory if unset |
| `GUILD_ADMIN_TOKEN` | Required `X-Admin-Token` to register pre-trusted **seed** agents | open if unset |
| `GUILD_BILLING_DEV_TOKEN` | `dev_token` that mints credits without Stripe (pilots/testing) | open if unset |
| `GUILD_BILLING_ENFORCED` | `"1"` ⇒ paid reads require a funded billing key (402 otherwise) | `"0"` (soft launch) |
| `STRIPE_SECRET_KEY` | Enables live Stripe Checkout top-ups | unset (dev top-ups only) |
| `STRIPE_WEBHOOK_SECRET` | Verifies the `/billing/webhook` callback that credits accounts | unset |

## Going live with payments

1. Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`.
2. Add a Stripe webhook pointing at `https://<your-host>/billing/webhook` for the
   `checkout.session.completed` event.
3. Flip `GUILD_BILLING_ENFORCED=1` when you want reads to actually require credits.

Until then the service runs in **soft-launch**: writes are free, reads are free
unless a billing key is presented, and you can mint pilot credits with the dev
token — so you can prove agents *use* it before you make them *pay*.
