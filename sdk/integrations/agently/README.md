# Agently: optional public counterparty evidence

An owned Agent Guild example for an application that has selected an unfamiliar
public agent endpoint or received a public passport. It mounts two ordinary
Agently Actions: `guild_preflight` and `guild_verify_public_passport`. This is not
an Agently-endorsed plugin, automatic trust hook or permission to delegate.

From this repository's root, use a dedicated environment:

```bash
python3 -m venv .venv-agently
.venv-agently/bin/pip install -r sdk/integrations/agently/requirements-test.txt
.venv-agently/bin/python -m sdk.integrations.agently.example
.venv-agently/bin/python -m pytest sdk/integrations/agently/tests -q
```

The runnable example uses the real published Agently **4.1.4.8** and Agently-Stage
**0.3.8** with only Guild HTTP mapped to a local fixture. Its expected output
includes six endpoint checks and a completed but **unverified** passport. No model,
production service or cryptographic verifier is called by that example. Local
validation used Python **3.12**; the source uses Python 3.10-compatible syntax, but
Agently's separate upstream Python 3.10 quality lane was not executed here.

## Use with an application

Mounting is explicit and makes no HTTP request. With the default transport, an
invocation sends the selected public input to the real fixed Guild service:

```python
from agently import Agently
from sdk.integrations.agently import GuildActions

agent = Agently.create_agent()
agent.use_actions(GuildActions(timeout=15))
record = agent.action.execute_action(
    "guild_preflight",
    {"request": {"url": selected_public_endpoint}},
)
# For async applications, await agent.action.async_execute_action(...) instead.
```

The native Action's outer `status="success"` means its function ran. The business
result is in `record["data"]`: require a complete `status="observed"` result before
using its six statuses, failed/unknown/scored lists, exact target and verdict.
Missing or host-compacted data is unavailable evidence. Unknown stays unknown even
when `verdict="no_failed_checks"`. Neither reachability, card-signature presence nor
a payment-claim observation establishes successful work, signature validity,
settlement or independent ownership. Later endpoint execution is not bound to the
observation.

For a passport already supplied as a **public JSON object**:

```python
record = await agent.action.async_execute_action(
    "guild_verify_public_passport",
    {
        "request": {
            "credential": supplied_public_passport,
            "expected_issuer_did": independently_expected_issuer_did,
            "expected_subject_did": independently_expected_subject_did,
        }
    },
)
```

Choose expected DIDs independently; copying claims from the received credential
into those fields provides no independent binding. This action preserves the
complete supported public JSON object in the POST, checks issuer/subject and
signed validity/freshness, then validates the remote verifier's boolean flags.
`verified=true` requires both flags plus validity and freshness after the response.
A negative verifier result is still a completed Action. This is **online reported
verification**, not independent local signature verification. No identity-to-endpoint
binding or truth of reputation claims is inferred.

## Disclosure and boundaries

Only the selected public URL or complete supplied public credential is sent to
`https://agent-guild-5d5r.onrender.com`. Guild performs the endpoint probe. The
`POST /credentials/verify` operation records `passport_verified`, including
unsuccessful verification. Agently and the host may separately retain invocation
inputs, diagnostics and results, including rejected inputs. Do not provide secret
URLs, confidential claims, credentials or unrelated conversation content. Publicness
and the independence of expected DIDs are caller preconditions, not reliable data
classifiers implemented by this example.

Endpoint validation is lexical hostname screening only: it does not resolve DNS or
provide an SSRF guarantee. It rejects IP literals, local/reserved suffixes,
userinfo, fragments and malformed HTTP(S) URLs; Guild separately screens the probe.
`allowed_hosts=None` permits a newly selected public-looking hostname; an optional
exact lowercase host list narrows it, and an empty list permits no endpoint.

Passports require plain bounded JSON objects, supported Ed25519 `did:key` and
`DataIntegrityProof`/`eddsa-jcs-2022` shape, signed UTC dates and the separately
expected DIDs. The canonical maintained `sdk.agentguild_verify.public_key_from_did`
helper decodes DIDs; its network/fetch/vet methods are never called. Credential size
is limited to 32 KiB, depth 16 and safe JSON numbers. Custom objects, cycles,
nonfinite values, unsafe keys and non-JSON claims are rejected. The default
freshness limit is 86400 seconds (`max_passport_age_seconds`, range 1–604800).
An optional host `expected_issuer_did` adds a restriction; both DIDs remain required
per invocation.

Requests use verified TLS, no proxies, redirects or retries, and a 64 KiB
uncompressed response cap. The HTTP deadline (`timeout`, range 0.1–45 seconds)
bounds waiting for the whole response and sends cancellation. Event-loop suspension
can delay timers. A host-supplied `transport_factory` must cooperate with cancellation
to stop underlying work; arbitrary overrides can outlive the wait. Host Action
policy may impose an earlier timeout or deny execution. The byte cap does not
bound transport buffer allocation or all parsing/CPU cost.

The Actions use the native `register_actions` / `use_actions` package contract,
declare logging/probing side effects and disable replay-safety claims. Input is
nested inside `request` so native structured dispatch preserves it for the handler's
strict checks. Agently strips unknown **outer** keys on `structured_plan` dispatch
and records that diagnostic; direct dispatch retains them and the handler rejects
them. No claim is made to reject keys already discarded by the framework.
Structured-protocol fixture execution is not evidence of real model planning.

Results contain fixed local fields, times, response size/digest and limitations;
arbitrary remote prose, live reputation and instructions are omitted. There is no
registration, issuance, enrollment, billing, payment or paid fallback. Independent
buyers, live adoption and current service health are unmeasured.

## Checks

The focused suite uses real Agently registration, synchronous/asynchronous direct
and structured dispatch, and a local HTTP server. It covers exact disclosure,
complete statuses, malformed and bypass inputs, signed dates, negative verification,
redirects, body limits, cancellation and uncooperative transport waiting. The
historical owned response and synthetic passport fixtures are identified in
[fixtures/README.md](fixtures/README.md). Run `ruff check sdk/integrations/agently`
for the same selected rules as Agently's changed-Python quality script.
