// sdk/agentguild_verify.mjs
import { createPublicKey, verify as edVerify } from "node:crypto";
import { createHash as _createHash } from "node:crypto";
var DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com";
var B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function b58decode(s) {
  let n = 0n;
  for (const ch of s) {
    const i = B58.indexOf(ch);
    if (i < 0) throw new Error("bad base58");
    n = n * 58n + BigInt(i);
  }
  let hex = n.toString(16);
  if (hex.length % 2) hex = "0" + hex;
  let bytes2 = hex === "0" ? [] : [...Buffer.from(hex, "hex")];
  for (const ch of s) {
    if (ch === "1") bytes2.unshift(0);
    else break;
  }
  return Buffer.from(bytes2);
}
function publicKeyFromDid(did) {
  const mb = did.startsWith("did:key:") ? did.slice(8) : did;
  if (!mb.startsWith("z")) throw new Error("unsupported did multibase");
  const raw = b58decode(mb.slice(1));
  if (raw[0] !== 237 || raw[1] !== 1) throw new Error("did:key is not Ed25519");
  return raw.subarray(2);
}
function edKey(raw32) {
  const der = Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), raw32]);
  return createPublicKey({ key: der, format: "der", type: "spki" });
}
function canon(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  const t = typeof v;
  if (t === "object") {
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
  }
  if (t === "number") {
    if (!Number.isFinite(v)) throw new Error("NaN/Infinity not permitted");
    return JSON.stringify(v);
  }
  return JSON.stringify(v);
}
function verifySig(payload, sigHex, raw32) {
  try {
    return edVerify(
      null,
      Buffer.from(canon(payload), "utf8"),
      edKey(raw32),
      Buffer.from(sigHex, "hex")
    );
  } catch {
    return false;
  }
}
function multibaseB58Decode(s) {
  if (!s.startsWith("z")) throw new Error("not base58btc multibase");
  return b58decode(s.slice(1));
}
function verifyDataIntegrity(vc) {
  const proof = vc.proof || {};
  if (proof.cryptosuite !== "eddsa-jcs-2022" || !proof.proofValue) return false;
  const { proofValue, ...proofConfig } = proof;
  const { proof: _omit, ...document } = vc;
  if ("@context" in proofConfig && canon(proofConfig["@context"]) !== canon(document["@context"] ?? null)) return false;
  const vm = proof.verificationMethod || "";
  const did = vm ? vm.split("#", 1)[0] : vc.issuer || "";
  if (vc.issuer && did !== vc.issuer) return false;
  const { createHash: createHash2 } = awaitlessCrypto();
  const hashData = Buffer.concat([
    createHash2("sha256").update(Buffer.from(canon(proofConfig), "utf8")).digest(),
    createHash2("sha256").update(Buffer.from(canon(document), "utf8")).digest()
  ]);
  try {
    return edVerify(
      null,
      hashData,
      edKey(publicKeyFromDid(did)),
      multibaseB58Decode(proofValue)
    );
  } catch {
    return false;
  }
}
function awaitlessCrypto() {
  return { createHash: _createHash };
}
function verifyCredential(vc) {
  try {
    const proof = vc.proof || {};
    if (proof.type === "DataIntegrityProof") return verifyDataIntegrity(vc);
    if (!proof.proofValue) return false;
    const { proofValue, ...proofRest } = proof;
    const { proof: _omit, ...rest } = vc;
    const payload = { ...rest, proof: proofRest };
    return verifySig(payload, proofValue, publicKeyFromDid(vc.issuer));
  } catch {
    return false;
  }
}
function verifyCheckpoint(cp) {
  try {
    if (typeof cp.proof !== "string") return false;
    const { proof, ...body } = cp;
    return verifySig(body, proof, publicKeyFromDid(cp.issuer));
  } catch {
    return false;
  }
}
function verifyPassport(vc, { expectedIssuer = null } = {}) {
  const valid = verifyCredential(vc);
  const subj = vc && vc.credentialSubject || {};
  const issuer = vc && vc.issuer || "";
  const anchor = valid ? subj.ledger_anchor || {} : {};
  const cp = anchor.checkpoint || null;
  const { id, ...claims } = subj;
  return {
    valid,
    issuer,
    issuerMatches: expectedIssuer ? issuer === expectedIssuer : null,
    subject: subj.id || "",
    claims: valid ? claims : null,
    verifiableCollaborations: anchor.verifiable_collaborations ?? null,
    checkpointValid: cp ? verifyCheckpoint(cp) : null
  };
}
async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  return r.json();
}
async function fetchPassport(agentId, host = DEFAULT_HOST) {
  return getJson(`${host}/agents/${agentId}/passport`);
}
async function issuerDid(host = DEFAULT_HOST) {
  return (await getJson(`${host}/.well-known/agent-guild-did.json`)).did;
}
async function vet(agentId, host = DEFAULT_HOST, { pinIssuer = true } = {}) {
  const vc = await fetchPassport(agentId, host);
  const expected = pinIssuer ? await issuerDid(host) : null;
  const res = verifyPassport(vc, { expectedIssuer: expected });
  const claims = res.claims || {};
  const trustworthy = res.valid && (res.issuerMatches === true || res.issuerMatches === null);
  return {
    agentId,
    verified: trustworthy,
    recommendation: trustworthy ? claims.recommendation ?? null : null,
    trust: claims.trust ?? null,
    risk: claims.risk ?? null,
    verifiableCollaborations: res.verifiableCollaborations,
    issuer: res.issuer,
    raw: res
  };
}
if (import.meta.url === `file://${process.argv[1]}`) {
  const [, , agentId, host] = process.argv;
  if (!agentId) {
    console.error("usage: node agentguild_verify.mjs <agent_id> [host]");
    process.exit(2);
  }
  vet(agentId, host || DEFAULT_HOST).then((d) => console.log(JSON.stringify(d, null, 2)));
}

