// ---------------------------------------------------------------------------
// Real ed25519 cryptography + did:key identity.
// Runs in the browser (and in Node for the verify script) with no backend.
// ---------------------------------------------------------------------------
import * as ed from "@noble/ed25519";
import { sha512 } from "@noble/hashes/sha512";
import { bytesToHex, hexToBytes, utf8ToBytes } from "@noble/hashes/utils";
import { base58 } from "@scure/base";
import type { KeyPair } from "./types";

// @noble/ed25519 v2 needs a sha512 implementation wired in for sync calls.
ed.etc.sha512Sync = (...m) => sha512(ed.etc.concatBytes(...m));

export { bytesToHex, hexToBytes };

/** Generate a fresh ed25519 keypair. `seed` (32 bytes hex) makes it deterministic. */
export function generateKeyPair(seed?: Uint8Array): KeyPair {
  const priv = seed && seed.length === 32 ? seed : ed.utils.randomPrivateKey();
  const pub = ed.getPublicKey(priv);
  return { publicKeyHex: bytesToHex(pub), privateKeyHex: bytesToHex(priv) };
}

// did:key multicodec prefix for ed25519-pub is 0xed 0x01.
const ED25519_MULTICODEC = new Uint8Array([0xed, 0x01]);

/** Derive a did:key string from an ed25519 public key (hex). */
export function didFromPublicKey(publicKeyHex: string): string {
  const pub = hexToBytes(publicKeyHex);
  const prefixed = new Uint8Array(ED25519_MULTICODEC.length + pub.length);
  prefixed.set(ED25519_MULTICODEC, 0);
  prefixed.set(pub, ED25519_MULTICODEC.length);
  // multibase base58btc is the literal prefix 'z' + base58 of the bytes.
  return `did:key:z${base58.encode(prefixed)}`;
}

/** Recover the ed25519 public key (hex) embedded in a did:key. */
export function publicKeyFromDid(did: string): string {
  const mb = did.replace(/^did:key:/, "");
  if (!mb.startsWith("z")) throw new Error("unsupported did multibase");
  const bytes = base58.decode(mb.slice(1));
  if (bytes[0] !== 0xed || bytes[1] !== 0x01) {
    throw new Error("did:key is not ed25519");
  }
  return bytesToHex(bytes.slice(2));
}

/** Deterministic JSON canonicalisation: sorted keys, no whitespace.
 *  (A pragmatic stand-in for full JSON-LD canonicalisation, sufficient and
 *  stable for an MVP — documented as a known simplification.) */
export function canonicalize(value: unknown): string {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(value as Record<string, unknown>).sort()) {
      out[k] = sortKeys((value as Record<string, unknown>)[k]);
    }
    return out;
  }
  return value;
}

/** Sign a canonicalised payload with an ed25519 private key (hex). Returns hex. */
export function signPayload(payload: unknown, privateKeyHex: string): string {
  const msg = utf8ToBytes(canonicalize(payload));
  const sig = ed.sign(msg, hexToBytes(privateKeyHex));
  return bytesToHex(sig);
}

/** Verify a hex signature over a canonicalised payload against a public key. */
export function verifyPayload(
  payload: unknown,
  signatureHex: string,
  publicKeyHex: string,
): boolean {
  try {
    const msg = utf8ToBytes(canonicalize(payload));
    return ed.verify(hexToBytes(signatureHex), msg, hexToBytes(publicKeyHex));
  } catch {
    return false;
  }
}
