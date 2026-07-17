// INDEPENDENT Node verification of agent-guild/caller-proof/v1 + the
// wallet-binding credential. No Agent Guild code — only third-party libs
// (@noble/ed25519, canonicalize (RFC 8785 JCS), bs58, ethers, js-sha256).
//
//   node verify_caller_proof_node.mjs caller_proof_vector.json
import { readFileSync } from "node:fs";
import canonicalize from "canonicalize";
import bs58 from "bs58";
import { sha256 } from "js-sha256";
import * as ed from "@noble/ed25519";
import { verifyMessage } from "ethers";

function pubFromDid(did) {
  const raw = bs58.decode(did.slice("did:key:".length).slice(1)); // strip 'z'
  if (raw[0] !== 0xed || raw[1] !== 0x01) throw new Error("not ed25519 did:key");
  return raw.slice(2);
}

function hexToBytes(hex) {
  const h = hex.startsWith("0x") ? hex.slice(2) : hex;
  return Uint8Array.from(Buffer.from(h, "hex"));
}

async function edVerifyJcs(payload, sigHex, pub) {
  const msg = new TextEncoder().encode(canonicalize(payload));
  return ed.verifyAsync(hexToBytes(sigHex), msg, pub);
}

async function verifyCallerProof(v) {
  const { payload, signature } = v.envelope;
  if (payload.v !== "agent-guild/caller-proof/v1") throw new Error("version");
  if (payload.aud !== "agent-guild") throw new Error("audience");
  if (payload.did !== v.expected_did) throw new Error("did");
  if (!(await edVerifyJcs(payload, signature, pubFromDid(payload.did))))
    throw new Error("caller-proof signature INVALID");
  const req = v.request;
  if (payload.method !== req.method) throw new Error("method binding");
  if (payload.resource !== req.resource) throw new Error("resource binding");
  if (payload.body_sha256 !== sha256(new TextEncoder().encode(req.body_utf8)))
    throw new Error("body-hash binding");
  console.log("PASS caller-proof: signature + audience + exact request binding");
  for (const k of ["method", "resource", "did", "nonce"]) {
    const t = JSON.parse(JSON.stringify(payload));
    t[k] = "TAMPERED";
    if (await edVerifyJcs(t, signature, pubFromDid(payload.did)))
      throw new Error(`tampering ${k} did NOT break the signature`);
  }
  console.log("PASS caller-proof tamper: mutations all rejected");
}

async function verifyWalletBinding(v) {
  const pub = Uint8Array.from(Buffer.from(v.did_public_key_hex, "hex"));
  if (!(await edVerifyJcs(v.binding, v.did_signature, pub)))
    throw new Error("wallet-binding DID signature INVALID");
  const recovered = verifyMessage(v.message,
    v.evm_signature.startsWith("0x") ? v.evm_signature : "0x" + v.evm_signature);
  if (recovered.toLowerCase() !== v.expected_evm_address.toLowerCase())
    throw new Error("EVM signature recovers a different address");
  console.log("PASS wallet-binding: DID + EVM signatures over the same binding");
}

// The ACTUAL Guild-issued credential: issuer signature, issuer identity,
// validity window and subject fields — OFFLINE cryptographic validity only.
// (Revocation/supersession is LIVE status held by the Guild store; no
// offline verifier can claim it.)
async function verifyIssuedCredential(v) {
  const cred = v.credential, binding = v.binding;
  if (cred.issuer !== v.expected_issuer_did) throw new Error("unexpected issuer");
  const body = Object.fromEntries(
    Object.entries(cred).filter(([k]) => k !== "proof"));
  if (!(await edVerifyJcs(body, cred.proof, pubFromDid(cred.issuer))))
    throw new Error("credential issuer signature INVALID");
  if (cred.type !== "AgentGuildWalletBinding") throw new Error("type");
  if (cred.protocol !== binding.v) throw new Error("protocol mismatch");
  if (cred.did !== binding.did) throw new Error("credential subject DID mismatch");
  if (cred.address.toLowerCase() !== binding.address.toLowerCase())
    throw new Error("credential address mismatch");
  if (cred.network !== binding.network) throw new Error("credential network mismatch");
  const at = v.verified_at;
  if (!(cred.issued_at <= at && at < cred.expires_at))
    throw new Error("credential outside its validity window");
  for (const k of ["did", "address", "network", "expires_at"]) {
    const t = JSON.parse(JSON.stringify(body));
    t[k] = "TAMPERED";
    if (await edVerifyJcs(t, cred.proof, pubFromDid(cred.issuer)))
      throw new Error(`tampering credential.${k} did NOT break the issuer signature`);
  }
  console.log("PASS issued credential: issuer signature + issuer + validity "
    + "window + subject fields (OFFLINE cryptographic validity)");
  console.log("NOTE: revocation/supersession is LIVE Guild-store status — "
    + "NOT verified here and not claimable offline");
}

const path = process.argv[2] || "caller_proof_vector.json";
const vec = JSON.parse(readFileSync(path, "utf8"));
await verifyCallerProof(vec.caller_proof);
await verifyWalletBinding(vec.wallet_binding);
await verifyIssuedCredential(vec.wallet_binding);
console.log("ALL INDEPENDENT NODE CHECKS PASSED");
