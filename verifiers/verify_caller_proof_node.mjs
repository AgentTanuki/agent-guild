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

const path = process.argv[2] || "caller_proof_vector.json";
const vec = JSON.parse(readFileSync(path, "utf8"));
await verifyCallerProof(vec.caller_proof);
await verifyWalletBinding(vec.wallet_binding);
console.log("ALL INDEPENDENT NODE CHECKS PASSED");
