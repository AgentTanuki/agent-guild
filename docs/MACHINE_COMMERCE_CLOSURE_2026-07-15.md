# Machine Commerce Closure Sprint — 2026-07-15

Goal: let an unaffiliated machine discover a paid Agent Guild trust operation,
pay through x402 with no account and no human, receive the result, verify a
signed offer and receipt, retry safely without paying twice, and leave an
independently verifiable revenue record — and **close the routes by which the
same paid information was available free.**

Nothing here is a payment. Real revenue is still $0: production
`/billing/revenue.real_settlement` records no independently confirmed
Base-mainnet settlement, and nothing in this sprint changes that.

## 1. Root causes reproduced

**Wrong-resource 402 binding.** `GET /search?capability=code-review` answered
with a 402 whose `resource.url` was `…/check`, not the actual request.
Reproduced against production (read-only):

```
$ curl -s "https://agent-guild-5d5r.onrender.com/search?capability=code-review" | jq .detail.resource.url
"https://agent-guild-5d5r.onrender.com/check"
```

Cause: payments were bound to a per-**capability** canonical URL
(`x402.RESOURCE_PATHS[endpoint]`), so every priced read that mapped to the
`best_agent` operation quoted `/check`; `{id}` templates never resolved to the
agent actually being read; and result-affecting query parameters were not part
of the binding at all.

**Free cross-protocol bypass.** Only HTTP reads were metered. A2A
`check: <capability>` returned the full AGD-1 decision for free, and the MCP
trust tools (`guild_check` / `guild_search` / `guild_best_agent` /
`guild_risk_score`) recorded `paid=false` while serving the identical payload.
The same information had one price on one transport and no price on two others.

## 2. Exact paid bypasses closed

A single shared paid-operation gateway (`app/payments.py`) now mediates HTTP,
MCP and A2A: **one semantic operation, one price, one enforcement policy on
every transport.** `authorize()` decides x402-settle / sandbox-credit / free
(soft-launch) / 402-challenge identically regardless of transport.

Every quote and acceptance binds to a `PaidRequest`:
- trusted **configured** public origin (`GUILD_PUBLIC_HOST`) — never a `Host`
  or `X-Forwarded-*` header;
- the actual HTTP method;
- the concrete path with real agent ids (no `{id}` templates);
- canonically-encoded, default-applied, result-affecting query parameters;
- amount, asset, network, recipient;
- the EIP-3009 expiry window and single-use nonce.

`x402.check_binding` rejects path substitution, query mutation, agent-id
substitution, method changes, hostile `Host` headers, price/recipient/asset/
network substitution, expired/not-yet-valid authorizations, replay and double
settlement. Legacy x402 **v1** (`X-PAYMENT`) is now **refused on priced HTTP
routes** with a machine-readable migration note (a v1 payload carries no
resource echo, so it cannot be exactly bound); the v1→v2 translation survives
only for the A2A task-correlated path, where the server binds to its stored
quote.

- **A2A**: official Google A2A-x402 v0.1 flow (`app/a2a_x402.py`) —
  `payment-required` Task → `payment-submitted` → `payment-completed`, decision
  delivered as an artifact plus the signed receipt. Extension declared in
  `/.well-known/agent-card.json` (only when the rail is enabled AND enforced).
  An unpaid A2A caller receives a payment-required Task, never the decision.
- **MCP**: the current official x402 MCP integration targets raw-`mcp`-SDK
  servers; the Guild server is FastMCP-based, so there is no supported drop-in.
  Rather than invent a pseudo-standard, the gated tools return a complete v2
  `PaymentRequired` challenge for the **canonical HTTP resource** and speak the
  official x402 MCP meta convention (`_meta['x402/payment']` in →
  `_meta['x402/payment-response']` out). An official-SDK MCP client follows it
  automatically (proven in `tests/test_mcp_x402.py`). Unpaid MCP callers never
  receive the paid payload. The no-account sandbox trial remains, explicitly
  labelled `credits_sandbox`, never revenue.

Genuinely free operations stay free: registration, evidence writes, proving,
passports, verification, capability listings, self-reads, guest utilities.

## 3. Reliability + trust artifacts

**payment-identifier** (official idempotency extension): identifiers persist
across restarts (`store.x402_payment_ids`), bound to payer + exact request hash
+ payload fingerprint + settlement + result hash. Same id + same request
returns the cached result with **no second settlement**; a different payer,
resource, parameters or payment fails closed (HTTP 409). Reserved **before**
settlement, so a crash between settlement and serving can never pay twice.
Restart recovery and concurrent-duplicate races are tested.

**offer-receipt** (official signed offer/receipt extension, `app/x402_artifacts.py`):
every 402 carries a JWS-signed offer; every served payment returns a JWS-signed
receipt plus an Agent Guild **namespaced evidence attachment**
(`io.agent-guild/evidence`, a sibling extension carrying the response hash,
request hash and AGI-1 checkpoint pin — the standard `offer-receipt` fields are
never altered). Signed by the **persistent Guild service identity** (did:key
Ed25519, the AGI-1 issuer) via JWS `alg:EdDSA`, `kid:<did:key…>#…` — **never**
the treasury key; the key binding is published at
`/.well-known/agent-guild-did.json`. did:web is not required (the spec permits
an external key registry), so existing AGI-1/did:key verification is untouched.

## 4. Standards and exact versions

