# Deploying the Agent Guild API

The service is a single FastAPI app (`app.main:app`) with two store backends:
the default single-JSON-file store at `$GUILD_DATA` (zero setup, fine for
local dev) and a SQLite backend (`GUILD_STORE=sqlite` — what production runs;
single-instance only, move to Postgres before scaling out). Point the data
path at a mounted disk and it survives restarts.

External services, honestly stated: when the x402 rail is enabled the app
settles REAL USDC on Base mainnet through the authenticated Coinbase CDP
facilitator and independently confirms settlements against a public Base RPC
— so a payments-enabled deployment depends on both. Stripe is optional and
only needed for live card top-ups. With the rail disabled (the local-dev
default) none of these are contacted.

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

## Shipping changes (machine-operated — the ONLY sanctioned path)

Since 2026-07-22, **nothing is pushed to `main` directly** — not by humans,
not by scheduled autonomous sessions, and branch protection enforces it. The
loop (`.github/workflows/ship.yml`; decisions unit-tested in
`live/scripts/ship_decision.py` + `tests/test_ship_decision.py`):

1. Push your change to a branch named `ship/<topic>`.
2. The `ship` workflow opens the PR to `main` and dispatches the full `ci`
   workflow against the branch head (both store backends, strict-KDF,
   contract drift, independent VC + caller-proof verifiers, trust-plane,
   x402 interop).
3. On a green `ci` conclusion the workflow merges (squash) ONLY when the PR
   head is exactly the certified SHA **and already contains current `main`**.
   If `main` advanced after certification — including when two concurrently
   certified branches race and one lands first — the branch is automatically
   updated with `main`, `ci` is dispatched again, and nothing merges until
   the COMBINED state is certified.
4. The workflow then checks out **the exact merged SHA** and runs the
   deployment-aware release gate (`live/scripts/release_gate.py`) from that
   tree: production must serve that SHA and pass the live probes, and the
   machine-readable attestation is uploaded per merged SHA.
5. A red gate triggers machine-complete recovery: an automatic
   `ship/revert-<sha>` branch carrying the revert goes back through this
   same loop — certified, merged, deployed, and the RECOVERY gate-certified.
   The issue that is also filed is telemetry, never the recovery mechanism.
   A failed revert halts rather than oscillating.
6. If the ship changed `server.json`, the pinned MCP-registry publish is
   dispatched automatically.

**Branch protection on `main` is REQUIRED, not optional.** The workflow
refuses to merge anything while `main` is unprotected. The one-time,
admin-only setup is `live/scripts/protect_main.sh`: pull requests required
(zero approvals — certification comes from `ci`, not from human eyes), every
non-release `ci` job a required status check, strict up-to-date enforcement,
`enforce_admins` on, force pushes and deletions off. GitHub Actions'
`GITHUB_TOKEN` cannot administer branch protection, which is exactly why this
single settings action belongs to the repo owner and to no one else.
