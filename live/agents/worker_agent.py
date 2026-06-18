"""General worker agent that solves any task type with a real LLM.

Quality, cost, and latency are genuine, visible properties (model, reliability
prompt, specialty). The offline `mock` backend synthesises deterministic answers
with a reliability-shaped accuracy so the harness is self-testable; real
providers receive only the real prompt (never the answer key).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from agentguild import llm
from tasks_real import TaskItem, render, _alts

CAPABILITY = "qa-agent"  # every worker advertises this; the hiring agent searches it

_REL = {
    "high": "You are a careful, expert analyst. Reason step by step internally, then answer.",
    "medium": "You are a competent analyst. Determine the answer and respond in the required format.",
    "low": "You are in a hurry. Answer immediately without deliberation; a fast guess is acceptable.",
}
_P_CORRECT = {"high": 0.95, "medium": 0.74, "low": 0.52}


@dataclass
class WorkerProfile:
    name: str
    provider: str            # "openai" | "anthropic" | "mock"
    model: str
    reliability: str         # high | medium | low
    specialty: str           # domain tag or "general"
    price_per_call: float    # economic cost charged to the hirer (experiment unit)
    est_latency_ms: float    # advertised latency

    def metadata(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "tier": self.reliability,
            "specialty": self.specialty, "price_per_call": self.price_per_call,
            "est_latency_ms": self.est_latency_ms,
        }


@dataclass
class WorkResult:
    raw: str
    latency_ms: float


class WorkerAgent:
    def __init__(self, profile: WorkerProfile):
        self.profile = profile

    def _system(self, task: TaskItem) -> str:
        s = _REL[self.profile.reliability]
        if self.profile.specialty != "general" and task.specialty == self.profile.specialty:
            s = f"You are a leading expert in {self.profile.specialty}. " + s
        if task.type == "summary":
            s += " Produce a single concise sentence capturing the key facts."
        else:
            s += " End with a line exactly like 'ANSWER: <TOKEN>'."
        return s

    def do(self, task: TaskItem, mock_key: Optional[str] = None) -> WorkResult:
        if self.profile.provider == "mock":
            return self._mock(task)
        _, user = render(task)
        c = llm.complete(self.profile.provider, self.profile.model, self._system(task), user)
        return WorkResult(raw=c.text, latency_ms=c.latency_ms)

    # Deterministic offline behaviour: accuracy shaped by reliability + specialty.
    def _mock(self, task: TaskItem) -> WorkResult:
        rng = random.Random(hash((task.id, self.profile.name)) & 0xFFFFFFFF)
        p = _P_CORRECT[self.profile.reliability]
        if self.profile.specialty != "general" and task.specialty == self.profile.specialty:
            p = max(p, 0.96)
        correct = rng.random() < p
        lat = self.profile.est_latency_ms * rng.uniform(0.8, 1.2)
        if task.type == "summary":
            if correct:
                bits = [grp[0] for grp in task.rubric]
                raw = "Summary: " + ", ".join(bits) + "."
            else:
                raw = "Summary: this text discusses a topic of some general interest."
            return WorkResult(raw=raw, latency_ms=lat)
        if correct:
            tok = task.answer_key
        else:
            alts = [t for t in _alts(task) if t != task.answer_key]
            tok = alts[0] if alts else "UNSURE"
        return WorkResult(raw=f"ANSWER: {tok}", latency_ms=lat)
