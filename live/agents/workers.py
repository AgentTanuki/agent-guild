"""Reference worker agents: Fact-Check and Summarisation.

Each worker is a real autonomous component backed by an LLM. Quality, cost, and
latency are genuine properties of how it is configured (model + reliability
prompt), which is what gives the marketplace real economic trade-offs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agentguild import llm

RELIABILITY_PROMPT = {
    "high": (
        "You are a meticulous fact-checker. RELIABILITY=HIGH. Read the claim carefully, "
        "reason about whether it is factually true, then answer. Output exactly one line "
        "'VERDICT: TRUE' or 'VERDICT: FALSE', then a brief reason."
    ),
    "medium": (
        "You are a fact-checker. RELIABILITY=MEDIUM. Decide whether the claim is true or false. "
        "Output 'VERDICT: TRUE' or 'VERDICT: FALSE' then a short reason."
    ),
    "low": (
        "You are a busy fact-checker. RELIABILITY=LOW. Answer as fast as possible and do not "
        "overthink; guess if unsure. Output 'VERDICT: TRUE' or 'VERDICT: FALSE'."
    ),
}


@dataclass
class WorkerProfile:
    name: str
    capability: str
    provider: str            # "mock" | "openai" | "anthropic"
    model: str
    reliability: str         # "high" | "medium" | "low"
    price_per_call: float    # economic cost charged to the hirer (experiment unit)
    est_latency_ms: float    # advertised latency, used in discovery

    def metadata(self) -> dict:
        return {
            "model": self.model, "provider": self.provider, "tier": self.reliability,
            "price_per_call": self.price_per_call, "est_latency_ms": self.est_latency_ms,
        }


@dataclass
class FactCheckResult:
    verdict: bool            # True == claim asserted true
    text: str
    latency_ms: float


class FactCheckAgent:
    def __init__(self, profile: WorkerProfile):
        self.profile = profile

    def verify(self, claim: str, mock_label: Optional[bool] = None) -> FactCheckResult:
        system = RELIABILITY_PROMPT[self.profile.reliability]
        user = f"Claim: {claim}\nIs this claim true or false?"
        # mock_label is ONLY consumed by the offline mock backend so self-tests
        # produce a quality gradient; real providers never receive it.
        if self.profile.provider == "mock" and mock_label is not None:
            user += f"\n[label:{'true' if mock_label else 'false'}]"
        c = llm.complete(self.profile.provider, self.profile.model, system, user)
        verdict = "VERDICT: TRUE" in c.text.upper()
        return FactCheckResult(verdict=verdict, text=c.text, latency_ms=c.latency_ms)


class SummariserAgent:
    def __init__(self, profile: WorkerProfile):
        self.profile = profile

    def summarise(self, text: str) -> str:
        system = "You are a concise summariser. Summarise the text in one sentence."
        c = llm.complete(self.profile.provider, self.profile.model, system, text)
        return c.text
