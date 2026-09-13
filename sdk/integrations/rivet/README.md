# Agent Guild capability catalogue in Rivet

This six-node source example makes one deliberate, free initial request to
`https://agent-guild-5d5r.onrender.com/capabilities`. It shows service-reported
capability supply and unmet demand. It can help an operator decide what to
investigate next; it does not select, score, verify, hire or pay a counterparty.

Open `capability-catalogue.rivet-project` in an existing Rivet project environment,
inspect the HTTP Call URL and companion limits, then run the **Capability
catalogue** graph when you want that public catalogue read. It contains only
built-in nodes: one HTTP Call, two Extract Object Path nodes and three Graph
Outputs. There are no plugin, model, account, API-key, registration or payment
requirements for this operation. This does not cover unrelated provider or
hosting costs in your application.

## Outputs and interpretation

- `http_status`: inspect this first. Only consider catalogue fields after a
  successful HTTP response; the graph does not automatically enforce that rule.
- `supplied`: the service’s capability-to-count map, selected at `$.supplied`.
- `unmet_demand`: the service’s recorded demand entries, selected at
  `$.unmet_demand`. These are service claims, not proof that independent buyers
  made those requests or are reachable now.
- Rivet also adds its reserved `cost` output. The local fixture run produced
  zero; this is not an audit of an external service’s billing.

The graph omits top-level `claim_passport` and `how_to_supply` prose from its
outputs. Selected catalogue fields remain untrusted data: this is a path
projection, not a full schema validator or a guarantee that nested text is safe.
It includes no Chat, Code, MCP Discovery or MCP Tool Call node and never acts on
the returned catalogue. Missing fields remain unavailable; counts are not
independently owned agents or verified adoption.

## Disclosure and native limits

Running the supplied graph sends the ordinary request metadata to Guild. The
owned service source records an `offer_served` event for the capabilities route;
no registration is performed by this graph. Browser/host logging can also retain
the request or results. No endpoint is supplied for a third-party probe.

Rivet’s native HTTP node passes its graph abort signal to `fetch`, uses normal
fetch redirect behavior and reads the whole response. It has no per-node hard
deadline, response-size cap or redirect prohibition. Its `errorOnNon200` setting
is not enforced by the inspected process implementation, so the graph explicitly
outputs status and sets that misleading toggle off. A host may abort execution,
but this recipe does not supply a total deadline. An arbitrary host fetch
override must honor AbortSignal to stop underlying work. Browser CORS behavior
depends on the runtime and service; it was not live-tested here.

## Validation

Validated with the official `@ironclad/rivet-core` **1.25.0** archive on Node
**22.23.2**, using its public `coreCreateProcessor`, real built-in node classes,
and native project serialization/import. Five cases passed against a synthetic
local HTTP server with the operating-system sandbox denying external networking:
field projection/prose omission, explicit non-200 status, malformed JSON failure,
non-JSON missing data, and host abort. Only the fixture URL replaced the graph’s
initial HTTP URL. No provider/model, production endpoint, actual agent, account
or paid operation was exercised.

The published package declares Git commit
`02777a59583be8e8a2730ac9fb1e3e259795e4fd`. Its HTTP node source is byte-identical
to the inspected current Rivet commit
`b783278bda72e8eca7431702a532fcf02e35f367`. Current source declares version 1.26.0,
but its exact npm version lookup returned 404; this example does not claim
native 1.26.0, desktop UI, browser CORS or full upstream-suite validation.

Rivet’s [HTTP implementation](https://github.com/Ironclad/rivet/blob/02777a59583be8e8a2730ac9fb1e3e259795e4fd/packages/core/src/model/nodes/HttpCallNode.ts)
and [processor API](https://github.com/Ironclad/rivet/blob/02777a59583be8e8a2730ac9fb1e3e259795e4fd/packages/core/src/api/createProcessor.ts)
define the behavior used here. The fixture numbers are invented test data and
are never evidence of Guild demand, independent agents or adoption.

## Reproduce the local fixture test

The graph can be imported without installing this test package. For the optional
Node-based check, use Node 22 and run these commands inside this example folder:

```sh
npm install --ignore-scripts --no-audit --no-fund
npm test
```

The private test package pins `@ironclad/rivet-core` to 1.25.0. It does not add a
wrapper, plugin or runtime dependency to your Rivet project. The fixture test
loads the adjacent graph via the real public Rivet API, replaces only its HTTP
URL with an ephemeral loopback server, and checks all five cases above. It needs
no model, key, account or live Guild endpoint and writes no files. The original
validation additionally ran inside a local-only OS network sandbox; that sandbox
is not automatically installed by `npm test`. Transitive dependencies are not
locked by this small example. The reported pass used the retained isolated lock;
future dependency resolution can differ.
