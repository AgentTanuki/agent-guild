# Taskmarket requester — signed approval before external delegation

This single-file adapter lets an EVM wallet create and monitor a Taskmarket
request without giving Agent Guild or Taskmarket custody of a private key.

The flow is deliberately two-phase:

1. `prepare()` returns the exact public description, deliverables, reward,
   deadline, Base network, USDC asset and maximum combined spend.
2. The host application grants one fresh approval for that exact plan. The
   adapter buys a Guild-signed machine envelope over the plan, then applies an
   AGSM-1 mandate to Taskmarket's exact x402 quote immediately before the
   caller-owned wallet signs it.

The adapter never accepts or rejects a submission. An unknown funding outcome
is reconciled by the plan digest and is never blindly retried.
The current signed-envelope price is read from Agent Guild's live paid catalog
when the client is created; it is not copied into this adapter.

## Install

Download the adapter and its two local imports, or use the repository copies:

```bash
npm install @x402/fetch @x402/evm viem
```

Create a free AGSM-1 mandate first with `POST /mandates`; the same Base EOA
must sign the exact-body caller proof used to create it. Then:

```js
import { privateKeyToAccount } from "viem/accounts";
import {
  createAgentGuildTaskmarketRequester,
} from "./taskmarket_requester.mjs";

const signer = privateKeyToAccount(process.env.EVM_PRIVATE_KEY);
const requester = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: process.env.AGENT_GUILD_MANDATE_ID,
  // Connect this callback to the host's normal user/policy approval surface.
  approve: async plan => {
    console.table({
      reward: plan.spend.taskmarket_funding_atomic,
      maximumTotal: plan.spend.maximum_total_atomic,
      deadline: plan.expected_deadline,
      deliverables: plan.deliverables.join("; "),
    });
    return {
      approved: await getFreshApprovalFromConfiguredPolicy(plan),
      approvalId: plan.approval_id,
    };
  },
});

const plan = requester.prepare({
  description: "Audit the public API for settlement inconsistencies.",
  rewardAtomic: "5000000", // 5 USDC
  durationHours: 48,
  deliverables: [
    "Markdown report with reproducible requests",
    "JSON list of findings and severity",
  ],
  tags: ["security", "api-audit"],
});

// Exactly one application-level funding attempt. If the network result is
// unknown, the adapter throws a non-retriable error with the plan digest.
const result = await requester.create(plan);
console.log(result.task.id);

// Read-only review; acceptance remains a separate explicit Taskmarket action.
const review = await requester.review(result.task.id);
console.log(review.submissions);
```

## Security boundaries

- The signer is an object supplied by the wallet runtime; the adapter never
  asks for, reads, stores or logs raw key material.
- The approval expires after ten minutes by default and its ID must match.
- The public task contains the exact acceptance deliverables and a digest of
  the complete semantic plan.
- The same AGSM-1 mandate caps both the one-cent signed approval receipt and
  the Taskmarket reward at pre-signature time. Its cumulative cap must cover
  the displayed `maximum_total_atomic`.
- A 402 quote is not payment and a successful HTTP response is not settlement
  proof. Use Taskmarket's returned task and Base receipt for final accounting.
- The adapter exposes no automatic submission acceptance or rejection path.
