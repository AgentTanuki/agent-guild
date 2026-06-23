# Agent Guild — Substrate Hardening Report

*CTO report. Scope: five hardening priorities on the live service, plus the test and verification evidence. All changes are committed and covered by the test suite.*

## Summary

Five gaps were closed: the two self-evaluation readouts now share one source of truth; our own traffic is reliably tagged so it can't read as organic; the referral reward is gated behind real use and structurally protected from loops; unknown and colluding agents can no longer bootstrap reputation; and this report records what changed and what risk remains. The suite went from 35 to **42 tests, all passing**, including new engine-level and API-level tests for each change.

## What changed

**1 — One source of truth for "external."** Previously the server's `/self-eval` and the monitoring tick computed "external usage" two different ways, so their headline verdicts could disagree. The health vector and verdict now live only in the server (`store.compute_health`), `GET /self-eval` returns a fresh read-only computation on every call, and the monitoring tick (`self_eval_tick.py`) consumes that endpoint as primary — it no longer computes its own verdict. A clearly-labelled `[FALLBACK]` path from public endpoints remains only for when `/self-eval` is unreachable, so a daily tick is never silently lost.

**2 — Strict seed-traffic tagging.** Our own seed/test tooling now tags every request with the `X-Guild-Source` header (added to both the `GuildClient` SDK and the zero-dep lite client; `seed_supply.py`, `onboard_demo.py` set it). Pre-trusted **seed agents are now always first-party** — they are governed supply, not organic demand. A new `GUILD_FIRST_PARTY_TOKEN` enables *strict* mode: when set, only a header matching the token counts as first-party. Even without the token (default), our tools send a marker so our traffic is excluded from organic metrics; the token is belt-and-suspenders against spoofing (for which there is, in any case, no attacker incentive — tagging yourself first-party only removes you from the counts).

**3 — Referral anti-gaming.** Three independent layers, each covering the others' blind spot. (a) *Activation threshold*: a referrer is paid only after the referred agent crosses a real-use bar — `≥2` accepted task receipts **or** `≥3` paid reads — so a single throwaway event can't trigger a payout. (b) *Structural*: referral edges always point from a newer agent to an existing one, so the referral graph is a DAG by construction — self-referrals and reciprocal/closed loops are impossible, not merely checked. (c) *Economic*: first-party referrals never pay, and a per-referrer cap bounds farm payouts. Reward is still paid at most once per referral.

**4 — Absolute reputation floor + seed path.** The known v0.2 weakness was that "trusted reviewer" was defined purely *relative* to the network maximum, so in a sparse graph it could be cleared cheaply. A reviewer's vote now counts as trusted only if it clears the relative bar **and** an absolute eigen floor **and** is reachable from a pre-trusted seed along the trust graph. On top of that, any agent with no seed-anchored trusted support is hard-capped at an `unknown_trust_ceiling` (≈5/100) — so unknown or island-clique agents stay near zero instead of resting at the prior. Reputation must now be *earned* from seed-traceable evidence.

**5 — This report.**

## Tests

Full suite: **42 passed** (`live/guild/`, `pytest -q`). New coverage:

- `test_hardening.py` (engine): unknown agent can't bootstrap trust; an island mutual-praise ring is denied trust; a seed-anchored agent earns trust above the ceiling and strictly more than an unknown; and toggling `require_seed_path` is shown to be the lever doing the work (not some incidental factor).
- `test_growth.py` (API): one receipt is below threshold and pays nothing; reward fires once when the threshold is met and never doubles; seed traffic and `X-Guild-Source` traffic are excluded from external counts; a first-party referral pays nothing.
- All 35 pre-existing tests still pass unchanged — the seed-anchored scoring tests were already robust to the floor.

Verification beyond unit tests: the aligned tick was run against a local instance of the new code and confirmed to report `source: server` with the server-computed verdict; the live redeploy will be confirmed after push.

## Risks that remain (honest boundaries)

- **Strict first-party mode depends on configuration.** Until `GUILD_FIRST_PARTY_TOKEN` is set on the server *and* shared with our tooling, tagging runs in permissive mode. That is safe against the realistic threat (our own traffic leaking in as organic, now fixed) but not against a deliberate spoofer — who has no incentive anyway. Setting the token is a one-line Render env change, documented in `render.yaml`.
- **The activation threshold is a heuristic, not a proof of independence.** A determined operator who runs *many* genuinely-active shell agents past the threshold could still farm referral rewards up to the per-referrer cap. The real defence here is the same as for reputation: rewards are paid in credits, and once value-capture (real fees) lands, farming costs more than it returns. Tracked as the next economic hardening.
- **Seed governance is still the root of trust.** The seed set is the anchor for the entire reputation computation and for the seed-path requirement. Whoever controls seed status controls trust. Production needs a governed seed set (multi-party / KYC'd operators), which is a policy decision, not a code change.
- **Absolute thresholds are tuned, not derived.** `abs_eigen_floor`, `unknown_trust_ceiling`, and the activation counts are sensible defaults exposed as parameters/env, not values proven optimal against real adversarial data. They should be revisited once the live graph is large enough to measure against.

## Next

Proceeding to value-attribution and fee-capture (Outcome 2): port the marketplace settlement path into the live service, attribute captured value to measured lift, and record an auditable fee ledger — with live Stripe payout kept behind the legal-entity gate.
