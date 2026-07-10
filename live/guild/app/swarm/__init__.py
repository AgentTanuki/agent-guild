"""Discovery Swarm — machine-native distribution layer for Agent Guild.

Six cooperating layers (docs/discovery-swarm/architecture.md):
  L1 identity factory · L2 capability seeds · L3 discovery mapper ·
  L4 discovery agents · L5 acquisition gateway · L6 provenance/referral graph.

Identities are signed documents, not processes; the shared runtime for
Pilot A is the existing FastAPI service. Every capability is deterministic,
side-effect-free, fixture-gated, and returns a Guild-signed provenance
envelope. AG-internal traffic is excluded from growth metrics by the
existing attribution layer.
"""
