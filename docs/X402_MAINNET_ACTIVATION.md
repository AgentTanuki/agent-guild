# x402 mainnet activation runbook — USDC on Base via the CDP facilitator

Status: **NOT ACTIVE.** This runbook stages the activation; nothing in the
repository activates mainnet, initiates payments, or touches credentials.
Real revenue is zero until a mainnet settlement is independently confirmed
on-chain, and the service itself enforces that definition
(`/billing/revenue` → `real_settlement`).

## What the code guarantees (implemented + tested, commit this runbook ships in)

* Mainnet (`eip155:8453`) requires the **authenticated Coinbase CDP
  facilitator** (`https://api.cdp.coinbase.com/platform/v2/x402`). Every
  `/verify` and `/settle` carries a fresh request-bound Bearer JWT (EdDSA or
  ES256, 120 s expiry) generated from the CDP API key — the exact format the
  official CDP SDK produces (`app/x402_cdp.py`). The unauthenticated
  `x402.org` facilitator remains **testnet-only** and is rejected for
  mainnet.
* The rail **fails closed** — at startup (a misconfigured mainnet rail
  refuses to boot) and at payment time — on: missing CDP credentials, an
  invalid/zero receiving address, the testnet USDC contract, a testnet
  facilitator, a non-https/local public resource origin, or a non-https
  confirmation RPC.
* A facilitator response is never trusted alone: mainnet settlements are
  **independently confirmed** against a configurable Base RPC
  (`eth_getTransactionReceipt`: status `0x1`, USDC contract
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, `Transfer` event to the
  configured recipient with the exact quoted amount) before the result is
  served or anything is classified as real settlement
  (`app/x402_confirm.py`). If the RPC is unavailable, the payment is
  recorded `settled_unconfirmed`, the result is withheld, and the buyer can
  re-present the same payment for idempotent recovery — never charged twice.
* Replay, resource/price/recipient binding, duplicate-transaction and
  double-settlement guards persist across restarts.
* `GET /x402/readiness` reports enabled/network/asset/recipient/facilitator
  host/config validity — never credentials.

## Secrets and env (Render → Environment; never commit these)

| Variable | Value | Notes |
| --- | --- | --- |
| `CDP_API_KEY_ID` | CDP Secret API key ID | create at portal.cdp.coinbase.com (Ed25519 recommended) |
| `CDP_API_KEY_SECRET` | CDP Secret API key material | base64 Ed25519 (or PEM EC); never logged/served |
| `GUILD_X402_ENABLED` | `1` | master switch |
| `GUILD_X402_PAY_TO` | `0xaa4E3ba0Eb5f564cAb54dDC08f5BaAfb3D4cA8E5` | the dedicated `agent-guild-treasury` wallet (public address). Mainnet is **PINNED in code** to this address (`x402.MAINNET_TREASURY`) — any other value fails closed, so a mistyped/swapped env var can never redirect settlements. Rotating the treasury is a reviewed code change. |
| `GUILD_X402_NETWORK` | `eip155:84532` for stage 2, `eip155:8453` for stage 3+ | CAIP-2 |
| `GUILD_X402_BASE_RPC` | e.g. `https://mainnet.base.org` (default) | independent confirmation endpoint; https required |
| `GUILD_X402_CONFIRM_TIMEOUT` | optional, default 45 (seconds) | receipt-poll bound |

`GUILD_X402_ASSET` and `GUILD_X402_FACILITATOR` are best left unset — they
default per network to the canonical USDC contract and the correct
facilitator, and mainnet validation rejects wrong values anyway.

## Staged activation

**Stage 0 — local tests (no network, no funds).**
`make test` (both stores) + `pytest tests_x402_interop` in its own venv.
Gate: everything green, `/x402/readiness` on a local run shows
`config_valid: true` for the intended mainnet env (set the env locally with
a THROWAWAY key to check validity; never production secrets on a laptop).

**Stage 1 — deploy dark.** Push (separate action; not part of this commit).
The release gate must certify the deployed SHA. x402 stays disabled
(`GUILD_X402_ENABLED` unset) — `/x402/readiness` shows `enabled: false`.

**Stage 2 — Base Sepolia live test.** Set `GUILD_X402_ENABLED=1`,
`GUILD_X402_PAY_TO`, `GUILD_X402_NETWORK=eip155:84532` in Render. From an
independent machine, run the official x402 client with a funded
Base-Sepolia test wallet against a paid read. Gate: 402 → pay → result →
`PAYMENT-RESPONSE`; `/billing/revenue` shows the settlement ONLY under
`testnet_settlement`; real revenue stays 0.

**Stage 2.5 — local preflight (read-only, secret-silent).** On the machine
holding the CDP key, run:

```
python live/scripts/x402_preflight.py --key-file <path to CDP key JSON>
```

It proves — without settling anything or printing any secret — that the
credentials produce valid request-bound JWTs, the AUTHENTICATED CDP
facilitator accepts them (`GET /supported`), exact/eip155:8453 is supported,
the recipient matches the pinned treasury, the confirmation RPC answers with
chainId 8453, and the service's own fail-closed validation passes.
Gate: `PREFLIGHT CLEAN`. A clean preflight is NOT a payment and NOT revenue.

**Stage 3 — one mainnet canary (~$0.005–0.01).** Set
`GUILD_X402_NETWORK=eip155:8453` plus `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`
in Render (service must boot — if it doesn't, the fail-closed validation
says why in the logs, without secrets). An **independent buyer** (not Guild-operated, e.g. Ross's own
separate machine with its own funded wallet, clearly labelled first-party
in any adoption metrics) pays one 10-credit read (= 0.01 USDC… note: one
`best_agent` read is $0.01; use `reputation`/`fraud_check` at 5 credits or
accept $0.01 — there is no $0.001-priced read today).
Gate: HTTP 200 with `PAYMENT-RESPONSE`; record in `/billing/revenue`
`real_settlement` with the tx hash.

**Stage 4 — confirm in the receiving wallet and on-chain.** Independently of
the service: look up the tx hash on basescan.org (or any Base node), verify
the USDC `Transfer` to `GUILD_X402_PAY_TO` for the exact amount, and verify
the wallet balance increased. Only after this is it honest to say a real
payment happened — and even then it is one canary, not adoption.

**Stage 5 — Bazaar discovery verification.** The bazaar extension rides in
every 402 challenge. Verify the CDP Bazaar (x402 discovery list) can see the
paid resources after real settlements exist; query the facilitator's
`/discovery/resources` for the Guild's resource URLs.

**Rollback.** Any anomaly (unexpected settlement records, unconfirmed
settlements piling up, wrong recipient, KYT declines): set
`GUILD_X402_ENABLED=0` in Render (rail off, service healthy, sandbox
credits unaffected), rotate the CDP API key in the CDP portal if
compromise is suspected, and reconcile `billing_log` x402 records against
basescan before re-enabling. Code rollback is a normal Render redeploy of
the previous SHA; the release gate re-certifies.

## Explicitly out of scope in this pass

* **Stripe** is NOT integrated. It remains a possible later SECONDARY
  option — card top-ups for sandbox credits already exist behind
  `STRIPE_SECRET_KEY` (unchanged), and a future off-ramp/secondary
  settlement rail could use it — but machine-native settlement is x402;
  adding Stripe here would add surface without serving the primary
  customer (autonomous agents).
* No funding of wallets, no key handling, no Render changes, no push.
