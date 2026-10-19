# Agent Guild — usefulness evaluation protocol (pre-registered 14 October 2026)

Status: proposed for founder sign-off. Once signed, the hypotheses, capture fields and credit rules below are frozen for the programme (22 Oct → 21 Dec). Changes after the first participant decision are recorded as amendments, never silently.

## 1. The one decision

> An autonomous agent, inside a workflow its operator already runs, is about to make its **first call** to an endpoint it has not used before — a remote MCP server, an A2A agent, or an x402-paid HTTP API — to call it, delegate a task to it, or pay it. Should it proceed now, proceed with safeguards (no payment, reduced budget, sandboxed result handling), or decline / pick an alternative?

Scope limits, stated so they cannot drift:

- "Unfamiliar" is defined by the operator: no successful call to that host in their own history (or in the last N days, N chosen by them and recorded once).
- The decision is made by the operator's **existing policy**. AG runs in **shadow mode**: its result is an observation written next to the decision, never an input the policy must obey. Participation grants AG no authority over the agent.
- Out of scope: ranking or choosing between candidates, payments/settlement, reputation aggregation, standards, security monitoring. If the data says the check does not beat a direct handshake in these workflows, that is the finding — not a reason to widen scope.

## 2. What AG could add beyond the operator's current evidence — hypotheses, not claims

| Operator's existing evidence | What it answers | What AG's `/preflight` adds (hypothesis) | How we will know |
|---|---|---|---|
| Provider list / allowlist | "Did someone I trust list it?" | **H1 — protocol diagnostics before the call**: a real MCP `initialize` / A2A card fetch separates "HTTP 200" from "a working agent protocol". The 31-Jul ecosystem measurement (92.9 % claim healthy, 33.9 % complete a task) is the prior; it has never been tested against an operator's *own* check. | Paired comparison: AG result vs the operator's direct check on the same endpoint at the same time (§4). H1 is supported only if AG catches handshake failures the direct check passed, and those failures are confirmed by the outcome. |
| Local history | "Did it work for me before?" | Nothing on a first call — by definition empty. **H3 — shared failure history**: outcomes other participants reported for the same host. | Only testable once ≥ 2 operators have reported on overlapping hosts. Expected to be **unobservable** in this cohort; recorded as such rather than promised. |
| Framework diagnostics | "Did the call error?" — *after* the call | **H2 — verified identity binding**: card signature verification (did:key / same-origin did:web / JWKS), presence vs validity vs binding kept apart; a registered identity with attestation history bound to the endpoint (2.7.1). | Only exercisable where cards are signed (0.8 % on 31 Jul). Likely to produce 0 decisions in 30; if so, say so. A tampered-card or impostor-DID case, if one occurs, is the strongest possible single datapoint. |
| Own handshake probe (curl / SDK ping) | "Does it answer?" | **H4 — payment-claim check** for x402 buyers: the card says paid; does the resource actually 402? | Only for participants whose agents pay. |

**The simple substitute to beat.** A 10-line script: `POST <url>` with an MCP `initialize` body (or `GET /.well-known/agent-card.json`) with a 5-second timeout, run by the agent itself immediately before the call. It costs no third party, no URL disclosure, and roughly half the latency (first-party median 0.33 s probe vs ~0.8 s round trip). AG's plausible edge is narrow: one call covering MCP + A2A + card + signature + payment across transports; identity binding; and, later, cross-operator history. **If the paired comparison shows AG agreeing with the substitute in every evaluable case, the programme reports "no advantage over a direct check in these workflows" and recommends stopping, before any further implementation.**

## 3. Design

