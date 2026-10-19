# Agent Guild preflight evaluation — participant onboarding and reporting (v2, 14 October 2026)

Supersedes `artifacts/assessment-2026-09-22/evaluation/PARTICIPANT-BRIEF.md` on one point: a registered key joins your records under one identity; it does **not** by itself make you "independent". Independence is recorded separately (§2) so that nothing about it rests on a credential.

## 1. What you are agreeing to (and not)

- A free, bounded evaluation of one check: `GET https://agent-guild-5d5r.onrender.com/preflight?url=<endpoint>` run immediately before your agent's first call to an endpoint it has not used before, plus one small outcome report afterwards.
- Your policy keeps control. AG's result is an observation. Nothing installs, pays, signs, or changes configuration; AG has no authority over your agent (AGCS-1).
- Mistakes and neutral results are as useful to us as benefits, and are published with the same weight.
- You can stop at any time; your key can be revoked by you or by us; nothing further is sent to you unless you ask.

## 2. Independence — how it is supported without personal information

- You qualify if the operator of the agent is not a member of the Agent Guild project (not Ross Burley / AgentTanuki, not the project's Codex or Claude sessions, not an agent the project runs).
- We record that with **one public reference you choose** — a GitHub profile, a repository, a forum thread or a project site that shows the operator exists outside the project. No name, email or organisation is required beyond what that public reference already shows.
- The attestation is written by the project operator (`POST /admin/evaluation/participants`, basis `operator_outside_project_team`, your public reference) and is revocable. It is what lets your decisions reach the published headline. Your key is used only to **join** your preflight to your outcome report; it never establishes independence on its own.
- First-party testing, demonstrations and crawlers are classed separately by construction (`first_party`, `tooling_or_crawler`); your records never mix with them.

## 3. Setup — the smallest version that fits a real workflow

1. **Register once** (free, ~1 s): `POST /agents/register` `{"name": "<your agent>", "capabilities": ["<anything>"]}` → keep the returned `api_key`. (MCP: `guild_register`.)
2. **Tell us your public reference** (one message); we record the attestation and confirm.
3. **Add the check before unfamiliar calls** — pick the route that matches your stack:
   - HTTP: `GET /preflight?url=<endpoint>` with header `X-API-Key: <key>` (one line in your pre-call hook / middleware).
   - MCP: tool `guild_preflight(url, api_key)` on `https://agent-guild-5d5r.onrender.com/mcp` (the key may also travel as the `X-API-Key` header of the MCP request).
   - Pi coding agent: `pi-agent-guild` extension (source install, pinned revision) with `AGENT_GUILD_API_KEY=<key>`; optional `AGENT_GUILD_REPORT_OUTCOMES=1` reports called/declined + success/failure automatically.
   - BeeAI: the merged `examples/` preflight example (`python/examples/`) wraps `A2AAgent(url)`.
   - A direct check beside it: whatever you already do (curl, SDK ping), or the 10-line substitute script we supply — run it on the same endpoint so the comparison is paired.
4. **Report once per decision** — `POST /preflight/outcome` with the same key (MCP: `guild_preflight_outcome(..., api_key)`):

   ```json
   {"preflight_id": "<from the verdict>",
    "action": "called|delegated|declined|skipped",
    "observed": "success|failure|unknown",
    "detail": "<what failed, if the call failed>",
    "baseline": {"direct_check": "success|failure|not_run",
                 "detail": "<what YOUR check found, if it failed>"},
    "decision_basis": "ag_evidence|own_check|both|other|unknown",
    "overhead_ms": 812,
    "reporter_kind": "integration|agent"}
   ```
   `detail` vocabulary: `timeout`, `unreachable`, `http_5xx`, `protocol_error`, `payment_mismatch`, `card_unavailable`, `signature_invalid`, `identity_mismatch`, `wrong_result`, `other`, `http_4xx`. AG can only be credited (or faulted) for a failure that names the check it scored; "it failed" without a detail is recorded as unresolved.
5. **Automatic where reliable, manual where not.** The Pi extension and a BeeAI/HTTP hook can report `called` + success/failure automatically from the tool result. `declined` decisions and `decision_basis` need a human or the agent's own reasoning; we ask for those in a weekly clarification list (§5).

Participant effort, measured and published: one extra round trip per unfamiliar endpoint (first-party median 0.33 s probe, ~0.8 s end to end), one POST, and roughly 10 minutes a week of clarification.

## 4. What is collected, kept and published

- **Collected per preflight:** the endpoint URL you asked about (needed to run the check; stored in AG's event log), the verdict and named checks, `preflight_id`, `observed_at`, probe latency, your User-Agent, your agent id (from the key). **Not collected:** task content, tool arguments, results, credentials, operator identity beyond the public reference you chose. Private-network and credential-bearing URLs are refused by policy before any probe.
- **Collected per outcome report:** the small record above. Nothing else.
- **Kept:** durably, in the service's event store (both backends), as the evidence behind the published aggregates. Retention is indefinite for the aggregates; you can ask for your records to be excluded from publication at any time.
- **Published:** aggregates only, at `GET /preflight/outcomes` (by ownership bucket, with the rule table beside the numbers; target hosts appear only as a top-N host count). An **identifiable case study** (template in `artifacts/assessment-2026-09-22/evaluation/CASE-STUDY-TEMPLATE.md`) is published only with your written permission, with your redactions.
- **Limits of the check, stated plainly:** it is a live probe at one moment and does not predict the endpoint minutes later; it never executes a task or a payment; it cannot see what the endpoint does with your data; a verified card signature proves integrity and key control, not safety; "no independent evidence" means absence of evidence, not risk; the prober can be blocked by WAFs and will then report an unreachability you may not see (that is recorded as AG's mistake).

## 5. Weekly clarification (10 minutes, by message)

Each week we send the list of your `preflight_id`s whose classification is unresolved (`direct_check_unspecified`, `counterfactual_unobserved`, `failure_out_of_scope`, no report) and ask two things per id: what did your own check find (vocabulary above), and what drove the decision. You answer the ones you can; the rest stay unknown. We never infer an answer from silence.

## 6. Leaving

Say so, or just stop calling. We revoke the attestation on request, exclude your records from any case study, and send nothing further.
