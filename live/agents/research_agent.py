"""The Research Agent — the autonomous *consumer* and the experiment's decision-maker.

When it needs a claim fact-checked it must pick a counterparty. It can either
consult the Guild (discover fact-checkers, rank by reputation + advertised
price/latency, choose the best expected value) or ignore the Guild (pick at
random, as it would have to without a trust layer). After execution it observes
correctness and issues a signed attestation back to the Guild.

No human is in the loop. The agent chooses.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from agentguild import GuildClient, GuildIdentity
from runtime import AgentRuntime


@dataclass
class SelectionWeights:
    """The agent's own payoff structure, used to rank counterparties by EXPECTED
    UTILITY. Because a correct vs wrong fact-check swings utility by reward+penalty
    (~2.0) while price differences are ~0.05, a rational agent weights quality far
    above price — but still breaks ties toward cheaper, faster agents."""
    reward: float = 1.0       # utility for a correct result
    penalty: float = 1.5      # utility lost for a wrong result (matches Econ)
    latency_cost: float = 0.05  # utility cost per second
    explore: float = 0.1      # chance to sample a random candidate (cold-start)


@dataclass
class Outcome:
    used_guild: bool
    chosen_id: str
    chosen_name: str
    correct: bool
    price: float
    latency_ms: float
    rating_issued: float


class ResearchAgent:
    def __init__(
        self,
        client: GuildClient,
        identity: GuildIdentity,
        runtime: AgentRuntime,
        rng: random.Random,
        weights: SelectionWeights | None = None,
    ):
        self.client = client
        self.identity = identity
        self.runtime = runtime
        self.rng = rng
        self.weights = weights or SelectionWeights()

    # --- selection ----------------------------------------------------------
    def _p_correct(self, r: dict) -> float:
        """Estimate P(correct) from reputation. Trust is already confidence-shrunk
        on the server; we additionally pull thin-evidence estimates toward 0.5 so
        the agent doesn't over-trust an agent it has barely seen."""
        t = r["trust"] / 100.0
        conf = r.get("confidence", 0.0)
        p = 0.5 + (t - 0.5) * (0.5 + 0.5 * conf)
        return min(0.98, max(0.02, p))

    def _expected_utility(self, r: dict) -> float:
        w = self.weights
        p = self._p_correct(r)
        price = r["metadata"].get("price_per_call", 0.0)
        lat = r["metadata"].get("est_latency_ms", 0.0) / 1000.0
        return w.reward * p - w.penalty * (1 - p) - price - w.latency_cost * lat

    def _select_by_reputation(self, results: list[dict]) -> dict:
        """Pick the counterparty with the highest expected utility, with a little
        exploration so cold-start agents still get sampled (and earn a record)."""
        if self.rng.random() < self.weights.explore:
            return self.rng.choice(results)
        return max(results, key=self._expected_utility)

    # --- the task -----------------------------------------------------------
    def fact_check(
        self,
        claim: str,
        label: bool,
        use_guild: bool,
        known_factcheckers: list[str],
    ) -> Outcome:
        if use_guild:
            results = self.client.search("fact-check", limit=50)
            chosen = self._select_by_reputation(results) if results else None
            chosen_id = chosen["id"] if chosen else self.rng.choice(known_factcheckers)
            chosen_name = chosen["name"] if chosen else "(random fallback)"
        else:
            # No trust layer: the agent knows providers exist but nothing about them.
            chosen_id = self.rng.choice(known_factcheckers)
            chosen_name = self.runtime.identities[chosen_id].id

        profile = self.runtime.profile_of(chosen_id)
        result = self.runtime.execute_factcheck(chosen_id, claim, mock_label=label)
        correct = result.verdict == label
        rating = 1.0 if correct else 0.0

        # Honest attestation of observed quality — this is the product.
        self.client.attest(
            self.identity, chosen_id, "fact-check", rating,
            comment=f"verdict {'correct' if correct else 'wrong'}",
        )

        return Outcome(
            used_guild=use_guild, chosen_id=chosen_id, chosen_name=chosen_name,
            correct=correct, price=profile.price_per_call,
            latency_ms=result.latency_ms, rating_issued=rating,
        )