// sdk/agentguild_envelope_client.mjs
import {
  createHash,
  createPrivateKey,
  createPublicKey as createPublicKey2,
  randomBytes,
  sign as edSign
} from "node:crypto";
var CALLER_PROOF_PROTOCOL = "agent-guild/caller-proof/v1";
var EVM_CALLER_PROOF_PROTOCOL = "agent-guild/caller-proof-evm/v1";
var CALLER_PROOF_HEADER = "X-Guild-Caller-Proof";
var BASE_MAINNET_CHAIN_ID = 8453;
var ED25519_PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
var ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
function bytes(value, field) {
  if (typeof value === "string") return Buffer.from(value, "utf8");
  if (value instanceof ArrayBuffer) return Buffer.from(value);
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError(`${field} must be a string, ArrayBuffer or Uint8Array`);
}
function sha256Hex(value) {
  return createHash("sha256").update(value).digest("hex");
}
function signatureHex(value, protocol) {
  if (protocol === EVM_CALLER_PROOF_PROTOCOL) {
    if (typeof value === "string") {
      const hex = value.startsWith("0x") ? value.slice(2) : value;
      if (!/^[0-9a-fA-F]{130}$/.test(hex)) {
        throw new TypeError("EVM signer returned a string that is not a 65-byte signature");
      }
      return "0x" + hex.toLowerCase();
    }
    const out2 = bytes(value, "signature");
    if (out2.length !== 65) throw new TypeError("EVM signature must be 65 bytes");
    return "0x" + out2.toString("hex");
  }
  if (typeof value === "string") {
    if (!/^[0-9a-fA-F]{128}$/.test(value)) {
      throw new TypeError("signer returned a string that is not a 64-byte hex signature");
    }
    return value.toLowerCase();
  }
  const out = bytes(value, "signature");
  if (out.length !== 64) throw new TypeError("Ed25519 signature must be 64 bytes");
  return out.toString("hex");
}
function evmWalletCallerProofSigner(evmSigner, { chainId = BASE_MAINNET_CHAIN_ID } = {}) {
  if (!evmSigner || typeof evmSigner.address !== "string" || typeof evmSigner.signMessage !== "function") {
    throw new TypeError("evmSigner must expose address and async signMessage(...)");
  }
  if (Number(chainId) !== BASE_MAINNET_CHAIN_ID) {
    throw new RangeError("the EVM caller-proof v1 identity is Base-mainnet-only");
  }
  const address = evmSigner.address.toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(address)) {
    throw new TypeError("evmSigner.address must be a 20-byte EVM address");
  }
  const did = `did:pkh:eip155:${BASE_MAINNET_CHAIN_ID}:${address}`;
  return Object.freeze({
    did,
    verificationMethod: `${did}#blockchainAccountId`,
    callerProofProtocol: EVM_CALLER_PROOF_PROTOCOL,
    async sign(message) {
      return evmSigner.signMessage({ message: { raw: bytes(message, "message") } });
    }
  });
}
function proofNonce() {
  return randomBytes(24).toString("base64url");
}
async function createCallerProof({
  signer,
  method = "POST",
  resource = "/envelopes/issue",
  body,
  ttlSeconds = 300,
  nonce = proofNonce(),
  now = Date.now()
}) {
  const protocol = signer?.callerProofProtocol || CALLER_PROOF_PROTOCOL;
  const didKey = protocol === CALLER_PROOF_PROTOCOL && signer?.did?.startsWith("did:key:");
  const evm = protocol === EVM_CALLER_PROOF_PROTOCOL && signer?.did?.startsWith(`did:pkh:eip155:${BASE_MAINNET_CHAIN_ID}:`);
  if (!didKey && !evm || typeof signer?.sign !== "function") {
    throw new TypeError("signer must expose a supported DID, proof protocol and async sign(bytes)");
  }
  const ttl = Math.trunc(Number(ttlSeconds));
  if (!Number.isFinite(ttl) || ttl < 1 || ttl > 600) {
    throw new RangeError("caller-proof ttlSeconds must be 1..600");
  }
  if (typeof nonce !== "string" || nonce.length < 8 || nonce.length > 128) {
    throw new RangeError("caller-proof nonce must be 8..128 characters");
  }
  const iat = Math.floor((now instanceof Date ? now.getTime() : Number(now)) / 1e3);
  const payload = {
    v: protocol,
    did: signer.did,
    method: String(method),
    resource: String(resource),
    body_sha256: sha256Hex(bytes(body, "body")),
    iat,
    exp: iat + ttl,
    nonce,
    aud: "agent-guild"
  };
  const signature = signatureHex(
    await signer.sign(Buffer.from(canon(payload), "utf8")),
    protocol
  );
  return {
    payload,
    signature,
    verificationMethod: signer.verificationMethod || (didKey ? `${signer.did}#${signer.did.slice("did:key:".length)}` : `${signer.did}#blockchainAccountId`)
  };
}
function encodeCallerProof(proof) {
  return Buffer.from(JSON.stringify(proof), "utf8").toString("base64");
}
var PROTECTED_PAYMENT_TIERS = Object.freeze({
  "1000-usdc": "1000000000",
  "10000-usdc": "10000000000",
  "100000-usdc": "100000000000",
  "1000000-usdc": "1000000000000",
  "4000000-usdc": "4000000000000"
});

