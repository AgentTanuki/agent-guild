"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "2.6.3"   # PATCH live census serving (2026-08-31): builds the
                        # complete durable signed discovery census before
                        # readiness, serves its immutable declared snapshot
                        # without a request-time full-history scan, and
                        # refreshes it on each scout cycle. History of 2.6.2:
                        # PATCH release recovery (2026-08-31): retries a
                        # boot-time persisted scout lease without consuming
                        # the six-hour interval, and waits boundedly for
                        # GitHub branch-policy convergence before merging a
                        # fully certified automatic revert. History of 2.6.1:
                        # PATCH no-relay transport parity (2026-08-31):
                        # maps natural objectives before HTTP/MCP demand and
                        # payment handling; makes /check and /incidents 422s
                        # machine-useful without rejected-input echo; counts
                        # objective metrics from durable event history; and
                        # closes non-obvious objective→probe undercounting.
                        # History of 2.6.0:
                        # MINOR machine-first communications (2026-08-31):
                        # adds the confidential AGIR-1 incident drop box with
                        # signed hash-only receipts; deterministic hash-bound
                        # objective matching and sub-1KiB first-contact
                        # capsules; explicit side-effect/authority metadata;
                        # objective→action metrics; and a <=5KiB agent card
                        # with linked capability/paid catalogues. Existing
                        # explicit check, invocation and payment paths remain.
                        # History of 2.5.42:
                        # PATCH revenue-semantics correction (2026-08-31):
                        # a confirmed mainnet x402 settlement IS revenue
                        # unless the payer is positively identified as
                        # Guild-controlled first-party/canary; adds
                        # gross/known-first-party/external settled revenue,
                        # payment + distinct-wallet counts and
                        # attribution_coverage to /commercial,
                        # /billing/revenue and /self-eval, derived at read
                        # time from the settlement ledger + configured
                        # first-party wallet registry. Attribution stays
                        # measured, never a revenue prerequisite; experiment
                        # promotion still requires the mechanical
                        # operation/price/arm link.
                        # History of 2.5.41:
                        # PATCH coordination and outcome hardening (2026-08-27):
                        # makes peer authority advisory/attenuated, separates
                        # authenticated worker delivery/stops from attributable
                        # requester grades, binds signed stop reasons and receipt
                        # hashes, and serves non-overclaiming evidence relations.
                        # History of 2.5.40:
                        # PATCH consolidated machine feedback (2026-08-27):
                        # adds concrete invocation guidance and truthful effect
                        # disclosure, standard machine discovery surfaces,
                        # exact crawler attribution, and transparent
                        # attestation task relations and score inclusion.
                        # History of 2.5.39:
                        # PATCH exact registry-scanner census correction
                        # (2026-08-16): demotes three privacy-safe actor aliases
                        # produced by the skills.sh reindex/audit burst. The
                        # correction is actor-scoped, so unrelated autonomous
                        # agents using the same HTTP libraries still qualify.
                        # History of 2.5.38:
                        # PATCH least-privilege skill packaging (2026-08-16):
                        # keeps the complete hosted/OpenClaw policy internal
                        # to Agent Skills discovery while publishing a tiny,
                        # read-only registry bundle with no executable remote
                        # dependency or payment authority. This prevents
                        # skills.sh clients and scanners from treating the
                        # whole application repository as an installed skill.
                        # History of 2.5.37:
                        # PATCH census first-party correction (2026-08-16):
                        # exact-pins five publicly evidenced Guild-operated
                        # legacy agents, upgrades current first-party account
                        # state at read time without rewriting event history,
                        # and prevents unproved members from bypassing T1 via
                        # a framework-shaped User-Agent. This intentionally
                        # lowers any contaminated reach total; no real external
                        # actor is inferred from a name, domain, or prefix.
                        # History of 2.5.36:
                        # PATCH integrations.sh owner discovery (2026-08-16):
                        # publishes the v3 /.well-known/integrations.json
                        # owner declaration consumed by integrations.sh and
                        # downstream agent configuration tools. It names only
                        # the live MCP and HTTP surfaces, keeps mixed HTTP auth
                        # explicitly unknown, and records a fetch only as a
                        # non-commercial discovery observation. Payments,
                        # pricing, authorization and settlement are unchanged.
                        # History of 2.5.35:
                        # publishes /.well-known/mcp.json as a short,
                        # cacheable alias of the live-generated MCP server
                        # card so agent-operated crawlers can verify and
                        # index the hosted endpoint. The fetch is measured by
                        # the same privacy-safe discovery census; commercial
                        # demand, pricing, authorization and settlement are
                        # unchanged.
                        # History of 2.5.34:
                        # discovery measurement (2026-08-15):
                        # records one non-commercial observation when a
                        # machine-readable discovery resource is successfully
                        # fetched, then publishes a durable, deduplicated,
                        # Guild-signed /discovery/reach proof toward 25,000
                        # attributable autonomous-agent callers. First-party,
                        # tests, generic tools, registries, anonymous actors
                        # and repeats are structurally excluded; historical
                        # paid-catalog rows seed only distinct actors, never
                        # raw fetch totals. Payments, pricing, authorization,
                        # settlement and commercial experiments are unchanged.
                        # History of 2.5.33:
                        # publishes a focused, independently discoverable MCP
                        # server for pre-signing x402 payment safety and an
                        # ARD-conformant AI catalogue. The existing general
                        # MCP server, product, price, payment, authorization,
                        # treasury and settlement paths are unchanged.
                        # History of 2.5.32:
                        # makes qualified paid-funnel journeys decidable from
                        # durable history without exposing IPs, credentials,
                        # bodies or payment payloads. Free catalogue entries,
                        # executable price challenges and verified mainnet
                        # completions are separated per pseudonymous actor so
                        # repeated catalogue rows cannot masquerade as buyer
                        # attempts. Pricing, attribution, experiments,
                        # payments and settlement are unchanged.
                        # History of 2.5.31:
                        # exposes the existing exact-payment AGPD-1 policy as
                        # a named MCP tool at the wallet's last reversible
                        # moment before x402 signing. Stock MCP clients now
                        # receive the same request-bound challenge, official
                        # payment-meta retry, schema-visible fallback and
                        # signed decision as the HTTP product. Price, policy,
                        # authorization, treasury and settlement are unchanged.
                        # History of 2.5.30:
                        # makes paid funnels and autonomous price experiments
                        # read one ordered snapshot from durable SQLite event
                        # history instead of the bounded serving cache. Event
                        # memory now hydrates as an exact retained tail with a
                        # disclosed offset; diagnostics distinguish retention
                        # from a genuinely stale writer, while JSON-mode
                        # partial history fails closed and cannot reprice.
                        # Revenue eligibility, attribution, active treatment
                        # state, prices and settlement are unchanged.
                        # History of 2.5.29:
                        # repairs the canonical x402 handoff for body-bound
                        # products: machines now learn the exact authoritative
                        # Bazaar request template through a non-attributed,
                        # non-executable discovery challenge instead of being
                        # sent to metrics or generic route schemas. A materialized
                        # evidence-bundle body now receives its own executable,
                        # request-bound quote instead of the registry sentinel;
                        # its opaque request digest binds the optional audience
                        # as well as URL and TTL. Pricing, authorization,
                        # settlement and telemetry are unchanged.
                        # History of 2.5.28:
                        # exposes the documented schema-visible x402 payment
                        # retry carrier on guild_preflight_deep, so MCP clients
                        # that cannot set request metadata can settle the
                        # strongest-demand paid tool through the same bound,
                        # fail-closed gateway. Price, product and settlement
                        # authority are unchanged.
                        # History of 2.5.27: runs one reversible conversion
                        # experiment: priced
                        # x402 challenges no longer advertise the free
                        # sandbox faucet at the purchase moment unless
                        # GUILD_X402_TRIAL_CTA=1. The faucet, free identity,
                        # passport, pricing, settlement and attribution are
                        # unchanged.
                        # History of 2.5.26:
                        # mirrors every canonical PAYMENT-REQUIRED challenge
                        # at the top level of the JSON 402 body for
                        # header-limited autonomous clients, while preserving
                        # the prior FastAPI detail body and changing no
                        # pricing, binding, settlement or telemetry path.
                        # History of 2.5.25: makes every HTTP POST Bazaar
                        # extension conform to
                        # the official body-method contract (bodyType + body)
                        # and limits /.well-known/x402 reusable resources to
                        # requests that can actually execute after payment;
                        # body-bound products remain discoverable through
                        # OpenAPI/MCP and return an exact request-bound quote.
                        # History of 2.5.24: tells machine buyers that MCP
                        # payment retries may
                        # use either official request _meta or the paid tool's
                        # schema-visible x402_payment argument, so argument-
                        # only adapters do not stall after a valid challenge.
                        # History of 2.5.23: publishes a cacheable
                        # SEP-1649/Smithery MCP server
                        # card generated from the live FastMCP tool registry,
                        # so stale crawlers can learn every current tool and
                        # its x402 retry schema without a session, payment or
                        # fabricated buyer impression. History of 2.5.22: lets
                        # schema-driven MCP agents retry paid reads with
                        # the official x402 PaymentPayload in a visible tool
                        # argument when their adapter cannot set request
                        # metadata; the official _meta carrier remains
                        # preferred and both use the identical settlement
                        # gate. History of 2.5.21: lets an explicitly marked,
                        # entirely unpaid and
                        # unauthenticated machine-catalogue probe obtain the
                        # authoritative x402 quote without fabricating a buyer
                        # impression; payment, authentication, settlement and
                        # the quote itself are unchanged. History of 2.5.20:
                        # makes the x402 Bazaar output contract for /search
                        # match its paid ranked SearchResponse, so validating
                        # autonomous buyers can accept the result after
                        # settlement. History of 2.5.19: lets registry
                        # validators discover the paid search
                        # operation with a non-executable bare probe while
                        # malformed paid or authenticated calls still fail
                        # before settlement. History of 2.5.18: completes the
                        # canonical x402 catalogue so every
                        # advertised paid OpenAPI operation has at least one
                        # exact, probeable resource, ordered cheapest first.
                        # History of 2.5.17: publishes
                        # draft-payment-discovery-00 offers[] and
                        # x-service-info from the live quote source so generic
                        # Payment Auth registries can discover every payable
                        # operation without weakening 402 authority. History
                        # of 2.5.16: lets generic registries safely probe a
                        # parameterized
                        # protected tier with an opaque placeholder while any
                        # executing retry still fails before settlement.
                        # History of 2.5.15: adds the singular OpenAPI path
                        # example still used by
                        # MPPScan so the last tier route is discoverable by
                        # generic machine registries. History of 2.5.14: makes
                        # every paid route quoteable by generic machine
                        # registries before placeholder validation, and gives
                        # native MPP the same caller-proof nonce, discovery and
                        # replay semantics as x402 v2. History of 2.5.13:
                        # adds native MPP evm/charge HTTP compatibility for
                        # Base-USDC buyers while preserving the single x402
                        # facilitator, durable idempotency, independent-chain
                        # confirmation and externality revenue rules. History
                        # of 2.5.12:
                        # lets any machine refresh liveness evidence for an
                        # already-declared endpoint without gaining redirect,
                        # identity or reputation authority; durable cooldown,
                        # concurrency caps and SSRF-safe probing remain. History
                        # of 2.5.11: publishes the canonical domain-owned Agent Skills
                        # well-known index and exact repository policy, with
                        # a closed acquisition source. Payment, proof and
                        # settlement behavior do not change. History of
                        # 2.5.10: prepares the authenticated ClawHub one-file policy
                        # and measures its first paid-catalog follow-through
                        # with a closed source id once independent registry
                        # publication clears; payment and proof policy do not
                        # change. History of 2.5.9: publishes literal
                        # machine-buyer intents from one
                        # product catalog across MCP, A2A, OpenAPI, llms.txt
                        # and x402; corrects Bazaar examples to real response
                        # shapes without changing payment or proof policy.
                        # History of 2.5.8: makes the paid best-agent and
                        # signed-decision reads discoverable, measurable and
                        # completion-attributed on HTTP, MCP and A2A, while
                        # requiring independently attested externality before
                        # settlement is revenue. History of 2.5.7: gives every
                        # paid OpenAPI operation explicit
                        # machine-buyer intent, proof value and use cases for
                        # semantic x402 registries. History of 2.5.6:
                        # publishes live-price OpenAPI x-payment-info so
                        # OpenAPI-first machines can discover every payable
                        # HTTP utility and inspect x402 v2 before execution.
                        # History of 2.5.5: publishes the canonical
                        # /.well-known/x402 fan-out
                        # so directories discover every paid product and
                        # obtain price/schema from its own live 402 instead of
                        # reusing stale cross-product metadata. History of
                        # 2.5.4: accepts a registered W3C DID anywhere the passport
                        # read previously required a Guild-local id, and gives
                        # an unknown machine the exact self-registration call
                        # instead of a dead-end lookup error. History of 2.5.3:
                        # packages the official A2A header negotiation,
                        # Agent Card declaration and response echo together
                        # with the fail-closed exact-message receiver gate.
                        # History of 2.5.2: aligns activation with the official
                        # A2A contract: A2A-Extensions activates;
                        # Message.extensions is an optional signed description,
                        # not a second gate.
                        # History of 2.5.1: publishes a canonical A2A
                        # machine-envelope extension
                        # contract: standard Agent Card declaration and
                        # activation, exact semantic-message binding, response
                        # echo, and replay/error rules. History of 2.5.0:
                        # adds a zero-dependency receiver-side gate that keeps
                        # discovery free, then pins issuer, recipient, exact
                        # payload, purpose, expiry and A2A message id and
                        # atomically consumes a paid envelope before side
                        # effects. History of 2.4.1: makes every
                        # protected-payment tier directly buyable
                        # from its free machine catalog by publishing the
                        # canonical Payan offer, buy URL, treasury seller and
                        # exact caller-proof request binding. History of 2.4.0:
                        # adds a non-custodial Taskmarket requester adapter:
                        # exact task/reward/deadline/deliverables preview,
                        # fresh approval, paid signed delegation envelope,
                        # AGSM-1 pre-signature cap, read-only submission review,
                        # and reconcile-before-retry handling. History of 2.3.0:
                        # adds free AGSM-1 cumulative spend mandates: a Base
                        # EOA signs persistent total/per-payee/count caps and
                        # each exact budget authorization atomically advances
                        # durable state before an x402 client may sign.
                        # History of 2.2.3: lets x402 directories HEAD-probe
                        # /check without
                        # executing, recording demand/offers, or settling;
                        # the quote still binds buyers to the exact GET.
                        # History of 2.2.2 (2026-08-12): publishes a
                        # single-file PayanAgent MCP payment policy
                        # that defaults to value-priced protected AGPD-1 and
                        # binds the same Base EOA before the Payan wallet signs.
                        # History of 2.2.1 (2026-08-12): exposes exact
                        # $1k/$10k/$100k/$1m/$4m protected-payment
                        # tiers for fixed-price JSON marketplaces. Each tier
                        # keeps the 2.2.0 value policy and exact 25 bps fee,
                        # while caller proof seals the tier route, complete
                        # payment request and canonical Payan buy URL; caller
                        # EOA still must equal the x402 payer. History of 2.2.0:
                        # adds value-based protected payment decisions: 25 bps
                        # of exact Base-USDC value ($0.01 floor, $10,000 cap),
                        # signed into AGPD-1 with value-tier evidence, fresh
                        # verified routing, and pre-settlement caller EOA ==
                        # x402 payer enforcement. x402 and Virtuals adapters
                        # opt in with protectedValue:true. The ordinary cheap
                        # AGPD-1 path remains compatible. History of 2.1.9:
                        # makes the $0.01 AGPD-1 pre-payment decision executable
                        # through JSON-only marketplaces. Caller proof seals
                        # the exact payee, chain, token, amount, resource,
                        # policy and Payan buy URL; unsigned payment retries
                        # fail before settlement. History of 2.1.8 (2026-08-12):
                        # makes premium AGD-1 decisions executable through
                        # JSON-only marketplaces: caller proof signs the exact
                        # capability, TTL and Payan buy URL; settlement binds
                        # their opaque digest. Anonymous probes receive a
                        # non-executable quote and paid unsigned retries fail
                        # before settlement. History of 2.1.7 (2026-08-12):
                        # publishes named, intent-tagged x402 resource metadata
                        # and a truthful AGD-1 output contract for autonomous
                        # buyer discovery in Coinbase Bazaar after settlement.
                        # History of 2.1.6 (2026-08-10):
                        # makes the Virtuals ACP adapter purchase and locally
                        # verify one AGPD-1 decision bound to the exact provider,
                        # chain, token, atomic amount and job URL before fund();
                        # publishes both wallet-policy factories in machine
                        # discovery. The identity+risk factory stays compatible.
                        # History of 2.1.5 (2026-08-08):
                        # makes paid machine envelopes directly listable and
                        # buyable through a headerless x402 marketplace relay:
                        # caller proof rides in a strict JSON wrapper, signs
                        # the complete semantic request, and binds the exact
                        # PayanAgent buy URL without permitting arbitrary
                        # resource substitution. Anonymous registry probes
                        # receive a non-executable discovery 402; anonymous
                        # payment retries still fail before settlement.
                        # History of 2.1.4 (2026-08-07):
                        # adds AGPD-1: a paid, short-lived eddsa-jcs-2022
                        # credential binding an exact x402 payee, chain, asset,
                        # amount and resource to signed wallet identity, live
                        # risk evidence and explicit thresholds; plus a public
                        # official-client hook that enforces it before payment
                        # payload creation. Additive API + registry metadata.
                        # History of 2.1.3 (2026-08-07):
                        # publishes the exact-wallet pre-payment policy in the
                        # immutable MCP Registry listing: a machine can resolve
                        # an EVM+CAIP-2 binding and load the Virtuals ACP
                        # fail-closed fund adapter without reading prose. The
                        # underlying additive wallet-binding API shipped in
                        # 2.1.2; this bump makes it externally discoverable.
                        # History of 2.1.2 (2026-08-07):
                        # one caller-owned Base EOA can now authenticate the
                        # exact machine-envelope request with EIP-191 and pay
                        # the x402 challenge. The separate did:key signer path
                        # remains supported. This is additive caller-auth
                        # behaviour plus changed registry acquisition metadata.
                        # History of 2.1.1 (2026-08-07):
                        # acquisition metadata now leads with the already-live
                        # signed machine-message product and embeds its one-call
                        # client in the official MCP Registry listing. No API or
                        # payment behaviour changes; the bump is required because
                        # registry metadata already published as 2.1.0 is immutable.
                        # History of 2.1.0 (2026-08-07):
                        # adds the machine-envelope product: paid, caller-
                        # proof-bound issuance of privacy-preserving signed
                        # message/intent commitments, plus free verification,
                        # across REST, MCP and every discovery surface. This
                        # is additive API behaviour and changes the already-
                        # published registry operation list, hence a new minor
                        # version rather than silently reusing 2.0.3.
                        # History of 2.0.3 (2026-08-01):
                        # registry discovery release — the publisher-provided
                        # metadata gains ai.agent-guild/paid-operations, which
                        # NAMES the paid operations and points at one
                        # live, registry-attributed catalog URL
                        # (/.well-known/agent-guild.json?src=paid_offer:registry).
                        # Deliberately carries NO prices: they move when the
                        # autonomous experiment engine runs, and a listing is
                        # republished rarely, so a copied price would be stale
                        # and a stale price is a lie. The passport block and
                        # the description are UNCHANGED and still lead.
                        # Bumped because ALREADY-PUBLISHED registry metadata
                        # changes, which must never silently reuse a version:
                        # the automated publish + exact-version readback keys
                        # off server.json changing. No API behaviour changes.
                        # History of 2.0.2 (2026-07-23):
                        # acquisition-only metadata release — the MCP Registry
                        # listing becomes passport-first (free self-serve
                        # Agent Passports: register → prove control → signed
                        # portable credential → evidence → offline verify)
                        # and the ai.agent-guild/payments block leaves the
                        # registry discovery metadata (payment behaviour on
                        # the service itself is UNCHANGED: same x402 gateway,
                        # same priced operations, still declared in
                        # contract.json and challenged honestly at call time).
                        # No API behaviour changes.
                        # History of 2.0.1 (2026-07-17):
                        # machine-integrity correction — adds the PUBLIC
                        # caller-proof (agent-guild/caller-proof/v1) and
                        # wallet-binding contracts to every surface, wires
                        # MCP _meta caller-proof verification on the real
                        # execution path, and replaces the self-mintable
                        # "verified_external_machine" settlement class with
                        # conservative classes (cryptographically bound ≠
                        # external; externality needs an independent
                        # allowlisted attestor). No breaking changes.
                        # History of 2.0.0:
                        # DELIBERATE MAJOR BUMP (machine-visible semver,
                        # 2026-07-15): payment enforcement on previously-free
                        # MCP tools (guild_check/guild_search/guild_best_agent/
                        # guild_risk_score) and the A2A `check` skill shipped
                        # AFTER 1.2.0 was already published to the MCP
                        # Registry. For a machine consumer "this call now
                        # returns a payment challenge instead of the result"
                        # is a breaking contract change; it must never reuse a
                        # published version. 1.x listings describe the
                        # pre-enforcement behaviour; 2.0.0 declares the x402
                        # payment mechanism + priced operations in its
                        # publisher-provided registry metadata.
                        # (Also in 2.0.0: x402 offer/receipt did:web service
                        # identity + durable payment crash recovery.)
