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

/** Verify a Guild-style JCS + Ed25519 document carrying a hex `proof` field. */
export function verifyJcsDocument(document, { expectedIssuer = null } = {}) {
  try {
    if (!document || typeof document !== "object"
        || typeof document.proof !== "string") return false;
    const issuer = document.issuer || "";
    if (!issuer || (expectedIssuer && issuer !== expectedIssuer)) return false;
    const { proof, ...body } = document;
    return verifySig(body, proof, publicKeyFromDid(issuer));
  } catch { return false; }
}

function multibaseB58Decode(s) {
  if (!s.startsWith("z")) throw new Error("not base58btc multibase");
  return b58decode(s.slice(1));
}

// Conforming W3C Data Integrity, cryptosuite eddsa-jcs-2022
// (https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022): JCS canonicalisation,
// hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(document)), Ed25519,
// base58btc-multibase proofValue.
function verifyDataIntegrity(vc) {
  const proof = vc.proof || {};
  if (proof.cryptosuite !== "eddsa-jcs-2022" || !proof.proofValue) return false;
  const { proofValue, ...proofConfig } = proof;
  const { proof: _omit, ...document } = vc;
  if ("@context" in proofConfig
      && canon(proofConfig["@context"]) !== canon(document["@context"] ?? null)) return false;
  const vm = proof.verificationMethod || "";
  const did = vm ? vm.split("#", 1)[0] : (vc.issuer || "");
  if (vc.issuer && did !== vc.issuer) return false;
  const { createHash } = awaitlessCrypto();
  const hashData = Buffer.concat([
    createHash("sha256").update(Buffer.from(canon(proofConfig), "utf8")).digest(),
    createHash("sha256").update(Buffer.from(canon(document), "utf8")).digest(),
  ]);
  try {
    return edVerify(null, hashData, edKey(publicKeyFromDid(did)),
                    multibaseB58Decode(proofValue));
  } catch { return false; }
}

// node:crypto is already imported statically; tiny indirection keeps the
// data-integrity path self-describing.
import { createHash as _createHash } from "node:crypto";
function awaitlessCrypto() { return { createHash: _createHash }; }