// sdk/integrations/x402_payment_policy.mjs
function normalizeHost(host) {
  return String(host || DEFAULT_HOST).replace(/\/$/, "");
}
function normalizeAddress(value, label) {
  const out = String(value || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(out)) {
    throw new Error(`${label} is not an exact EVM address`);
  }
  return out;
}
async function getJson2(fetcher, url, init, label) {
  const response = await fetcher(url, init);
  if (response.status === 402) {
    throw new Error(
      `${label} requires payment; configure a separate unguarded x402 meteredFetch or a funded Agent Guild apiKey`
    );
  }
  if (!response.ok) throw new Error(`${label} failed: HTTP ${response.status}`);
  return response.json();
}
function resolveOption(option, context) {
  return typeof option === "function" ? option(context) : option;
}
function samePayment(actual, expected) {
  return actual?.scheme === expected.scheme && actual?.network === expected.network && String(actual?.asset || "").toLowerCase() === expected.asset && actual?.amount === expected.amount && String(actual?.pay_to || "").toLowerCase() === expected.pay_to && actual?.resource === expected.resource;
}
function expectedProtectedDecisionFeeCredits(amountAtomic, {
  feeBps = 25,
  minFeeCredits = 10,
  maxFeeCredits = 1e7
} = {}) {
  const amount = BigInt(amountAtomic);
  const rawAtomic = (amount * BigInt(feeBps) + 9999n) / 10000n;
  const rawCredits = (rawAtomic + 999n) / 1000n;
  return Number(
    rawCredits < BigInt(minFeeCredits) ? BigInt(minFeeCredits) : rawCredits > BigInt(maxFeeCredits) ? BigInt(maxFeeCredits) : rawCredits
  );
}
async function buyDecision({
  base,
  endpointPath,
  decisionFetch,
  apiKey,
  body,
  callerSigner
}) {
  const rawBody = JSON.stringify(body);
  const headers = {
    accept: "application/json",
    "content-type": "application/json"
  };
  if (apiKey) headers["X-API-Key"] = apiKey;
  if (callerSigner) {
    const proof = await createCallerProof({
      signer: callerSigner,
      method: "POST",
      resource: endpointPath,
      body: rawBody
    });
    headers[CALLER_PROOF_HEADER] = encodeCallerProof(proof);
  }
  return getJson2(
    decisionFetch,
    `${base}${endpointPath}`,
    { method: "POST", headers, body: rawBody },
    "Agent Guild signed payment decision"
  );
}
function createAgentGuildX402PaymentPolicy({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch,
  meteredFetch = null,
  apiKey = null,
  capability = null,
  maxRisk = 32.99,
  minConfidence = 0.5,
  ttlSeconds = 300,
  maxAmountAtomic = null,
  pinIssuer = true,
  now = () => /* @__PURE__ */ new Date(),
  onDecision = null,
  protectedValue = false,
  evmSigner = null,
  maxDecisionFeeCredits = 1e7
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("fetchImpl must be a function");
  }
  const decisionFetch = meteredFetch || (apiKey ? fetchImpl : null);
  if (typeof decisionFetch !== "function") {
    throw new TypeError(
      "meteredFetch is required unless a funded Agent Guild apiKey is supplied"
    );
  }
  if (meteredFetch === fetchImpl && !apiKey) {
    throw new TypeError(
      "meteredFetch must be a separate unguarded x402 transport to avoid recursion"
    );
  }
  const base = normalizeHost(host);
  if (protectedValue && !evmSigner) {
    throw new TypeError("evmSigner is required for protectedValue payer continuity");
  }
  if (protectedValue && typeof meteredFetch !== "function") {
    throw new TypeError("protectedValue requires meteredFetch; API keys cannot buy this mainnet-only product");
  }
  const callerSigner = protectedValue ? evmWalletCallerProofSigner(evmSigner) : null;
  return async function agentGuildX402PaymentPolicy(context) {
    try {
      const selected = context?.selectedRequirements || {};
      const resource = String(context?.paymentRequired?.resource?.url || "");
      const expected = {
        scheme: String(selected.scheme || ""),
        network: String(selected.network || ""),
        asset: normalizeAddress(selected.asset, "asset"),
        amount: String(selected.amount || ""),
        pay_to: normalizeAddress(selected.payTo, "payTo"),
        resource
      };
      if (!/^[0-9]+$/.test(expected.amount) || BigInt(expected.amount) <= 0n) {
        throw new Error("amount is not a positive atomic-unit integer string");
      }
      if (!/^https?:\/\//.test(expected.resource)) {
        throw new Error("payment resource is not an http(s) URL");
      }
      if (maxAmountAtomic !== null && BigInt(expected.amount) > BigInt(maxAmountAtomic)) {
        return {
          abort: true,
          reason: "payment exceeds the caller's local maxAmountAtomic policy"
        };
      }
      const requestedCapability = resolveOption(capability, context) || null;
      const body = {
        payment: expected,
        capability: requestedCapability,
        policy: {
          max_risk: Number(maxRisk),
          min_confidence: Number(minConfidence)
        },
        ttl_seconds: Number(ttlSeconds)
      };
      if (protectedValue) {
        const expectedFee = expectedProtectedDecisionFeeCredits(expected.amount);
        if (expectedFee > Number(maxDecisionFeeCredits)) {
          return {
            abort: true,
            reason: "protected-value decision fee exceeds maxDecisionFeeCredits"
          };
        }
      }
      const endpointPath = protectedValue ? "/wallet-binding/protected-decision" : "/wallet-binding/decision";
      const decision = await buyDecision({
        base,
        endpointPath,
        decisionFetch,
        apiKey,
        body,
        callerSigner
      });
      const expectedIssuer = pinIssuer ? (await getJson2(
        fetchImpl,
        `${base}/.well-known/agent-guild-did.json`,
        { headers: { accept: "application/json" } },
        "issuer DID discovery"
      )).did : null;
      const subject = decision?.credentialSubject || {};
      const validFrom = new Date(decision?.validFrom);
      const validUntil = new Date(decision?.validUntil);
      const clock = now();
      const fresh = Number.isFinite(validFrom.getTime()) && Number.isFinite(validUntil.getTime()) && validFrom <= clock && clock <= validUntil;
      const proofValid = verifyCredential(decision) && (!expectedIssuer || decision.issuer === expectedIssuer);
      const exact = samePayment(subject.payment, expected);
      const effective = subject?.policy?.effective || {};
      const policyExact = Number(effective.max_risk) <= Number(maxRisk) && Number(effective.min_confidence) >= Number(minConfidence);
      const capabilityExact = requestedCapability ? subject?.counterparty?.agent?.capabilities?.includes(requestedCapability) : true;
      const expectedFeeCredits = protectedValue ? expectedProtectedDecisionFeeCredits(expected.amount) : null;
      const protection = subject?.protection || {};
      const protectionExact = !protectedValue || protection.contract === "agent-guild/protected-value-policy/v1" && protection?.pricing?.contract === "agent-guild/protected-value-pricing/v1" && protection?.pricing?.basis_points === 25 && protection?.pricing?.minimum_fee_credits === 10 && protection?.pricing?.maximum_fee_credits === 1e7 && protection?.pricing?.fee_credits === expectedFeeCredits && protection?.pricing?.protected_amount_atomic === expected.amount && protection?.service_client?.caller_did === callerSigner.did && protection?.service_client?.payer_eoa === evmSigner.address.toLowerCase() && protection?.reachability?.recommended_for_routing === true && protection?.value_at_risk?.tiers?.[protection.required_value_tier] === true;
      const permitted = proofValid && fresh && exact && policyExact && capabilityExact && subject.contract === "AGPD-1/1.0" && protectionExact && subject.decision === "allow";
      if (typeof onDecision === "function") await onDecision(decision, context);
      if (!permitted) {
        const sealedBlock = proofValid && fresh && exact && policyExact && capabilityExact && subject.contract === "AGPD-1/1.0" && protectionExact && subject.decision !== "allow";
        return {
          abort: true,
          reason: sealedBlock && subject.reason ? subject.reason : "signed payment decision was invalid, stale, inexact, or did not allow payment"
        };
      }
      return void 0;
    } catch (error) {
      return {
        abort: true,
        reason: `counterparty payment verification unavailable: ${error?.message || error}`
      };
    }
  };
}

