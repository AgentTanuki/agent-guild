"""Autonomous hiring agent for the real-world experiment.

Free to use the Guild or bypass it. When it uses the Guild it selects the worker
with the highest EXPECTED UTILITY (estimated P(correct) from reputation, traded
off against price and latency), with light exploration. After observing the
deterministically-evaluated outcome it issues a signed attestation. No hardcoded
preference for any worker; no human in the loop.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from agentguild import GuildClient, GuildIdentity
from worker_agent import WorkerAgent, CAPABILITY
from tasks_real import TaskItem, evaluate


@dataclass
class SelectionWeights:
    reward: float = 1.0
    penalty: float = 1.5        # a wrong answer is costlier than a right one is valuable
    latency_cost: float = 0.05  # per second
    explore: float = 0.1
    optimism: float = 0.35      # exploration bonus for under-observed (low-confidence) agents


@dataclass
class Outcome:
    used_guild: bool
    chosen_id: str
    chosen_name: str
    provider: str
    model: str
    task_type: str
    correct: bool
    price: float
    latency_ms: float


class HiringAgent:
    def __init__(
        self,
        client: GuildClient,
        identity: GuildIdentity,
        workers: dict[str, WorkerAgent],
        rng: random.Random,
        weights: Optional[SelectionWeights] = None,
        offline: bool = False,
    ):
        self.client = client
        self.identity = identity
        self.workers = workers
        self.rng = rng
        self.w = weights or SelectionWeights()
        self.offline = offline  # pass answer key to the mock backend only

    def _p_correct(self, r: dict) -> float:
        t = r["trust"] / 100.0
        conf = r.get("confidence", 0.0)
        return min(0.98, max(0.02, 0.5 + (t - 0.5) * (0.5 + 0.5 * conf)))

    def _eu(self, r: dict) -> float:
        p = self._p_correct(r)
        conf = r.get("confidence", 0.0)
        price = r["metadata"].get("price_per_call", 0.0)
        lat = r["metadata"].get("est_latency_ms", 0.0) / 1000.0
        eu = self.w.reward * p - self.w.penalty * (1 - p)
        # Cost/latency only sway the choice once we actually have quality evidence;
        # otherwise minimising price degenerates to 'pick cheapest' (often the worst).
        eu -= conf * (price + self.w.latency_cost * lat)
        # Optimism under uncertainty: try under-observed agents so the graph fills
        # instead of fixating early on a cheap, unproven one.
        eu += self.w.optimism * (1 - conf)
        return eu

    def _select(self, results: list[dict]) -> dict:
        if self.rng.random() < self.w.explore:
            return self.rng.choice(results)
        return max(results, key=self._eu)

    def run_task(self, task: TaskItem, use_guild: bool, known_ids: list[str]) -> Outcome:
        if use_guild:
            results = self.client.search(CAPABILITY, limit=50)
            chosen = self._select(results) if results else None
            chosen_id = chosen["id"] if chosen else self.rng.choice(known_ids)
        else:
            chosen_id = self.rng.choice(known_ids)

        worker = self.workers[chosen_id]
        res = worker.do(task, mock_key=task.answer_key if self.offline else None)
        correct = evaluate(task, res.raw)

        self.client.attest(
            self.identity, chosen_id, CAPABILITY, 1.0 if correct else 0.0,
            task_id=task.id, comment=f"{task.type}:{'ok' if correct else 'wrong'}",
        )
        pf = worker.profile
        return Outcome(
            used_guild=use_guild, chosen_id=chosen_id, chosen_name=pf.name,
            provider=pf.provider, model=pf.model, task_type=task.type,
            correct=correct, price=pf.price_per_call, latency_ms=res.latency_ms,
        )
