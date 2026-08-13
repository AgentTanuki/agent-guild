"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "2.5.7"   # PATCH (machine-visible semver, 2026-08-13):
                        # gives every paid OpenAPI operation explicit
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
