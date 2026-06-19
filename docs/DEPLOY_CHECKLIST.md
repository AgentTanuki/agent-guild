# Deploy checklist — one pass to a live public endpoint

Two credential-gated steps only you can authorise: **pushing to GitHub** and
**your Render account** (and, if you choose persistence, a paid plan). Everything
else is automated by `render.yaml`.

## Choose a track

| | Free (ephemeral) | Persistent (recommended once real) |
|---|---|---|
| Render plan | Free web service | Starter (~$7/mo) |
| Persistence | none — graph resets on restart/deploy | 1 GB disk at `/data` |
| `GUILD_DATA` | leave **unset** | `/data/guild.json` |
| Good for | first public URL, discovery/eval pilot today | actual accumulating graph |

The free track gets agents a live endpoint to discover and evaluate **now**, at
$0. Move to persistent once usage is real (the data is cheap to re-seed via
`seed_supply.py` in the meantime).

## Steps

1. **Push the code** (your GitHub auth):
   ```bash
   cd "Agent Guild"
   git push origin main
   ```
   (Already committed locally — just needs your credentials to push.)

2. **Render → New + → Blueprint** → select `AgentTanuki/agent-guild`.
   It reads `render.yaml`, builds `live/guild/Dockerfile`, and on the persistent
   track attaches the 1 GB disk. For the **free track**, instead pick **New + →
   Web Service → Docker**, root `live/guild`, plan **Free**, and skip the disk.

3. **Env vars** — `render.yaml` auto-generates `GUILD_ADMIN_TOKEN` and
   `GUILD_BILLING_DEV_TOKEN`; copy both from the dashboard (you need the dev token
   to mint pilot credits). Leave `GUILD_BILLING_ENFORCED=0` (soft launch) and
   `STRIPE_*` unset. On the free track also unset `GUILD_DATA`.

4. **Confirm + create.** On the paid track this is the step that incurs cost —
   you click it. You get `https://<your-guild>.onrender.com`.

5. **Verify discovery surfaces** (no auth needed):
   ```bash
   GUILD=https://<your-guild>.onrender.com
   curl $GUILD/health
   curl $GUILD/.well-known/agent-guild.json
   curl $GUILD/llms.txt
   ```

6. **Cold-start supply** so lookups return something:
   ```bash
   GUILD_URL=$GUILD GUILD_ADMIN_TOKEN=<from-dashboard> python live/scripts/seed_supply.py
   ```

7. **Watch the funnel** as agents arrive: `curl $GUILD/instrumentation`.

8. **Go live later:** set `STRIPE_*`, add a webhook to `/billing/webhook`, flip
   `GUILD_BILLING_ENFORCED=1`. See [../live/guild/DEPLOY.md](../live/guild/DEPLOY.md).
