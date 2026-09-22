# Agent Guild — usefulness charter (22 September → 21 December 2026)

Governing objective (Ross, 2026-09-22): **demonstrate that independently operated agents make better decisions using AG's evidence, with measured benefits and measured mistakes.** The question every workstream answers: *how did an agent benefit from AG?*

This charter names one use case, states the evidence baseline honestly, describes the smallest implementation (already built on a ship branch), the route to independent use, the measures, the costs, and the review points. It is a bounded 90-day programme, not a roadmap.

## 1. The evidence baseline, in four separated categories

Read live on 2026-09-22 from `/instrumentation`, `/instrumentation/objectives`, `/funnel/paid`, `/funnel/paid/actors`, `/funnel/passports`, `/self-eval`, `/billing/revenue`, plus the repo and campaign records. Nothing below is extrapolated.

**(a) Capability demonstrated in tests.** Substantial. 2,253 tests pass across the four suite partitions on the current branch (JSON backend; the sqlite partition runs in CI). Demonstrated in tests and by first-party transport-level verification: free `/preflight` with live probes and unknowns excluded from the verdict; signed passports with offline verification; AGFC-1 natural-language first contact (no caller-text relay, ~640-byte capsules); AGIR-1 signed incident receipts; AGCS-1 coordination-safety policy; x402 payment binding and chain-verified settlement; durable-history metrics; an autonomous ship loop with certified rollback. This category answers "can AG do it", not "did anyone benefit".

**(b) Independently observed use of AG.** Thin, and mostly reads. Genuine-external (recent window): 5 unique agents made a first query, 1 repeated. Paid-funnel qualified actors ever: 60, of which 18 are one monitoring crawler (`agentstatus-probe`) under rotating version strings, so ≤ 43 distinct; 4 have ever requested a price (two are reliability probes exercising our 402), 0 completed. Natural-language front door: 152 objective requests, 120 mapped. Passports: 7 qualified exposures, 0 registrations, 0 external self-claims; 43 third-party passport verifications, class mostly unknown. Settlements: 5 mainnet payments from 4 wallets ($0.045), ownership unknown; payment does not prove value. Framework placements: a Pi extension (source-installable, unpublished on npm), a Strands guide, 52 PRs opened as AgentTanuki against third-party repos (count as of 8 Sep; merges and actual use unverified). None of these is usefulness.

**(c) Agent actions following AG evidence.** Essentially unobserved, not zero. `delegations_following_recommendation` = 0 in every class. `full_detail_followthrough.followed` = 1 of 49 mapped requests, provenance unknown (may be our own probe). The Pi gate can block a call after a `do_not_delegate` verdict, but no such event has ever been reported back. Two July observations (an agent registering, returning and asking how to prove; another volunteering its endpoint) are actions on AG's surfaces, not on AG's evidence.

