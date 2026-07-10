#!/bin/bash
# Full MCP verification battery. Usage: mcp_battery.sh <base_url> [boot]
B="$1"
if [ "$2" = "boot" ]; then
  cd "/sessions/pensive-wizardly-pascal/mnt/Agent Guild/live/guild"
  rm -f /tmp/audit/bat.json*
  GUILD_DATA=/tmp/audit/bat.json GUILD_BOOTSTRAP_EVAL=0 GUILD_ADMIN_TOKEN=t python3 -m uvicorn app.main:app --port 8799 > /tmp/audit/uv_bat.log 2>&1 &
  BPID=$!
  for i in $(seq 1 25); do sleep 1; curl -s -m 2 -o /dev/null localhost:8799/health && break; done
fi
UA="pilot-a-audit-mcp/1"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"pilot-a-audit","version":"1"}}}'
hdr=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -A "$UA")

echo "== 1. initialize (public Host where applicable) =="
SID=$(curl -s -m 15 "${hdr[@]}" -D - -o /tmp/audit/b1.json -X POST "$B/mcp/" -d "$INIT" | grep -i '^mcp-session-id' | tr -d '\r' | cut -d' ' -f2)
CODE1=$(head -c 40 /tmp/audit/b1.json | grep -q jsonrpc && echo has-json || true)
echo "session=$SID body_sample=$(head -c 100 /tmp/audit/b1.json | tail -c 60)"

echo "== 2. notifications/initialized =="
curl -s -m 10 -o /dev/null -w "code=%{http_code}\n" "${hdr[@]}" -H "Mcp-Session-Id: $SID" -X POST "$B/mcp/" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

echo "== 3. tools/list =="
curl -s -m 15 "${hdr[@]}" -H "Mcp-Session-Id: $SID" -X POST "$B/mcp/" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' -o /tmp/audit/b3.raw
python3 -c "
import json
raw=open('/tmp/audit/b3.raw').read()
line=[l for l in raw.splitlines() if l.startswith('data:')]
d=json.loads(line[0][5:]) if line else json.loads(raw)
tools=[t['name'] for t in d['result']['tools']]
print('tools:', len(tools), '| has ag_json_canonicalize:', 'ag_json_canonicalize' in tools, '| has guild_check:', 'guild_check' in tools)"

echo "== 4. invoke harmless tool (ag_json_canonicalize) =="
curl -s -m 20 "${hdr[@]}" -H "Mcp-Session-Id: $SID" -X POST "$B/mcp/" -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ag_json_canonicalize","arguments":{"payload":{"value":{"b":2,"a":1}}}}}' -o /tmp/audit/b4.raw
python3 -c "
import json
raw=open('/tmp/audit/b4.raw').read()
line=[l for l in raw.splitlines() if l.startswith('data:')]
d=json.loads(line[0][5:]) if line else json.loads(raw)
r=d['result']
print('isError:', r.get('isError'), '| canonical in payload:', '\"canonical\"' in json.dumps(r))"

echo "== 5. malformed invocation (missing required arg) =="
curl -s -m 15 "${hdr[@]}" -H "Mcp-Session-Id: $SID" -X POST "$B/mcp/" -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"ag_json_canonicalize","arguments":{"payload":{"wrong_field":1}}}}' -o /tmp/audit/b5.raw
python3 -c "
import json
raw=open('/tmp/audit/b5.raw').read()
line=[l for l in raw.splitlines() if l.startswith('data:')]
d=json.loads(line[0][5:]) if line else json.loads(raw)
s=json.dumps(d)
print('machine-readable error:', ('required' in s or 'validation' in s or 'error' in s.lower()) and 'jsonrpc' in s)"

echo "== 6. unauthorised write (guild_attest, bogus key) =="
curl -s -m 15 "${hdr[@]}" -H "Mcp-Session-Id: $SID" -X POST "$B/mcp/" -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"guild_attest","arguments":{"issuer_api_key":"sk_bogus","subject_id":"agent_x","capability":"x","rating":0.5}}}' -o /tmp/audit/b6.raw
python3 -c "
import json
raw=open('/tmp/audit/b6.raw').read()
line=[l for l in raw.splitlines() if l.startswith('data:')]
d=json.loads(line[0][5:]) if line else json.loads(raw)
s=json.dumps(d).lower()
print('rejected with structured error:', ('key' in s or 'auth' in s or 'invalid' in s or 'error' in s))"

echo "== 7. ordinary FastAPI routes =="
echo "health: $(curl -s -m 10 -o /dev/null -w '%{http_code}' $B/health) | check: $(curl -s -m 10 -o /dev/null -w '%{http_code}' "$B/check?capability=fact-check") | card: $(curl -s -m 10 -o /dev/null -w '%{http_code}' $B/.well-known/agent-card.json)"

if [ "$2" = "boot" ]; then
  echo "== 8. spoofed Host -> guard fires (local only) =="
  curl -s -m 10 -o /tmp/audit/b8.txt -w "spoofed-host code=%{http_code}\n" "${hdr[@]}" -H "Host: evil.example" -X POST "$B/mcp/" -d "$INIT"
  echo "== 9. public Host accepted via allowlist (local, spoofed to prod host) =="
  curl -s -m 10 -o /dev/null -w "public-host code=%{http_code}\n" "${hdr[@]}" -H "Host: agent-guild-5d5r.onrender.com" -X POST "$B/mcp/" -d "$INIT"
  kill $BPID 2>/dev/null
fi
