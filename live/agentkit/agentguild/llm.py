"""LLM provider abstraction for reference agents and the Phase 3 experiment.

Supports real OpenAI and Anthropic calls, plus a deterministic `mock` backend so
the whole system can be self-tested without keys or cost. Real providers are
used when the matching SDK is installed and the API key is set.

A single `complete()` returns text plus measured latency, so callers can model
the cost / quality / latency trade-offs the experiment is about.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass


@dataclass
class Completion:
    text: str
    latency_ms: float
    provider: str
    model: str


def available_providers() -> list[str]:
    out = ["mock"]
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            out.append("openai")
        except Exception:
            pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            out.append("anthropic")
        except Exception:
            pass
    return out


def complete(
    provider: str,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> Completion:
    t0 = time.time()
    if provider == "openai":
        text = _openai(model, system, user, temperature, max_tokens)
    elif provider == "anthropic":
        text = _anthropic(model, system, user, temperature, max_tokens)
    else:
        text = _mock(model, system, user)
    return Completion(text=text, latency_ms=(time.time() - t0) * 1000, provider=provider, model=model)


def _openai(model, system, user, temperature, max_tokens) -> str:
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _anthropic(model, system, user, temperature, max_tokens) -> str:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, system=system, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(getattr(b, "text", "") for b in resp.content).strip()


def _mock(model: str, system: str, user: str) -> str:
    """Deterministic stand-in. Used only for offline self-test; the reference
    agents parse a verdict token from the output, which this still provides."""
    h = hashlib.sha256((model + system + user).encode()).hexdigest()
    # Bias the canned verdict by a hidden 'reliability' hint in the system prompt,
    # so mock runs still exercise the quality-difference logic end to end.
    bit = int(h[:2], 16)
    if "RELIABILITY=HIGH" in system:
        verdict = "TRUE" if "[label:true]" in user.lower() else "FALSE"
    elif "RELIABILITY=LOW" in system:
        verdict = "TRUE" if bit % 2 == 0 else "FALSE"
    else:
        verdict = "TRUE" if bit % 3 != 0 else "FALSE"
    return f"VERDICT: {verdict}\nREASON: (mock) deterministic response {h[:6]}"
