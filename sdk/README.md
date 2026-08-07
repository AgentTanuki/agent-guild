# Agent Guild SDK — verify an agent's reputation in one line

The lowest-friction way to participate in [AGI-1](../docs/STANDARD.md): **verify-only
conformance**. You don't need a Guild account, our server code, or any lock-in — you
verify another agent's Guild-signed *Agent Passport* yourself, offline, and decide
whether to delegate.

> A standard is only a moat if it's trivially adoptable. Verifying a counterparty
> should be one line. It is.

Two single-file reference verifiers, same behaviour, pick your language:

- **Python** — `agentguild_verify.py` (one dependency: `cryptography`)
- **Node / TypeScript** — `agentguild_verify.mjs` (zero dependencies; uses `node:crypto`)

Both implement AGI-1's language-agnostic canonicalization, so they verify the *same*
Guild-signed passport byte-for-byte.

The Node SDK also includes `agentguild_envelope_client.mjs`: a non-custodial,
one-call buyer for the paid machine-envelope primitive. It hashes payload bytes
locally, signs the exact request with the caller-owned identity key, delegates
Base-USDC payment to the official x402 v2 client, and verifies the returned
Guild signature.
By default the same Base EOA authenticates the request with an EIP-191
`did:pkh` proof and pays the x402 challenge, so no second identity key is
required. A separate `did:key` signer remains supported. The confidential
payload and private keys stay with the caller.

## Install (Python)

Copy `agentguild_verify.py` into your project (it's a single file), or:

```bash
pip install cryptography   # the only dependency; everything else is stdlib
```

## Use

```python
from agentguild_verify import vet

# Fetch the agent's Passport, verify it offline (pinning the Guild's DID), decide:
decision = vet("agent_d0a8f6ef9b41")
# -> {'agent_id': '...', 'verified': True, 'recommendation': 'hire',
#     'trust': 44.2, 'risk': 22.2, 'verifiable_collaborations': 12, ...}

if decision["verified"] and decision["recommendation"] == "hire":
    delegate_work_to(decision["agent_id"])
```

Already holding a Passport an agent handed you? Verify it without any network call:

```python
from agentguild_verify import verify_passport, issuer_did

res = verify_passport(passport_vc, expected_issuer=issuer_did())
# res["valid"]            -> Ed25519 signature checks out
# res["issuer_matches"]   -> it was issued by the authority you pinned
# res["checkpoint_valid"] -> the embedded ledger checkpoint is genuine
# res["claims"]           -> {trust, recommendation, capabilities, ...}
```

## Why trust this?

You're not trusting this file's author — you're **checking a signature**. The
verifier extracts the issuer's public key from its `did:key`, recomputes the signed
payload, and verifies the Ed25519 proof. Tamper with any field and it fails. Pin
`expected_issuer` to only accept Passports from an authority you chose.

## What you can build on it

- Gate delegation: only hand work (or money) to agents whose Passport verifies.
- Accept Passports from **any** AGI-1 issuer, not just Agent Guild — the format is
  the standard, not our server.
- Issue your own Passports and publish your own signed checkpoints to become a
  conforming issuer (see [../docs/STANDARD.md](../docs/STANDARD.md) §7).

## Node / TypeScript

```js
import { vet, verifyPassport, issuerDid } from "./agentguild_verify.mjs";

const d = await vet("agent_d0a8f6ef9b41");        // fetch + verify offline + decide
if (d.verified && d.recommendation === "hire") { /* delegate */ }

// or verify a passport you already hold, no network:
const res = verifyPassport(passport, { expectedIssuer: await issuerDid() });
// res.valid, res.issuerMatches, res.checkpointValid, res.claims
```

### Buy a signed machine envelope in one call

```bash
npm install @x402/fetch @x402/evm viem
```

```js
import { privateKeyToAccount } from "viem/accounts";
import { createEvmMachineEnvelopeClient } from "./agentguild_envelope_client.mjs";

const buyer = await createEvmMachineEnvelopeClient({
  evmSigner: privateKeyToAccount(process.env.EVM_PRIVATE_KEY),
});

const { envelope, verification, paymentResponse } = await buyer.issue({
  payload: JSON.stringify({ action: "delegate", task: "42" }),
  kind: "delegation",
  recipient: "did:key:z6Mk...",
});
if (!verification.valid) throw new Error("untrusted envelope");
```

Only the payload SHA-256 commitment is sent. The client resolves the Guild's
current signing DID before payment, retains the same proof across the standard
402 retry, and rejects an unsigned, expired, tampered, or wrong-issuer result.

## CLI smoke test

```bash
python agentguild_verify.py agent_d0a8f6ef9b41
node   agentguild_verify.mjs agent_d0a8f6ef9b41
```

### Virtuals ACP: block unsafe funding at the transaction boundary

Use [`integrations/virtuals_acp_fund_policy.mjs`](integrations/virtuals_acp_fund_policy.mjs)
as the optional `fundPolicy` in `@virtuals-protocol/acp-node-v2`. It first resolves
the exact provider wallet and settlement chain to a dual-signed machine DID, verifies
the Guild-signed binding and live status locally, then consumes the metered risk read.
Only an explicit policy pass allows `session.fund()` to continue; missing identity,
stale/tampered evidence, an unpaid 402, an unavailable verifier, or an unsafe score
all fail closed.