- **Real workflows, outside the project team.** Participants are operators or maintainers we do not run (independence recorded per §6 of the onboarding flow). First-party runs (our corpus, demos, scheduled tasks) stay in `first_party` and never count.
- **Shadow mode first.** The operator's policy decides. AG's result is logged beside the decision. A participant may choose, after ≥ 10 shadow decisions, to let AG's verdict influence the decision (still advisory) — the mode is recorded per decision.
- **Pre-reveal recording where practical.** Before AG's result is shown to the agent (or the human), the record holds the intended action and the evidence already in hand. Integrations that cannot do this (the check is one synchronous call) record the intended action as "policy default" and say so.
- **Prospective paired comparison.** For every decision the operator's own direct check (or the substitute script) and AG's check run on the same endpoint within the same minute. The 2 × 2 of (direct passes/fails) × (AG clean/warns) is resolved by the observed outcome.
- **Small controlled comparison — optional, one operator.** If one participant reaches ≥ 20 decisions, alternate per decision whether AG's result is revealed before the decision (arm A) or only logged (arm B). Compare decision changes and outcomes. This is a feasibility check, not a powered trial; it is reported as such.
- **Pre-registration.** Hypotheses H1–H4, the capture fields (§4) and the credit rules (§5) are fixed before the first invitation is sent. The rule table ships inside the service (`GET /preflight/outcomes → rules`), so participants read the same rules we apply.

## 4. Capture — one record per evaluable decision

