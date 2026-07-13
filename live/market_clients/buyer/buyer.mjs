#!/usr/bin/env node
/**
 * Autonomous MARKET BUYER — Node.js framework (the worker is Python/FastAPI).
 *
 * Runs the complete machine-only loop with zero human steps:
 *   discover → evaluate → negotiate (signed offer) → escrow → invoke →
 *   deliver → verify → settle → attest → reputation → passport verify.
 *
 * Discovery uses ONLY the Guild's public machine interfaces (/check routing
 * gate). Verification recomputes the deliverable hash AND the work itself.
 * Passport verification is OFFLINE via the standalone SDK verifier.
 *
 * Modes:
 *   node buyer.mjs guild       — Guild-selected delegation (full loop)
 *   node buyer.mjs unassisted  — baseline: no trust data, no routing gate, no
 *                                escrow; picks a provider naively and invokes
 *                                it directly (measures the difference)
 * Output: one JSON metrics object on stdout.
 */
import { createHash } from "node:crypto";
import { verifyPassport, verifyCredential } from "../../../sdk/agentguild_verify.mjs";

const GUILD = (process.env.GUILD_URL || "https://agent-guild-5d5r.onrender.com").replace(/\/$/, "");
const CAPABILITY = "text.stats";
const INPUT = process.env.BUYER_INPUT ||
  "The quick brown fox jumps over the lazy dog. The dog did not mind.";
const AMOUNT = Number(process.env.BUYER_AMOUNT || 5);
const MODE = process.argv[2] || "guild";

const fpHeaders = () => {
  // Guild-operated traffic ALWAYS tags first-party (mirrors
  // live/market_clients/_firstparty.py): in honor mode a non-empty sentinel
  // tags the caller; the old version returned {} without a token and
  // silently counted this Guild-run buyer as external.
  const tok = (process.env.GUILD_FIRST_PARTY_TOKEN || "").trim();
  return {
    "X-Agent-Guild-First-Party": tok || "guild-operated-script",
    "X-Agent-Guild-Role": "test",
  };
};

const metrics = {
  mode: MODE, capability: CAPABILITY, started_at: new Date().toISOString(),
  steps: [], retries: 0, human_interventions: 0, success: false,
  total_cost_credits_sandbox: 0, provider: null,
};

let stepStart = Date.now();
function step(name, data = {}) {
  const now = Date.now();
  metrics.steps.push({ name, ms: now - stepStart, ...data });
  stepStart = now;
  console.error(`[buyer:${MODE}] ${name} ${JSON.stringify(data).slice(0, 160)}`);
}

