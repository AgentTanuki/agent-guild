// Independent, OFFICIAL x402 offer/receipt verifier.
//
// The Agent Guild server is Python; this Node verifier proves the JWS-signed
// offers and receipts it produces are standards-conformant by verifying them
// with the OFFICIAL TypeScript extension package (@x402/extensions —
// verifyOfferSignatureJWS / verifyReceiptSignatureJWS), which resolves the
// did:key `kid` and checks the EdDSA signature independently of any Guild
// code. No Guild verification path is trusted here.
//
// Usage:  node x402_offer_receipt_verify.mjs <vector.json>
// The vector is produced by tests/test_signed_offer_receipt.py::_write_vector.
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

async function verifyOffer(signed) {
  // no publicKey → the verifier extracts it from the JWS `kid` (did:key)
  return await verifyOfferSignatureJWS(signed);
}
async function verifyReceipt(signed) {
  return await verifyReceiptSignatureJWS(signed);
}

async function main() {
  // 1. valid offer verifies and its payload matches the expected fields
  const offer = await verifyOffer(vector.valid_offer);
  check("valid offer verifies (official JWS verifier, did:key resolved)",
        offer && offer.resourceUrl === vector.expected.offer_resource_url &&
        offer.amount === vector.expected.amount &&
        offer.payTo === vector.expected.pay_to);

  // 2. valid receipt verifies and binds the actual resource + payer + tx
  const receipt = await verifyReceipt(vector.valid_receipt);
  check("valid receipt verifies (official JWS verifier)",
        receipt && receipt.resourceUrl === vector.expected.receipt_resource_url &&
        receipt.payer === vector.expected.payer &&
        receipt.transaction === vector.expected.transaction);

  // 3. tampered offer payload must NOT verify
  await expectReject("tampered offer rejected", () =>
    verifyOffer(vector.tampered_offer));

  // 4. tampered receipt payload must NOT verify
  await expectReject("tampered receipt rejected", () =>
    verifyReceipt(vector.tampered_receipt));

  // 5. wrongly-signed offer (different key) must NOT verify
  await expectReject("wrong-key offer rejected", () =>
    verifyOffer(vector.wrong_key_offer));

  // 6. wrongly-signed receipt (different key) must NOT verify
  await expectReject("wrong-key receipt rejected", () =>
    verifyReceipt(vector.wrong_key_receipt));

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