| Field | Content | Source label |
|---|---|---|
| `endpoint` | host (+ path) of the endpoint; the operation (`call` / `delegate` / `pay`); timestamp | integration hook or participant |
| `baseline.evidence` | what the operator already had: allowlist? local history? framework diagnostics? direct check result (`baseline.direct_check`: success / failure / not_run) and what it found (`baseline.detail`, same vocabulary as AG's checks) | participant report |
| `baseline.intended_action` | what the policy would have done before seeing AG (`called` / `delegated` / `declined` / `skipped`, or `policy_default` where pre-reveal was impractical) | participant report |
| `ag.result` | `preflight_id`, `observed_at`, verdict, named failed checks, named unknown checks, `probe_latency_ms` | AG record (observed) |
| `decision.changed` | yes / no; `decision_basis` (`ag_evidence` / `own_check` / `both` / `other` / `unknown`) and the participant's one-line reason | participant report (a claim) |
| `action` | what the agent actually did (`called` / `delegated` / `declined` / `skipped`) | integration hook (observed) or participant report — stated which |
| `outcome` | `observed` (success / failure / unknown), `detail` (timeout, unreachable, http_5xx, protocol_error, payment_mismatch, card_unavailable, signature_invalid, identity_mismatch, wrong_result, other, http_4xx), time after `observed_at` | integration hook (observed) or participant report — stated which |
| `corroboration` | how the outcome was confirmed: hook log line, framework error, the operator's direct re-check, none | observed / participant / none |
| `classification` | result and kind from the published rule table (§5) | our inference, rule cited |
| `cost` | `probe_latency_ms` (AG), `overhead_ms` (participant-measured round trip), extra calls (always 1 GET + 1 POST per decision), integration burden (hours to set up, lines changed, recorded once per participant) | observed / participant |
| `study_overhead` | time spent on pre-reveal recording, manual clarification, outcome reporting — kept apart from normal-use overhead (the GET itself) | participant, recorded once per week |
| `mode` | shadow / advisory-influence; controlled arm A/B if applicable | integration config |

Every field carries its source label. Participant reports, independently corroborated observations and our inferences are never merged into one column.

## 5. Credit and mistake rules (as implemented in AGPO-1/1.3, 2.7.1)

Benefit is credited only when **all three** hold: AG's verdict was a warning naming check X; the reporter's own direct check (or the observed outcome) failed in a way that maps to the **same** check X; and the reporter states AG's evidence drove the decision (`ag_evidence` / `both`). Then: `avoided_broken_endpoint` (benefit).

Everything else is not benefit:

| Situation | Result | Kind |
|---|---|---|
| AG warned about X, direct check failed on X, decision based on own check | `correct_warning_independently_confirmed` | neutral (consistency) |
| AG warned about X, direct check failed on **unrelated** Y (e.g. unsigned-card warning, then `wrong_result`) | `failure_out_of_scope` | unresolved — AG did not predict this |
| AG warned, direct check failed, reporter gave no detail | `direct_check_unspecified` | unresolved |
| AG clean; direct check or call failed on a check AG scored **proven** | `missed_problem` | mistake |
| AG warned on reachability/handshake; the call (or direct check) **succeeded** | `unnecessary_refusal` | mistake |
| AG warned on identity/payment/evidence; the call succeeded | `warning_not_contradicted` | neutral — a working call does not test those |
| AG clean; call succeeded | `consistent_outcome` | neutral — agreement with a direct handshake is consistency, not benefit |
| Declined with no direct check | `counterfactual_unobserved` | unknown |
| Report > 24 h after the evidence | `stale_join` | unresolved |
| No report | counted in `runs_without_outcome` | unknown — never refusal, never success |

Mistake measures reported alongside benefits, every review: `unnecessary_refusal` and `missed_problem` counts; **misleading confidence** = a check AG scored `proven` that the outcome contradicted within the freshness window (subset of missed problems, listed by check name); **stale evidence** = decisions acted on > 10 minutes after `observed_at` (recorded, excluded from the benefit numerator); latency and overhead medians per participant.

Only decisions from participants whose operator independence is **attested** (onboarding §2) can move the headline. A registered key on both legs joins records under one identity; it is not evidence of independence (2.7.1 change).

## 6. Worked examples

**Benefit.** A BeeAI `RequirementAgent` is about to delegate a summarisation task to an A2A agent found in a directory. Baseline: `curl -I` returns 200 (direct_check = success at HTTP level). Intended action: `delegated`. AG: `do_not_delegate` — `endpoint_reachable: proven`, `protocol_handshake: failed` ("HTTP 200 but no A2A card and `message/send` not answered"). The agent's policy (shadow mode) still delegates; the call fails with a JSON-RPC parse error. Report: `delegated`, `observed: failure`, `detail: protocol_error`, `decision_basis: own_check`. Classification: `evidence_correct_not_followed` (neutral) — AG was right but the decision was not changed. In advisory mode the same case, with the agent declining and the direct check re-run as `POST message/send → protocol_error`, is `declined`, `baseline.direct_check: failure`, `baseline.detail: protocol_error`, `decision_basis: ag_evidence` → **`avoided_broken_endpoint` (benefit)**, corroborated by the reporter's direct check (a claim) and the hook's error line (observed).

**Mistake — unnecessary refusal.** A remote MCP server sits behind a WAF that blocks AG's prober. AG: `do_not_delegate` (`endpoint_reachable: failed`). The operator's agent calls it (shadow mode) and the tool call succeeds. Report: `called`, `observed: success`. Classification: **`unnecessary_refusal` (mistake)** — AG reported a reachability failure that a working call contradicts. Cost recorded: had the verdict been followed, a working tool would have been refused.

**Mistake — missed problem.** AG: `no_failed_checks` at 10:00:03. The agent calls at 10:00:05 and receives HTTP 503. Report: `called`, `observed: failure`, `detail: http_5xx`. Classification: **`missed_problem` (mistake)** against `endpoint_reachable: proven`. Freshness note: the outcome is 2 s after the evidence, so this is not stale; a 503 ten minutes later would be excluded from the numerator and listed under stale evidence.

**Unresolved.** AG: `delegate_with_caution` (`agent_card_signed: failed`). The agent's policy declines because the operator forbids unsigned counterparties anyway. No direct check. Report: `declined`, `baseline.direct_check: not_run`. Classification: `counterfactual_unobserved` (unknown). If the operator later runs a direct check and finds a wrong result on a test task: `baseline.detail: wrong_result` → `failure_out_of_scope` — an unsigned-card warning does not predict a wrong result, so no credit either way.

**No difference.** AG clean; the operator's substitute script also passes; the call succeeds → `consistent_outcome` (neutral). Thirty of these in a row is the "no advantage over a direct check" finding.

## 7. Analysis plan (fixed)

Per participant and pooled: decisions; benefit / mistake / neutral / unresolved / unknown; paired-comparison 2 × 2; median `probe_latency_ms`, median `overhead_ms`; study overhead vs normal-use overhead; stale-evidence count; retention (AG still enabled 14 days after the participant's last study decision, unprompted). Nothing is summed across first-party, heuristic and attested-independent buckets. The report format is the six questions of `docs/DIRECTION_2026-09-22.md`.
