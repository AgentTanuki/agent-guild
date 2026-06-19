#!/usr/bin/env bash
# Agent Guild — first-contact live test.
# Proves the full autonomous loop against the LIVE endpoint with nothing but
# curl + python3: an agent registers, acquires credits with no human, reads the
# discovery manifest, pays for a best-agent lookup and a risk score, and we watch
# the instrumentation funnel increment. Fails gracefully if supply isn't seeded.
#
#   bash first_contact.sh [capability]
#   (defaults: live URL below, capability "fact-check")
set -uo pipefail

GUILD="${GUILD_URL:-https://agent-guild-5d5r.onrender.com}"
CAP="${1:-fact-check}"
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

echo "Agent Guild — first contact → $GUILD   (capability: $CAP)"
echo "=================================================================="

echo; echo "[0] Instrumentation BEFORE:"
curl -s "$GUILD/instrumentation" | j "'    '+json.dumps(d)"

echo; echo "[1] Register a trial consumer agent (POST /agents/register):"
AGENT=$(curl -s -X POST "$GUILD/agents/register" -H 'Content-Type: application/json' \
  -d '{"name":"FirstContact-Consumer","capabilities":["research"],"metadata":{"external":true}}')
AGENT_ID=$(echo "$AGENT" | j "d.get('id','?')")
echo "    agent id = $AGENT_ID"

echo; echo "[2] Claim trial credits, no human (POST /billing/trial):"
TRIAL=$(curl -s -X POST "$GUILD/billing/trial")
KEY=$(echo "$TRIAL" | j "d.get('key','')")
echo "    billing key = ${KEY:0:14}…   balance = $(echo "$TRIAL" | j "d.get('balance','?')") credits"
[ -z "$KEY" ] && { echo "    !! no billing key returned — aborting"; exit 1; }

echo; echo "[3] Confirm balance (GET /billing/account):"
curl -s "$GUILD/billing/account" -H "X-API-Key: $KEY" \
  | j "'    balance='+str(d.get('balance'))+'  credit_usd=\$'+str(d.get('credit_usd'))"

echo; echo "[4] Read the discovery manifest (what an agent uses to decide):"
curl -s "$GUILD/.well-known/agent-guild.json" \
  | j "'    '+d['name']+' v'+d['version']+'  |  discover='+str(d['endpoints']['discover']['cost_credits'])+'cr  risk_score='+str(d['endpoints']['risk_score']['cost_credits'])+'cr'"

echo; echo "[5] Best-agent lookup — PAID (GET /search), headers captured:"
BODY=$(curl -s -D /tmp/ag_hdr -H "X-API-Key: $KEY" "$GUILD/search?capability=$CAP&limit=1")
COUNT=$(echo "$BODY" | j "d.get('count',0)"); COUNT=${COUNT:-0}

if [ "$COUNT" = "0" ]; then
  echo "    (no agents returned for '$CAP')"
  echo
  echo "  ──────────────────────────────────────────────────────────────"
  echo "  SUPPLY NOT SEEDED YET — the graph has no '$CAP' agents to rank."
  echo "  The lookup still worked and was metered (see headers below), but"
  echo "  there's nothing to recommend. Seed supply once, then re-run:"
  echo
  echo "    cd \"/Users/rossburley/Claude/Projects/Agent Guild/live\" \\"
  echo "      && python3 -m pip install -q httpx \\"
  echo "      && GUILD_URL=$GUILD GUILD_ADMIN_TOKEN=<token-from-render> \\"
  echo "         python3 scripts/seed_supply.py"
  echo "  ──────────────────────────────────────────────────────────────"
  echo
  echo "[7] Charge headers from the (empty) lookup:"
  grep -i 'x-guild' /tmp/ag_hdr | sed 's/^/    /' || echo "    (none)"
  echo; echo "[9] Instrumentation AFTER (note paid_query already +1):"
  curl -s "$GUILD/instrumentation" | j "'    '+json.dumps(d)"
  exit 0
fi

BEST_ID=$(echo "$BODY" | j "d['results'][0]['id']")
BEST_NAME=$(echo "$BODY" | j "d['results'][0]['name']")
BEST_TRUST=$(echo "$BODY" | j "round(d['results'][0]['trust'],1)")
echo "    recommended: $BEST_NAME   (trust=$BEST_TRUST, id=$BEST_ID)"

echo; echo "[6] Risk-score on the recommended agent — PAID (GET /agents/{id}/risk-score):"
curl -s -H "X-API-Key: $KEY" "$GUILD/agents/$BEST_ID/risk-score" \
  | j "'    risk='+str(d.get('risk'))+' → '+str(d.get('recommendation'))+'   (confidence='+str(d.get('confidence'))+', collusion_suspicion='+str(d.get('collusion_suspicion'))+')'"

echo; echo "[7] Response headers proving credits were charged (from the lookup):"
grep -i 'x-guild' /tmp/ag_hdr | sed 's/^/    /' || echo "    (none)"

echo; echo "[8] Balance after spending (GET /billing/account):"
curl -s "$GUILD/billing/account" -H "X-API-Key: $KEY" \
  | j "'    balance='+str(d.get('balance'))+'  spent='+str(d.get('spent'))+' credits'"

echo; echo "[9] Instrumentation AFTER:"
curl -s "$GUILD/instrumentation" | j "'    '+json.dumps(d)"

echo
echo "✅ Full live loop: discover → acquire credits → paid lookup → risk-check → funnel incremented."