export function verifyCredential(vc) {
  try {
    const proof = vc.proof || {};
    // Conforming DataIntegrityProof (all newly issued credentials).
    if (proof.type === "DataIntegrityProof") return verifyDataIntegrity(vc);
    // AGI-1 legacy format (historical credentials only): hex signature over
    // the credential with proof-sans-proofValue embedded.
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

/** Evidence v2's portable subset; Unicode values, ASCII keys, safe integers. */
export function evidenceCanonical(value) {
  if (Array.isArray(value)) value.forEach(evidenceCanonical);
  else if (value !== null && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (!/^[\x00-\x7f]*$/.test(key)) throw new Error("evidence field names must be ASCII");
      evidenceCanonical(item);
    }
  } else if (typeof value === "number" && !Number.isSafeInteger(value)) {
    throw new Error("evidence numbers must be safe integers");
  }
  return canon(value);
}

const evidenceDigest = (value) => _createHash("sha256").update(evidenceCanonical(value)).digest("hex");
const omit = (obj, keys) => Object.fromEntries(Object.entries(obj).filter(([k]) => !keys.includes(k)));

function evidenceInclusion(body, pub) {
  const anchor = body.ledger_anchor;
  const digest = evidenceDigest(omit(body, ["ledger_anchor"]));
  const entry = anchor.checkpoint_entry, cp = entry.checkpoint;
  const inclusion = anchor.inclusion, record = inclusion.record;
  if (!(anchor.snapshot_sha256 === digest && record.type === "evidence_commitment"
    && canon(record.body) === canon({ version: 2, snapshot_sha256: digest })
    && record.actor_did === body.issuer && cp.issuer === body.issuer
    && verifyCheckpoint(cp) && verifySig(omit(entry, ["entry_proof"]), entry.entry_proof, pub)
    && entry.index === anchor.checkpoint_index && entry.index === inclusion.checkpoint_index
    && entry.ledger_length === cp.count && cp.head_hash === anchor.head_hash
    && cp.head_hash === inclusion.checkpoint_head_hash && entry.published_at === anchor.published_at
    && cp.merkle_root === inclusion.checkpoint_merkle_root)) return false;
  let h = evidenceDigest(omit(record, ["hash", "id"]));
  if (record.hash !== h || record.id !== "evt_" + h.slice(0, 12)) return false;
  let index = inclusion.seq, width = cp.count, p = 0;
  if (!Number.isSafeInteger(index) || !Number.isSafeInteger(width)
    || index < 0 || index >= width || record.seq !== index) return false;
  while (width > 1) {
    const step = inclusion.path[p++], side = index % 2 ? "left" : "right";
    if (step.position !== side || !/^[0-9a-f]{64}$/.test(step.hash)) return false;
    if (index === width - 1 && width % 2 && step.hash !== h) return false;
    h = _createHash("sha256").update(side === "left" ? step.hash + h : h + step.hash).digest("hex");
    index = Math.floor(index / 2); width = Math.ceil(width / 2);
  }
  return p === inclusion.path.length && h === cp.merkle_root;
}

/** Offline v2 verification. Pin expectedIssuer from trusted local configuration.
 * Pass expectedEndpoint and expectedAudience to bind evidence to your task.
 * No network calls; no inference that a signed observation guarantees safety.
 */
export function verifyEvidenceBundle(bundle, { expectedIssuer, expectedEndpoint = null,
  expectedAudience = null, now = new Date() } = {}) {
  const out = { valid: false, signature_valid: false, checksum_valid: false,
    ledger_inclusion_valid: false, expired: true };
  try {
    if (!expectedIssuer || bundle.issuer !== expectedIssuer)
      return { ...out, reason: "issuer does not match caller's trusted issuer" };
    const body = omit(bundle, ["proof", "bundle_sha256"]), pub = publicKeyFromDid(bundle.issuer);
    out.signature_valid = verifySig(body, bundle.proof, pub);
    if (bundle.type !== "AgentGuildEvidenceBundle" || bundle.version !== 2)
      return { ...out, reason: "v2 required for observation inclusion verification" };
    const issued = Date.parse(bundle.issued_at), expires = Date.parse(bundle.valid_until);
    const at = now instanceof Date ? now.getTime() : NaN;
    const zoned = (v) => typeof v === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(v);
    out.expired = !Number.isFinite(expires) || at >= expires;
    const timeValid = zoned(bundle.issued_at) && zoned(bundle.valid_until) && issued <= at && at < expires;
    const bindingValid = (expectedEndpoint === null || expectedEndpoint === bundle.requested_endpoint)
      && (expectedAudience === null || expectedAudience === bundle.audience);
    out.checksum_valid = evidenceDigest(omit(bundle, ["bundle_sha256"])) === bundle.bundle_sha256;
    out.ledger_inclusion_valid = evidenceInclusion(body, pub);
    const structureValid = typeof bundle.requested_endpoint === "string" && !!bundle.requested_endpoint
      && typeof bundle.commitment_nonce === "string" && /^[0-9a-f]{32}$/.test(bundle.commitment_nonce);
    Object.assign(out, { issuer: bundle.issuer, subject_endpoint: bundle.subject_endpoint,
      requested_endpoint: bundle.requested_endpoint, issued_at: bundle.issued_at,
      policy_decision: bundle.policy?.decision, request_binding_valid: bindingValid, version: 2 });
    out.valid = !!(out.signature_valid && out.checksum_valid && out.ledger_inclusion_valid
      && bindingValid && timeValid && structureValid);
    out.note = "origin and integrity only; observations are not a safety guarantee. Expiry does not erase historical evidence.";
  } catch { out.reason = "malformed or incomplete evidence"; }
  return out;
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

export function verifyMachineEnvelope(envelope,
                                      { expectedIssuer = null, now = new Date() } = {}) {
  try {
    if (envelope.type !== "AgentGuildMachineEnvelope"
        || envelope.protocol !== "agent-guild/machine-envelope/v1") {
      throw new Error("unsupported envelope");
    }
    const issuer = envelope.issuer || "";
    const { envelope_sha256: claimedDigest, ...withoutDigest } = envelope;
    const { proof, ...signed } = withoutDigest;
    if (typeof proof !== "string" || typeof claimedDigest !== "string") {
      throw new Error("missing proof/digest");
    }
    const digestValid = _createHash("sha256")
      .update(Buffer.from(canon(withoutDigest), "utf8")).digest("hex") === claimedDigest;
    const signatureValid = verifySig(signed, proof, publicKeyFromDid(issuer));
    const validUntil = new Date(envelope.valid_until);
    const expired = !Number.isFinite(validUntil.getTime()) || now > validUntil;
    const issuerMatches = expectedIssuer ? issuer === expectedIssuer : null;
    return {
      valid: signatureValid && digestValid && !expired && issuerMatches !== false,
      signatureValid, digestValid, expired, issuer, issuerMatches,
      senderDid: envelope.sender?.did ?? null,
      recipient: envelope.message?.recipient ?? null,
      payloadSha256: envelope.message?.payload_sha256 ?? null,
      note: "Integrity/provenance only; this does not attest payload truth, recipient acceptance or settlement.",
    };
  } catch {
    return { valid: false, signatureValid: false, digestValid: false,
             expired: true, issuer: envelope?.issuer ?? "" };
  }
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
