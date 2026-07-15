// Independent, OFFICIAL x402 offer/receipt verifier.
//
// The Agent Guild server is Python; this Node verifier proves the JWS-signed
// offers and receipts it produces are standards-conformant by verifying them
// with the OFFICIAL TypeScript extension package (@x402/extensions —
// verifyOfferSignatureJWS / verifyReceiptSignatureJWS, pinned exact version).
// The package resolves the `kid` DID URL itself: for did:web it fetches
// {origin}/.well-known/did.json and reads the authorised verification
// method's publicKeyMultibase — so a passing run proves the full chain
// origin → DID document → authorised key → signature with NO Guild
// verification code trusted anywhere.
//
// On top of the package's signature verification this harness enforces the
// RELYING-PARTY origin check: the kid's did:web identity must be the DID of
// the expected resource origin. A hostile-but-internally-valid did:web
// signer (its own domain, its own DID document, its own key) verifies
// cryptographically yet proves nothing about THIS resource — case 7 asserts
// that such an artifact is rejected by the origin binding.
//
// Usage:  node x402_offer_receipt_verify.mjs <vector.json>
// The vector is produced by tests/test_signed_offer_receipt.py.
// Exit 0 = every case behaved as expected; non-zero = a mismatch.

import { readFileSync } from "node:fs";
import {
  verifyOfferSignatureJWS,
  verifyReceiptSignatureJWS,
} from "@x402/extensions";

const path = process.argv[2];
if (!path) {
  console.error("usage: node x402_offer_receipt_verify.mjs <vector.json>");
  process.exit(2);
}
const vector = JSON.parse(readFileSync(path, "utf8"));

let failures = 0;
function check(name, ok) {
  const status = ok ? "ok" : "FAIL";
  if (!ok) failures++;
  console.log(`  [${status}] ${name}`);
}

// did:web DID for an origin (mirror of the W3C did:web mapping: domain is
// the identifier, an explicit port's colon is percent-encoded %3A).
function didWebFromOrigin(origin) {
  const u = new URL(origin);
  let ident = encodeURIComponent(u.hostname);
  if (u.port) ident += "%3A" + u.port;
  return "did:web:" + ident;
}

function jwsKid(signed) {
  const header = JSON.parse(
    Buffer.from(signed.signature.split(".")[0], "base64url").toString("utf8"));
  return header.kid || "";
}

function kidMatchesOrigin(signed, origin) {
  return jwsKid(signed).split("#")[0] === didWebFromOrigin(origin);
}

async function verifyOffer(signed) {
  // no publicKey → the OFFICIAL verifier resolves the kid itself
  // (did:web → fetch {origin}/.well-known/did.json → publicKeyMultibase)
  return await verifyOfferSignatureJWS(signed);
}
async function verifyReceipt(signed) {
  return await verifyReceiptSignatureJWS(signed);
}

async function main() {
  const origin = vector.expected.origin;

  // 1. valid offer: official resolution + signature + payload + ORIGIN bind
  const offer = await verifyOffer(vector.valid_offer);
  check("valid offer verifies (official did:web resolution → signature)",
        offer && offer.resourceUrl === vector.expected.offer_resource_url &&
        offer.amount === vector.expected.amount &&
        offer.payTo === vector.expected.pay_to);
  check("valid offer kid is bound to the resource origin",
        kidMatchesOrigin(vector.valid_offer, origin));

  // 2. valid receipt verifies and binds the actual resource + payer + tx
  const receipt = await verifyReceipt(vector.valid_receipt);
  check("valid receipt verifies (official did:web resolution → signature)",
        receipt && receipt.resourceUrl === vector.expected.receipt_resource_url &&
        receipt.payer === vector.expected.payer &&
        receipt.transaction === vector.expected.transaction);
  check("valid receipt kid is bound to the resource origin",
        kidMatchesOrigin(vector.valid_receipt, origin));

  // 3./4. tampered payloads must NOT verify
  await expectReject("tampered offer rejected", () =>
    verifyOffer(vector.tampered_offer));
  await expectReject("tampered receipt rejected", () =>
    verifyReceipt(vector.tampered_receipt));

  // 5. wrong-key artifacts (foreign signature, Guild kid) must NOT verify:
  // the resolver fetches the GENUINE DID document, whose authorised key
  // does not match the signature.
  await expectReject("wrong-key offer rejected", () =>
    verifyOffer(vector.wrong_key_offer));
  await expectReject("wrong-key receipt rejected", () =>
    verifyReceipt(vector.wrong_key_receipt));

  // 6. key substitution: the kid resolves to a DID document that authorises
  // a DIFFERENT key than the one that signed — must NOT verify.
  if (vector.substituted_key_offer) {
    await expectReject("substituted-key offer rejected", () =>
      verifyOffer(vector.substituted_key_offer));
  }

  // 7. hostile origin: an attacker with their own valid did:web identity
  // signs an offer for OUR resource. The signature itself verifies against
  // the attacker's own DID document — the ORIGIN CHECK must reject it.
  if (vector.hostile_origin_offer) {
    check("hostile-origin offer rejected by the origin binding",
          !kidMatchesOrigin(vector.hostile_origin_offer, origin));
  }

  console.log(failures === 0
    ? "ALL OFFER/RECEIPT CASES PASSED (independent official verifier)"
    : `${failures} CASE(S) FAILED`);
  process.exit(failures === 0 ? 0 : 1);
}

async function expectReject(name, fn) {
  let rejected = false;
  try {
    const payload = await fn();
    // some verifiers return a payload without throwing on a bad sig; a
    // tampered artifact must EITHER throw OR return something that no longer
    // matches — treat a clean successful verify as a failure of the case.
    rejected = payload == null;
  } catch (_e) {
    rejected = true;
  }
  check(name, rejected);
}

main().catch((e) => {
  console.error("verifier crashed:", e);
  process.exit(3);
});
