# Endpoint evidence a machine can keep and verify

`POST /evidence/bundle` returns version 2. Send the exact `url`, optional
`audience`, and `ttl_seconds` in JSON. An unpaid request returns its own x402
quote, bound to all three effective inputs. Use the live quote's price and
payment requirements. No account or interactive checkout is required for x402.
Credit balances are a sandbox rail, never cash revenue.

MCP clients can complete the same purchase without another HTTP connection:

1. Call `guild_evidence_bundle(url, ttl_seconds=3600, audience="<task>")`.
   The unpaid tool error contains the exact quote in both JSON text and
   `structuredContent`.
2. Sign that quote using your existing authorized payment capability. Retry the
   same tool and inputs with `_meta["x402/payment"]`, or the schema-visible
   `x402_payment` argument if your client cannot send request metadata.
3. Retain the returned bundle and `_meta["x402/payment-response"]` receipt.
   `guild_evidence_verify(bundle, expected_endpoint=url,
   expected_audience="<task>")` is free. Offline verification remains available
   below and does not depend on Guild availability.

The MCP and HTTP forms share the exact price, payment binding and saved purchase.
A completed x402 purchase can be recovered through either transport with its
original payment credential. MCP's text contains the artifact JSON itself;
inbox messages and other unsigned fields are never appended to the signed bundle.
For a live check without retained evidence, use free `guild_preflight(url)`.

The buyer receives an observation, the Guild's explicit policy and limitations,
the index history, a signature, and a proof that the observation's commitment is
included in a signed ledger checkpoint. A successful cryptographic verification
proves the issuer's commitment and integrity. It does **not** prove the observation
true, the endpoint safe, or that an independent party witnessed the checkpoint.
Timestamps are the issuer's assertions. A self-contained bundle cannot detect an
issuer handing different buyers separate internally consistent ledger views.
Independent checkpoint retention/comparison is needed for that threat; public
checkpoints are available at `/ledger/checkpoints`. This product adds no external
timestamping or independent witness.

## Verify before relying on a bundle

The standalone files are served free at `/sdk/agentguild_verify.py` and
`/sdk/agentguild_verify.mjs`. Python requires `cryptography`; Node uses built-ins.
Pin the issuer through your own trusted configuration. Do not learn a trusted
issuer by copying the untrusted bundle's `issuer` field.

```python
from agentguild_verify import verify_evidence_bundle
result = verify_evidence_bundle(
    bundle, expected_issuer=trusted_guild_did,
    expected_endpoint=request_url, expected_audience=task_audience)
if not result["valid"] or not result["ledger_inclusion_valid"]:
    raise ValueError("invalid evidence")
```

```javascript
import { verifyEvidenceBundle } from "./agentguild_verify.mjs";
const result = verifyEvidenceBundle(bundle, {
  expectedIssuer: trustedGuildDid,
  expectedEndpoint: requestUrl,
  expectedAudience: taskAudience,
});
if (!result.valid || !result.ledger_inclusion_valid) throw new Error("invalid evidence");
```

`requested_endpoint` preserves the complete trimmed request URL, including query
and trailing slash. `subject_endpoint` and `subject_id` identify the existing
normalized index group; its history can merge those URL variants. The observation
is made for the requested URL. Expiry limits current use; a correctly signed but
expired bundle can still describe the historical observation.

## The bytes and proof

Version 2 uses raw Ed25519 with a hexadecimal signature over AGI-1 canonical JSON,
**not** the W3C Data Integrity `eddsa-jcs-2022` suite. This artifact restricts field
names to ASCII and numbers to JavaScript-safe integers. Unicode string values
remain supported. Unsupported values refuse issuance before charging instead of
silently producing different Python and JavaScript signature bytes. Existing
passport and checkpoint encodings are unchanged.

1. The snapshot core is the bundle without `proof`, `bundle_sha256`, or
   `ledger_anchor`. It includes a fresh 128-bit `commitment_nonce`.
2. The ledger receives an `evidence_commitment` event whose body contains only
   `version: 2` and the SHA-256 digest of the canonical snapshot core.
3. A newly published checkpoint commits that event. The bundle carries the
   complete event, its Merkle path, the signed checkpoint and signed feed entry.
4. The outer `proof` signs the bundle without **both** `proof` and
   `bundle_sha256`. The checksum covers the bundle including `proof`, excluding
   only `bundle_sha256`.

Verification checks the snapshot digest, record content hash and identity,
position-bounded Merkle path (including odd-leaf duplication), checkpoint and feed
signatures, issuer, exact request/audience, checksum and time window. The public
ledger does not receive the nonce, endpoint, audience or observation. The salt
prevents guessing those contents from the public digest. Publication time and
event count remain visible; this is not a traffic-anonymity claim. Sharing the
bundle reveals its contents to the recipient.

## Payment and recovery boundaries

An eligibility phase checks a funded sandbox balance or performs facilitator
verification without settlement. Invalid credentials cannot start new probes or
commitments. The normal payment gateway still rechecks and settles **after** the
complete bundle passes artifact validation. Eligibility is not a funds reservation;
concurrent spending can still make the final settlement fail. An issuance failure
never charges. A failure after a hash-only append can leave an unpurchased
commitment; such events must never be counted as revenue or adoption.
The existing abuse-control system bounds new issuance to 120 attempts per IP per
hour by default (`GUILD_RL_EVIDENCE_ISSUE` and its `_WINDOW_S` setting). This is a
per-process burst limit, not a lifetime storage quota or Sybil defense. Recovery
of an already completed purchase bypasses this new-issuance limit.

An x402 retry of a completed purchase matches the stored payer, exact request
hash and full payment-payload fingerprint before returning the saved bytes and
receipt. It needs no new observation, signer, checkpoint publication or facilitator
call. Expiration of that payment's settlement authorization does not revoke access
to the completed result. Changed payloads or request scope fail closed. Mainnet
derives a durable recovery identifier even when the client omitted one; testnet
needs the optional payment-identifier extension for this cached-result behavior.
Completed records are not removed by abandoned-payment garbage collection.
The exact original payment credential acts as a bearer capability for this one
result. Protect it accordingly: a wallet address alone cannot retrieve the bundle,
but anyone who obtains the full credential can replay it. This authentication is
distinct from whether the buyer's ownership has been independently attributed.

Native MPP still authenticates its outer challenge, including its expiry, before
entering this shared path; this change does not promise recovery through an expired
MPP envelope. Credit purchases do not acquire x402 idempotency. An incomplete
purchase still uses the existing settlement-recovery rules. No refund, permanent
hosting SLA, or independent timestamping service is implied.

Legacy v1 bundles remain signature-verifiable through the Python/server verifier,
with `ledger_inclusion_valid: false`. The Node evidence verifier requires v2 for a
successful result. Do not interpret v1's checkpoint reference as proof that its
observation was committed.

## Commercial test

This repairs a product already advertised for sale; it is not evidence that a
customer will pay. Keep the current price and free preflight alternative while
observing actual scoped quotes, payment attempts, confirmed non-internal receipts,
successful delivery and repeat use. Generic catalog probes and internally funded
demonstrations do not establish demand. A meaningful next signal is a machine
buying evidence for its own task and using or buying it again.