- x402 protocol v2; Python SDK `x402==2.15.0` (pinned).
- x402 extensions: `bazaar`, `payment-identifier`, offer/receipt
  (`extension-offer-and-receipt`, payload schema v1, JWS branch).
- A2A x402 extension **v0.1** (`https://github.com/google-a2a/a2a-x402/v0.1`).
- MCP over FastMCP `3.4.4` (pinned); official x402 MCP meta keys.
- Independent verifier: official TypeScript `@x402/extensions` (JWS offer +
  receipt verification with did:key resolution).

## 5. Independent interop evidence

- **x402 HTTP interop** (`tests_x402_interop/`): the OFFICIAL `x402` SDK client
  with a real EVM signer pays end-to-end over real HTTP in a CLEAN venv, and
  asserts the challenge now quotes the exact `/search?capability=…` resource
  (not `/check`). Tamper + replay rejected. **4 passed.**
- **MCP interop** (`tests/test_mcp_x402.py`): an MCP client builds the payment
  with the official x402 SDK types + `attach_payment_to_meta`, echoes the
  canonical resource, retries via `_meta['x402/payment']`, and returns with the
  verified result + signed receipt.
- **Signed offer/receipt** (`tests/test_signed_offer_receipt.py` +
  `verifiers/x402_offer_receipt_verify.mjs`): the independent official
  TypeScript verifier confirms valid offers/receipts verify and tampered /
  wrong-key artifacts are rejected — "ALL OFFER/RECEIPT CASES PASSED
  (independent official verifier)".
- **VC verifiers**: existing independent Python + `@digitalbazaar` Node
  eddsa-jcs-2022 verifiers still pass (no regression).

## 6. Complete test totals

| Suite | Result |
|---|---|
| Guild suite, JSON store | 620 passed, 1 skipped |
| Guild suite, SQLite store | 620 passed, 1 skipped |
| Strict-KDF credential suites | 27 passed |
| x402 interop (clean venv, official EVM client) | 4 passed |
| Trust-plane tests + conformance | 38 passed, 9 skipped |
| Independent VC verifiers (Python + Node) | pass |
| Independent x402 offer/receipt verifier (Node) | 6/6 cases pass |
| Contract conformance | no drift |

New suites: `test_x402_v2.py` (rewritten for exact-resource binding + full
attack matrix), `test_mcp_x402.py`, `test_a2a_x402.py`,
`test_payment_identifier.py`, `test_signed_offer_receipt.py`,
`test_first_party_canary.py`.

## 7. Autonomous first-payment canary

`live/scripts/first_party_canary.py` — secret-silent, one-shot, **dry-run by
default**. Uses the official x402 client + a locally-supplied ignored key,
hard-caps at **0.01 USDC lifetime**, discovers the paid resource from the
published machine surfaces, verifies readiness / Base chain id / canonical USDC
/ exact price / pinned treasury before signing, refuses any unexpected
recipient/network/asset/amount/resource/production-SHA, persists its
payment-identifier + signed payload **before** paying (a crash re-sends the
identical bytes → cached result, never a second settlement), verifies the
result + PAYMENT-RESPONSE + signed offer + signed receipt + independent Base
receipt + exact USDC transfer, and confirms `real_settlement.transactions`
rose by one and contains the tx hash. Emits a public-only evidence artifact
labelled `first_party_mainnet_canary` (sample dry-run at
`artifacts/first_party_mainnet_canary_evidence.json`). **Execution mode was NOT
run this session.**

## 8. Unresolved blockers (unsoftened)

- **Not deployed.** Everything here is committed locally and un-pushed.
  Production still runs the old code — the `/search` → `/check` defect is still
  live in prod until this is pushed and released.
- **Framework matrix not re-run per-framework.** The CrewAI / LangChain /
  LangGraph / OpenAI-Agents / MCP integration suites need their own isolated
  venvs (CrewAI pins `mcp~=1.26` vs the proxy's 1.28). Those adapters are
  untouched by this sprint; the native-integration tests skip cleanly in the
  core venv (1 passed, 6 skipped). The full per-framework matrix must be run in
  CI before release.
- **Canary execution unproven end-to-end.** `--execute` has never run: it
  needs a Ramp-funded buyer wallet. The settlement/confirmation/revenue-delta
  path is covered only by unit tests + the dry run until then.
- **Real revenue is $0** and must stay uncalled until production
  `/billing/revenue` records an independently confirmed Base-mainnet
  settlement.

## 9. Single command Ross runs after Ramp funds arrive

After this branch is pushed, released, and the buyer wallet is funded (≥ 0.01
USDC on Base), from the repo root:

First, the secret-silent preflight (validates the protected key file's
existence/permissions/format, derives ONLY the public buyer address, and
checks live readiness — no signature, no payment):

```
python live/scripts/first_party_canary.py --preflight
```

Then the single execution command (the key lives at
`live/secrets/x402_mainnet_canary.key` — the script's default; gitignored,
mode 0600):

```
python live/scripts/first_party_canary.py --execute --watch \
    --key-file live/secrets/x402_mainnet_canary.key \
    --expect-sha "$(git rev-parse HEAD)"
```

It waits for funding, pays exactly one trust decision (≤ 0.01 USDC), verifies
the whole loop, and writes `artifacts/first_party_mainnet_canary_evidence.json`.
Only then, if `real_settlement.transactions` increased by one, is there a first
confirmed settlement — and it is `first_party_mainnet_canary`, not external
adoption and not customer revenue.