**(d) Verified benefits or mistakes attributable to that evidence.** None, in either direction. No record exists of an agent avoiding a broken endpoint, rejecting a tampered credential or choosing a counterparty because of AG, and equally no record of an unnecessary refusal or a missed problem. The trust index's `production_n_recommended` is 0; its 0.46 lift is bootstrap-only and is not validation. Two known preflight mistakes were found by our own agents on 8 Sep (false `do_not_delegate` on working MCP endpoints; caution on AG's own card) and fixed on 8 Sep (#185, #199, #202) — first-party defect discovery, not independent evidence.

**What we know:** AG is reachable, well-formed, honest and cheap to call; a handful of independent agents read it; the category of need is real (the July HF incident: ~700 agents rebuilt most of AG's feature list ad hoc with no root of trust). **What we do not know:** whether any decision anywhere was better or worse because of AG. **The observation that resolves it:** a joined record of verdict → action → outcome from an actor we do not operate, with the counterfactual checked. That record type did not exist until today.

## 2. The selected use case: the free endpoint preflight decision

*"I am about to call, delegate to, or pay this agent/MCP endpoint. Should I, right now?"*

Why this one, against the five criteria:

- **A concrete decision another agent already faces.** Every delegating or paying agent faces it on every unfamiliar endpoint; the 31 July ecosystem measurement found 92.9% of registry agents claim healthy and 33.9% complete a task, so the decision has real variance.
- **Evidence AG can reliably supply today.** `/preflight` is live, free, keyless, sub-second, tri-transport, and reports unknowns as unknowns. Its known false-negative class was fixed on 8 Sep.
- **A feasible route into an independently operated workflow.** The Pi extension exists (`guild_preflight` tool, optional `tool_call` gate); Mastra's `verifyAgentCard` and Strands are documented hooks. The check is one configuration line in most frameworks.
- **An outcome we can observe.** The endpoint is then called or not, and the call succeeds or fails — a binary the caller sees within seconds, and can report.
- **A simple alternative to compare against.** A direct HTTP/handshake check by the agent itself, or no check at all. The reporter records this as `baseline.direct_check`.

Rejected for now: passports (few agents currently face a credential-verification decision; outcome is diffuse); payment decisions (population of x402-paying agents is tiny); `/check` capability ranking (paid, and the trust index has never been consumed in production).

## 3. The smallest necessary implementation — built, on `ship/preflight-outcomes-agpo1`

One complete path, nothing else:

1. Every `/preflight` and MCP `guild_preflight` verdict is now stamped with an opaque `preflight_id`, `observed_at`, `probe_latency_ms`, a freshness note and a declarative `outcome_report` action (advisory, no local authority).
2. `POST /preflight/outcome` (free, no key, 120/hr/IP) and MCP `guild_preflight_outcome` accept one small record: `action` (called / delegated / declined / skipped), `observed` (success / failure / unknown), coarse `detail`, `baseline.direct_check` (success / failure / not_run), `overhead_ms`, `reporter_kind` (integration = observed action; agent = participant report). No payload, no identity beyond the caller's own.
3. `GET /preflight/outcomes` publishes the ledger by actor class — independent / first-party / propagation client / tooling-or-crawler — with the classification rule table beside the numbers. Benefits (`avoided_broken_endpoint`) and mistakes (`unnecessary_refusal`, `missed_problem`) carry equal weight; a skip without a direct check is `counterfactual_unobserved`; runs without outcomes are UNKNOWN; independence requires both the preflight and the report to pass the existing `is_genuine_external` gate. The headline is literally "No independently demonstrated benefit yet." until an independent benefit record exists.
4. The Pi extension gains `guild_preflight_outcome` (agent self-report) and an opt-in `AGENT_GUILD_REPORT_OUTCOMES=1` under which a gate *block* reports the single fact it observed (declined, counterfactual not checked). Default off.

18 server tests and 2 extension tests pin the honesty properties. Contract artifacts regenerated. Not yet deployed: the branches need publishing through GitHub Desktop (Ross's authority), after which the ship loop merges, deploys and gates them without a human.

Explicitly **not** built: any new product, any reputation aggregation, A2A parity for outcome reports (A2A callers can use HTTP), retroactive inference of outcomes from silence, and further telemetry until this path has taught us something.

## 4. Capturing benefits and mistakes

Per evaluable case the record holds: the decision (preflight of URL X at time T), the evidence without AG (`baseline.direct_check`), what AG supplied (verdict, failed checks, unknowns, latency — joined by id), what the agent did (`action`), the observed outcome (`observed`, `detail`), the benefit / mistake / no-difference classification (ours, by the published rule), and how it was checked (`checked_by` in the rule table; observed action vs participant report vs our inference labelled separately).

Mistake measures: `unnecessary_refusal`, `missed_problem`, `probe_latency_ms` (median per class), `overhead_ms` reported by callers, and stale-evidence cases (an outcome reported long after `observed_at` — to be added to the summary only if it occurs). Following AG's advice is never rewarded by itself: `no_difference` and `counterfactual_unobserved` are the honest labels for "followed, nothing learned".

Replayable cases: a first-party corpus of known-good and known-broken endpoints (the recruiter session is invited to run adversarial probes of `/preflight`; every false positive and false negative is recorded as a first-party mistake). Synthetic, first-party and independent records are never summed.

## 5. Route to independent use — voluntary, machine-native, no outreach

- Publish `pi-agent-guild` to npm under AgentTanuki (needs Ross: npm authority). Pi's package directory lists `pi-package` keywords automatically; discovery is by the operator's own search, not by us.
- Keep the Strands and Mastra guides accurate to the stamped verdict; a Mastra `verifyAgentCard` adapter only if a maintainer accepts one — an integration is "available", never "adopted", until an operator we do not run actually uses it.
- The stamped `outcome_report` action travels with every verdict on every transport, so any agent that already calls `/preflight` learns how to report without being told.
- No campaigns, posts, DMs, incentives, listings-for-listings'-sake, or manufactured transactions. Our own agents may exercise the path and expose defects; their records are first-party by construction.

## 6. Measures

Primary (the only ones that can satisfy the objective):
- Independent benefit records (`avoided_broken_endpoint` with a reporter-verified baseline).
- Independent mistake records (`unnecessary_refusal`, `missed_problem`) — reported with the same prominence.
- Independent `no_difference` and `counterfactual_unobserved` counts (honest denominators).

Supporting (adoption; may inform, never lead):
- Independent runs stamped and the share with any outcome report.
- Distinct independent reporters; distinct operators where an identity allows it.
- Framework placements in actual use (an operator we do not run, observed).

## 7. Resource limits and operating costs

Existing limits stand: `/preflight` free and keyless, outcome reports 120/hr/IP, 256 KiB body cap, no new quotas. Operating costs: Render `starter` plan for the service (persistent disk) plus a `free` gate instance; exact monthly amounts and the AI-compute cost of the scheduled and Codex tasks are **unmeasured** — Ross to supply the Render invoice and the Codex/Claude spend so the 30-day review can weigh them. No new spending commitments are made by this charter.

## 8. Review points and criteria

- **Day 0 (22 Sep):** path built; branches awaiting publish; baseline recorded.
- **Day 7 (29 Sep):** release live and gated; `/preflight/outcomes` serving; first-party adversarial corpus run once; npm publish requested. *Continue* if the path works end to end first-party. *Change* if the record schema proved wrong in first-party use.
- **Day 30 (22 Oct):** *Continue* if ≥ 1 independent outcome record of any classification exists (the path is being used). If none: distinguish **no distribution** (no independent runs stamped at all), **failed activation** (independent runs but zero reports — the report step is too costly or invisible), and **missing observation** (reports but all `counterfactual_unobserved`). Each is a different finding with a different smallest next action; none is "AG is useless".
- **Day 60 (21 Nov):** *Continue* if ≥ 1 independent benefit **or** ≥ 1 independent mistake has been recorded (either proves the instrument measures reality). *Change* the use case if ≥ 10 independent records exist and all are `no_difference` — that is a demonstrated lack of benefit for this decision, a legitimate and valuable negative result.
- **Day 90 (21 Dec):** report in the section-9 format. *Stop* this use case if independent records exist and show no benefit; *stop* the programme's assumption of distribution if no independent run was ever stamped despite the npm listing and guides, and revisit distribution rather than the product.

"We need more telemetry" is not an acceptable outcome at any review: the one path is instrumented; what it teaches decides the next step.

## 9. How progress is reported (every review, every scheduled task)

1. Which independent agent benefited?
2. What decision improved?
3. What evidence supports that conclusion?
4. What mistakes or costs did AG introduce?
5. What remains unknown?
6. What is the next smallest action that could establish usefulness?

If no benefit has been demonstrated: **"No independently demonstrated benefit yet."** Activity counts and code output are never substituted.

### Status at charter time

1. No independently demonstrated benefit yet.
2. No decision has been shown to improve.
3. No evidence of benefit; the record type that could hold it exists as of today on an unpublished branch.
4. Known first-party mistakes: two false-negative classes in `/preflight` (fixed 8 Sep); probe latency unmeasured until the stamp ships. Cost to the ecosystem so far: none measured.
5. Unknown: whether any independent agent acts on a verdict; whether the report step will be taken; operating cost per month.
6. Next smallest action: publish the two ship branches so the path is live, then let the 7-day review read `/preflight/outcomes`.
