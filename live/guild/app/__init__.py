"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "2.1.3"   # PATCH (machine-visible semver, 2026-08-07):
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