// sdk/integrations/payanagent_payment_policy.entry.mjs
var DEFAULT_MODE = "protected";
function envValue(name) {
  return globalThis.process?.env?.[name];
}
function optionalInteger(value, label) {
  if (value === void 0 || value === null || value === "") return null;
  const out = String(value);
  if (!/^[0-9]+$/.test(out)) {
    throw new TypeError(`${label} must be an unsigned integer string`);
  }
  return out;
}
function finiteNumber(value, fallback, label) {
  const out = value === void 0 || value === null || value === "" ? Number(fallback) : Number(value);
  if (!Number.isFinite(out)) throw new TypeError(`${label} must be finite`);
  return out;
}
function positiveInteger(value, fallback, label) {
  const out = finiteNumber(value, fallback, label);
  if (!Number.isSafeInteger(out) || out <= 0) {
    throw new TypeError(`${label} must be a positive safe integer`);
  }
  return out;
}
function createPaymentPolicy(context, options = {}) {
  if (!context?.signer || typeof context.signer.address !== "string" || typeof context.signer.signMessage !== "function") {
    throw new TypeError("PayanAgent policy context must expose an EVM signer");
  }
  if (typeof context.createUnguardedPaidFetch !== "function") {
    throw new TypeError(
      "PayanAgent policy context must expose createUnguardedPaidFetch()"
    );
  }
  const mode = String(
    options.mode ?? envValue("AGENT_GUILD_PAYAN_POLICY_MODE") ?? DEFAULT_MODE
  ).toLowerCase();
  if (!(/* @__PURE__ */ new Set(["protected", "standard"])).has(mode)) {
    throw new TypeError(
      "AGENT_GUILD_PAYAN_POLICY_MODE must be protected or standard"
    );
  }
  const maxAmountAtomic = optionalInteger(
    options.maxAmountAtomic ?? envValue("AGENT_GUILD_PAYAN_MAX_AMOUNT_ATOMIC"),
    "AGENT_GUILD_PAYAN_MAX_AMOUNT_ATOMIC"
  );
  const maxDecisionFeeCredits = positiveInteger(
    options.maxDecisionFeeCredits ?? envValue("AGENT_GUILD_PAYAN_MAX_DECISION_FEE_CREDITS"),
    1e7,
    "AGENT_GUILD_PAYAN_MAX_DECISION_FEE_CREDITS"
  );
  const maxRisk = finiteNumber(
    options.maxRisk ?? envValue("AGENT_GUILD_PAYAN_MAX_RISK"),
    32.99,
    "AGENT_GUILD_PAYAN_MAX_RISK"
  );
  const minConfidence = finiteNumber(
    options.minConfidence ?? envValue("AGENT_GUILD_PAYAN_MIN_CONFIDENCE"),
    0.5,
    "AGENT_GUILD_PAYAN_MIN_CONFIDENCE"
  );
  if (maxRisk < 0 || maxRisk > 100) {
    throw new RangeError("AGENT_GUILD_PAYAN_MAX_RISK must be between 0 and 100");
  }
  if (minConfidence < 0 || minConfidence > 1) {
    throw new RangeError(
      "AGENT_GUILD_PAYAN_MIN_CONFIDENCE must be between 0 and 1"
    );
  }
  return createAgentGuildX402PaymentPolicy({
    host: options.host,
    fetchImpl: options.fetchImpl ?? context.fetchImpl ?? globalThis.fetch,
    meteredFetch: context.createUnguardedPaidFetch(),
    maxAmountAtomic,
    maxDecisionFeeCredits,
    maxRisk,
    minConfidence,
    protectedValue: mode === "protected",
    evmSigner: mode === "protected" ? context.signer : null,
    onDecision: options.onDecision,
    now: options.now
  });
}
var payanagent_payment_policy_entry_default = createPaymentPolicy;
export {
  createPaymentPolicy,
  payanagent_payment_policy_entry_default as default
};
