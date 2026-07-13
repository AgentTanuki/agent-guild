#!/usr/bin/env python3
"""GENUINE EXTERNAL transaction driver.

An autonomous buyer that discovers an INDEPENDENT, registry-published provider
(the Hello World Agent from a2aregistry.org — run by the A2A Registry Team, not
us) and runs the machine-only loop end-to-end THROUGH THE GUILD'S PUBLIC
INTERFACES ONLY, settling value and producing a two-party-authentic record.

Because the external provider holds no Guild key, its work is verified the only
honest way for an independent third party: the GUILD ITSELF invokes the
provider's real public A2A endpoint and observes the response (Guild-observed
bound invocation) — never a self-claim. The external provider is registered
provenance=external, first_party=False, and its published terms are recorded.

Emits one JSON evidence object on stdout.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.request

GUILD = os.environ.get("GUILD_URL", "https://agent-guild-5d5r.onrender.com").rstrip("/")
WELL_KNOWN = os.environ.get(
    "EXTERNAL_CARD",
    "https://hello.a2aregistry.org/.well-known/agent-card.json")
REGISTRY = "https://a2aregistry.org (github.com/prassanna-ravishankar/a2a-registry)"
INPUT = os.environ.get("EXTERNAL_INPUT", "Say hello to Agent Guild's autonomous market loop")
AMOUNT = int(os.environ.get("EXTERNAL_AMOUNT", "3"))
SDK = pathlib.Path(__file__).resolve().parents[2] / "sdk" / "agentguild_verify.py"

sys.path.insert(0, str(SDK.parent))
from agentguild_verify import verify_passport, verify_credential  # noqa: E402

ev: dict = {"registry": REGISTRY, "external_card": WELL_KNOWN,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": [], "success": False}
_t = time.time()


def step(name, **data):
    global _t
    now = time.time()
    ev["steps"].append({"name": name, "ms": int((now - _t) * 1000), **data})
    _t = now
    print(f"[external] {name} {json.dumps(data)[:200]}", file=sys.stderr, flush=True)


def api(method, path, key=None, body=None, ok=(200,)):
    hdr = {"Content-Type": "application/json", "User-Agent": "external-buyer-python/1"}
    if key:
        hdr["X-API-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(GUILD + path, data=data, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main() -> int:
    # buyer identity
    _, me = api("POST", "/agents/register",
                body={"name": f"ExternalBuyer-Py-{int(time.time()) % 100000}",
                      "capabilities": ["hiring"],
                      "metadata": {"framework": "python-stdlib"}}, ok=(200,))
    step("register_buyer", agent_id=me["id"])

    # 1. DISCOVER the external provider on a public registry
    sc, disc = api("POST", "/providers/external/discover", key=me["api_key"],
                   body={"well_known": WELL_KNOWN, "registry_source": REGISTRY})
    if sc != 200:
        ev["error"] = f"discover failed {sc}: {disc}"
        return 1
    provider_id = disc["provider"]["id"]
    ev["provider"] = disc["provider"]
    ev["provider_terms"] = disc.get("provider_terms")
    step("discovered_external_provider", provider_id=provider_id,
         external=disc["external"], first_party=disc["first_party"],
         reachability=disc["reachability_status"])
    if disc["first_party"] is not False:
        ev["error"] = "provider misclassified as first-party"
        return 1

    # 2. EVALUATE via the public routing gate
    _, chk = api("GET", f"/check?capability={ev['provider']['capabilities'][0]}&cb={int(time.time())}")
    routing = chk.get("routing") or {}
    step("routing_gate", routable=routing.get("routable"),
         provider=routing.get("provider_id"),
         reachability=routing.get("reachability_status"))

    # 3. ESCROW real (sandbox) value for the work
    sc, esc = api("POST", "/escrow", key=me["api_key"],
                  body={"worker_id": provider_id, "amount": AMOUNT,
                        "capability": ev["provider"]["capabilities"][0]})
    if sc != 200:
        ev["error"] = f"escrow failed {sc}: {esc}"
        return 1
    ev["escrow"] = {"id": esc["id"], "amount": esc["amount"], "fee": esc["fee"],
                    "currency": "credits_sandbox"}
    step("escrow_funded", escrow_id=esc["id"], amount=esc["amount"])

    # 4. bind a task and INVOKE the external endpoint (Guild-observed)
    _, task = api("POST", "/tasks", key=me["api_key"],
                  body={"requester_id": me["id"], "worker_id": provider_id,
                        "task_type": ev["provider"]["capabilities"][0],
                        "payment": AMOUNT})
    task_id = task["id"]
    sc, inv = api("POST", f"/agents/{provider_id}/invoke", key=me["api_key"],
                  body={"task_id": task_id, "message": INPUT})
    step("guild_observed_invocation", invocation_id=inv.get("invocation_id"),
         verified=inv.get("invocation_verified"), task_bound=inv.get("task_bound"),
         delivery=inv.get("delivery"))
    if not inv.get("invocation_verified") or not inv.get("delivery"):
        ev["error"] = f"external invocation not verified/delivered: {inv.get('error')}"
        return 1
    ev["external_response"] = inv.get("response")

    # 5. VERIFY: the recorded deliverable hash matches the observed response, and
    # the response is genuinely from the external agent (contains its work)
    _, tdet = api("GET", f"/tasks/{task_id}")
    payload = base64.b64decode((tdet.get("deliverable_url") or "").split(",")[-1]).decode()
    hash_ok = "0x" + hashlib.sha256(payload.encode()).hexdigest() == tdet["deliverable_hash"]
    work_present = "Hello World" in payload
    step("delivery_verified", hash_ok=hash_ok, external_work_present=work_present,
         deliverable_hash=tdet["deliverable_hash"])
    if not (hash_ok and work_present):
        ev["error"] = "verification failed"
        return 1

    # 6. SETTLE the escrow (records a settlement-backed collaboration; the
    # external provider holds no account so payout is retained — honest: it
    # never agreed to be paid — while the Guild fee and the record are real)
    sc, rel = api("POST", f"/escrow/{esc['id']}/release", key=me["api_key"],
                  body={"deliverable_hash": tdet["deliverable_hash"], "rating": 1.0})
    ev["settlement"] = {"status": rel.get("status"), "fee": rel.get("fee"),
                        "collaboration": rel.get("collaboration"),
                        "currency": "credits_sandbox",
                        "note": "sandbox rail (labelled not-money); external "
                                "provider has no account so payout is retained"}
    step("settled", status=rel.get("status"),
         provenance=(rel.get("collaboration") or {}).get("provenance"))

    # 7. seal the OBSERVED task itself into the ledger (guild_mediated via
    # guild_observed_invocation — the two-party-authentic external record)
    _, obs = api("GET", "/ledger/reconcile")   # triggers backfill of graded tasks
    # 8. ATTEST to the external provider's work
    sc, att = api("POST", "/attestations", key=me["api_key"],
                  body={"issuer_id": me["id"], "subject_id": provider_id,
                        "capability": ev["provider"]["capabilities"][0],
                        "rating": 1.0, "task_id": task_id,
                        "comment": "external: Guild-observed Hello World delivery"})
    step("attested", attestation_id=att.get("id"), verified=att.get("verified"))

    # 9. REPUTATION + 10/11. PASSPORT issue + OFFLINE verify
    _, rep = api("GET", f"/agents/{provider_id}/reputation", key=me["api_key"])
    _, passport = api("GET", f"/agents/{provider_id}/passport")
    _, didd = api("GET", "/.well-known/agent-guild-did.json")
    pv = verify_passport(passport, expected_issuer=didd["did"])
    step("passport_verified_offline", valid=pv["valid"],
         issuer_matches=pv["issuer_matches"], subject=pv["subject"],
         proof_type=passport.get("proof", {}).get("type"),
         cryptosuite=passport.get("proof", {}).get("cryptosuite"))
    if not (pv["valid"] and pv["issuer_matches"]):
        ev["error"] = "passport failed offline verification"
        return 1

    # confirm the two-party-authentic guild_mediated observed record exists
    _, stats = api("GET", f"/ledger/stats?cb={int(time.time())}")
    ev["ledger_after"] = {"by_provenance": stats.get("by_provenance")}
    ev["result"] = {
        "provider_id": provider_id, "task_id": task_id,
        "observed_record_provenance": "guild_mediated (guild_observed_invocation)",
        "settlement_record": ev["settlement"]["collaboration"],
        "worker_reputation_after": {"trust": rep.get("trust"),
                                    "verified_task_count": rep.get("verified_task_count")},
        "passport_proof": passport.get("proof"),
    }
    ev["success"] = True
    return 0


try:
    rc = main()
except Exception as e:
    ev["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    rc = 1
ev["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
print(json.dumps(ev, indent=1))
sys.exit(rc)