async function api(method, path, { key, body, ok = [200] } = {}) {
  const r = await fetch(GUILD + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "User-Agent": "market-buyer-node/1",
      ...(key ? { "X-API-Key": key } : {}),
      ...fpHeaders(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await r.json().catch(() => ({}));
  if (!ok.includes(r.status)) {
    throw new Error(`${method} ${path} -> ${r.status}: ${JSON.stringify(json).slice(0, 300)}`);
  }
  return json;
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

function localStats(text) {
  const words = text.split(/\s+/).filter(Boolean);
  return {
    capability: CAPABILITY,
    chars: text.length,
    words: words.length,
    lines: text.split("\n").length,
    unique_words: new Set(words.map((w) => w.toLowerCase().replace(/[.,!?;:"()\[\]]/g, "")).values()).size,
    sha256: createHash("sha256").update(text, "utf8").digest("hex"),
  };
}

async function ensureIdentity() {
  const me = await api("POST", "/agents/register", {
    body: { name: `MarketBuyer-Node-${Date.now() % 100000}`, capabilities: ["hiring"],
            metadata: { framework: "node", operator: "agent-guild (first-party demo buyer)" } },
  });
  step("register_buyer", { agent_id: me.id });
  return me;
}

async function guildLoop() {
  const me = await ensureIdentity();

  // 1. DISCOVER + EVALUATE through the public routing gate only
  let routing = null, decision = null;
  for (let i = 0; i < 60; i++) {
    const check = await api("GET", `/check?capability=${CAPABILITY}&cb=${Date.now()}`,
                            { key: me.api_key, ok: [200] });
    decision = check.decision;
    if (check.routing?.routable) { routing = check.routing; break; }
    metrics.retries++;
    await sleep(10000);
  }
  if (!routing) throw new Error("routing gate never produced a verified reachable provider");
  metrics.provider = routing.provider_id;
  step("discover_via_routing_gate", {
    provider: routing.provider_id, endpoint: routing.endpoint,
    reachability: routing.reachability_status, trust: routing.trust,
    estimate: decision?.estimate, confidence: decision?.confidence,
  });

  // 2. NEGOTIATE: signed offer (escrow funded at offer time)
  const offer = await api("POST", "/offers", {
    key: me.api_key,
    body: { worker_id: routing.provider_id, capability: CAPABILITY,
            amount: AMOUNT, deadline_seconds: 900,
            terms: { input: INPUT, format: "stats-json" } },
  });
  metrics.total_cost_credits_sandbox += AMOUNT;
  step("signed_offer_posted", {
    offer_id: offer.id, offer_hash: offer.core_hash,
    value_tier: offer.core.value_tier, escrow_id: offer.escrow_id,
  });

  // 3. wait for acceptance (worker countersigns) + delivery
  let o = offer, task = null;
  for (let i = 0; i < 90; i++) {
    o = await api("GET", `/offers/${offer.id}`);
    if (o.status === "accepted" && o.task_id) {
      task = await api("GET", `/tasks/${o.task_id}`);
      if (task.deliverable_hash) break;
    }
    if (o.status === "expired") throw new Error("offer expired unaccepted");
    metrics.retries++;
    await sleep(10000);
  }
  if (!task?.deliverable_hash) throw new Error("no delivery before timeout");
  step("accepted_and_delivered", {
    task_id: o.task_id, accept_hash_bound: o.accept?.core?.offer_hash === offer.core_hash,
    deliverable_hash: task.deliverable_hash,
  });

  // 4. GUILD-OBSERVED INVOKE bound to the task (evidence for guild_mediated)
  const inv = await api("POST", `/agents/${routing.provider_id}/invoke`, {
    key: me.api_key, body: { task_id: o.task_id, message: INPUT },
  });
  step("guild_observed_invocation", {
    invocation_id: inv.invocation_id, verified: inv.invocation_verified,
    task_bound: inv.task_bound,
  });

  // 5. VERIFY: hash matches AND the work is actually correct
  const dataUri = task.deliverable_url || "";
  const payload = Buffer.from(dataUri.split(",")[1] || "", "base64").toString("utf8");
  const hashOk = "0x" + createHash("sha256").update(payload, "utf8").digest("hex")
    === task.deliverable_hash;
  const got = JSON.parse(payload);
  const want = localStats(INPUT);
  const correct = got.sha256 === want.sha256 && got.chars === want.chars
    && got.words === want.words;
  if (!hashOk || !correct) throw new Error(`verification failed hash=${hashOk} correct=${correct}`);
  step("delivery_verified", { hash_ok: hashOk, work_correct: correct });

  // 6. SETTLE the escrow (worker paid, Guild takes its fee)
  const settled = await api("POST", `/escrow/${offer.escrow_id}/release`, {
    key: me.api_key, body: { deliverable_hash: task.deliverable_hash, rating: 1.0 },
  });
  step("settled", { escrow_id: offer.escrow_id, status: settled.status,
                    ledger_provenance: settled.collaboration?.provenance });

  // 7. ATTEST
  const att = await api("POST", "/attestations", {
    key: me.api_key,
    body: { issuer_id: me.id, subject_id: routing.provider_id,
            capability: CAPABILITY, rating: 1.0, task_id: o.task_id,
            comment: "machine-loop: verified correct delivery" },
  });
  step("attested", { attestation_id: att.id, verified: att.verified });

  // 8. REPUTATION moved; 9. PASSPORT issued + verified OFFLINE
  const rep = await api("GET", `/agents/${routing.provider_id}/reputation`, { key: me.api_key });
  const passport = await api("GET", `/agents/${routing.provider_id}/passport`);
  const didDoc = await api("GET", "/.well-known/agent-guild-did.json");
  const pv = verifyPassport(passport, { expectedIssuer: didDoc.did });
  if (!pv.valid || pv.issuerMatches !== true) throw new Error("passport failed offline verification");
  step("passport_verified_offline", {
    subject: pv.subject, issuer_pinned: pv.issuerMatches,
    proof_type: passport.proof?.type, cryptosuite: passport.proof?.cryptosuite,
    checkpoint_valid: pv.checkpointValid,
  });

  metrics.success = true;
  metrics.result = {
    offer_id: offer.id, task_id: o.task_id, escrow_id: offer.escrow_id,
    provider: routing.provider_id, trust_at_hire: routing.trust,
    worker_reputation_after: { trust: rep.trust, verified_task_count: rep.verified_task_count },
    passport_proof: passport.proof,
  };
}

async function unassistedLoop() {
  const me = await ensureIdentity();
  // naive selection: first agent CLAIMING the capability, no trust data, no
  // reachability gate, no escrow — invoke whatever endpoint it advertises.
  const agents = await api("GET", "/agents");
  const candidates = agents.filter((a) => (a.capabilities || []).includes(CAPABILITY));
  if (!candidates.length) throw new Error("no candidates claim the capability");
  const pick = candidates[0];
  metrics.provider = pick.id;
  step("naive_pick", { provider: pick.id, has_endpoint_info: false });
  const detail = await api("GET", `/agents/${pick.id}`, { key: me.api_key });
  const endpoint = detail.metadata?.endpoint;
  if (!endpoint) {
    step("dead_end_no_endpoint", { provider: pick.id });
    throw new Error("naively-picked provider has no reachable endpoint (dead end)");
  }
  const r = await fetch(endpoint, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "message/send",
      params: { message: { role: "user", messageId: "m1",
        parts: [{ kind: "text", text: INPUT }] } } }),
  });
  const body = await r.json().catch(() => null);
  const text = body?.result?.parts?.[0]?.text;
  const got = text ? JSON.parse(text) : null;
  const ok = got && got.sha256 === localStats(INPUT).sha256;
  if (!ok) throw new Error("unassisted invocation returned wrong/no result");
  step("unassisted_invocation_ok", {});
  metrics.success = true;   // no escrow, no receipt, no recourse — noted
  metrics.result = { note: "no escrow, no signed records, no recourse, no reputation update" };
}

try {
  if (MODE === "guild") await guildLoop();
  else await unassistedLoop();
} catch (e) {
  metrics.error = String(e.message || e).slice(0, 400);
}
metrics.finished_at = new Date().toISOString();
metrics.total_ms = metrics.steps.reduce((a, s) => a + s.ms, 0);
console.log(JSON.stringify(metrics, null, 1));
process.exit(metrics.success ? 0 : 1);
