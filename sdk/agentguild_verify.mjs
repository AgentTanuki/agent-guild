// Agent Guild — standalone AGI-1 Passport verifier for Node / TypeScript.
//
// The JavaScript-ecosystem twin of agentguild_verify.py: the lowest-friction way
// for any JS/TS agent or framework to check another agent's reputation. Verify a
// Guild-signed Agent Passport OFFLINE — no account, no SDK lock-in, no server code.
// Zero npm dependencies: uses Node's built-in `node:crypto` for Ed25519.
//
//   import { vet } from "./agentguild_verify.mjs";
//   const d = await vet("agent_d0a8f6ef9b41");   // fetch + verify offline + decide
//   if (d.verified && d.recommendation === "hire") { /* delegate */ }
//
// You are not trusting this file's author — you are checking an Ed25519 signature
// against the issuer's did:key. AGI-1 signs over a language-agnostic canonical JSON
// (sorted keys, ECMAScript number formatting), so this verifies byte-for-byte what
// the Python issuer signed. Spec: https://agent-guild-5d5r.onrender.com/standard

import { createPublicKey, verify as edVerify } from "node:crypto";

export const DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com";
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function b58decode(s) {
  let n = 0n;
  for (const ch of s) {
    const i = B58.indexOf(ch);
    if (i < 0) throw new Error("bad base58");
    n = n * 58n + BigInt(i);
  }
  let hex = n.toString(16);
  if (hex.length % 2) hex = "0" + hex;
  let bytes = hex === "0" ? [] : [...Buffer.from(hex, "hex")];
  for (const ch of s) { if (ch === "1") bytes.unshift(0); else break; }
  return Buffer.from(bytes);
}

export function publicKeyFromDid(did) {
  const mb = did.startsWith("did:key:") ? did.slice(8) : did;
  if (!mb.startsWith("z")) throw new Error("unsupported did multibase");
  const raw = b58decode(mb.slice(1));
  if (raw[0] !== 0xed || raw[1] !== 0x01) throw new Error("did:key is not Ed25519");
  return raw.subarray(2); // 32-byte raw Ed25519 public key
}

function edKey(raw32) {
  // Wrap the raw key in an SPKI DER so node:crypto can use it.
  const der = Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), raw32]);
  return createPublicKey({ key: der, format: "der", type: "spki" });
}

// Language-agnostic canonical JSON (AGI-1): sorted keys, no whitespace, ECMAScript
// number formatting. JSON.stringify already formats numbers the ECMAScript way
// (an integer-valued number has no decimal point), so a value the Python issuer
// canonicalised as "0" we also produce as "0".
export function canon(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  const t = typeof v;
  if (t === "object") {
    return "{" + Object.keys(v).sort()
      .map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
  }
  if (t === "number") {
    if (!Number.isFinite(v)) throw new Error("NaN/Infinity not permitted");
    return JSON.stringify(v);
  }
  return JSON.stringify(v); // string | boolean
}

function verifySig(payload, sigHex, raw32) {
  try {
    return edVerify(null, Buffer.from(canon(payload), "utf8"),
                    edKey(raw32), Buffer.from(sigHex, "hex"));
  } catch { return false; }
}

export function verifyCredential(vc) {
  try {
    const proof = vc.proof || {};
    if (!proof.proofValue) return false;
    const { proofValue, ...proofRest } = proof;
    const { proof: _omit, ...rest } = vc;
    const payload = { ...rest, proof: proofRest };
    return verifySig(payload, proofValue, publicKeyFromDid(vc.issuer));
  } catch { return false; }
}

export function verifyCheckpoint(cp) {
  try {
    if (typeof cp.proof !== "string") return false;
    const { proof, ...body } = cp;
    return verifySig(body, proof, publicKeyFromDid(cp.issuer));
  } catch { return false; }
}

export function verifyPassport(vc, { expectedIssuer = null } = {}) {
  const valid = verifyCredential(vc);
  const subj = (vc && vc.credentialSubject) || {};
  const issuer = (vc && vc.issuer) || "";
  const anchor = valid ? (subj.ledger_anchor || {}) : {};
  const cp = anchor.checkpoint || null;
  const { id, ...claims } = subj;
  return {
    valid,
    issuer,
    issuerMatches: expectedIssuer ? issuer === expectedIssuer : null,
    subject: subj.id || "",
    claims: valid ? claims : null,
    verifiableCollaborations: anchor.verifiable_collaborations ?? null,
    checkpointValid: cp ? verifyCheckpoint(cp) : null,
  };
}

async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  return r.json();
}

export async function fetchPassport(agentId, host = DEFAULT_HOST) {
  return getJson(`${host}/agents/${agentId}/passport`);
}

export async function issuerDid(host = DEFAULT_HOST) {
  return (await getJson(`${host}/.well-known/agent-guild-did.json`)).did;
}

export async function vet(agentId, host = DEFAULT_HOST, { pinIssuer = true } = {}) {
  const vc = await fetchPassport(agentId, host);
  const expected = pinIssuer ? await issuerDid(host) : null;
  const res = verifyPassport(vc, { expectedIssuer: expected });
  const claims = res.claims || {};
  const trustworthy = res.valid && (res.issuerMatches === true || res.issuerMatches === null);
  return {
    agentId,
    verified: trustworthy,
    recommendation: trustworthy ? (claims.recommendation ?? null) : null,
    trust: claims.trust ?? null,
    risk: claims.risk ?? null,
    verifiableCollaborations: res.verifiableCollaborations,
    issuer: res.issuer,
    raw: res,
  };
}

// CLI: node agentguild_verify.mjs <agent_id> [host]
if (import.meta.url === `file://${process.argv[1]}`) {
  const [, , agentId, host] = process.argv;
  if (!agentId) { console.error("usage: node agentguild_verify.mjs <agent_id> [host]"); process.exit(2); }
  vet(agentId, host || DEFAULT_HOST).then((d) => console.log(JSON.stringify(d, null, 2)));
}
