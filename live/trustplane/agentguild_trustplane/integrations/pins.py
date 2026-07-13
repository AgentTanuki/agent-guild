"""Supported framework versions — pinned, tested ranges.

`check_pins()` is executed by the integration test suite on the installed
environment; an untested framework version fails loudly instead of guessing.
"""
from __future__ import annotations

import importlib
from typing import Any

# name -> (import module, tested lower bound, tested upper bound EXCLUSIVE)
SUPPORTED: dict[str, dict[str, Any]] = {
    "crewai": {"module": "crewai", "min": (1, 15), "max": (2, 0),
               "tested": "1.15.2"},
    "langchain-core": {"module": "langchain_core", "min": (1, 4), "max": (2, 0),
                       "tested": "1.4.9"},
    "langgraph": {"module": "langgraph", "min": (1, 0), "max": (2, 0),
                  "tested": "1.2.9"},
    "openai-agents": {"module": "agents", "min": (0, 18), "max": (0, 30),
                      "tested": "0.18.2"},
    "mcp": {"module": "mcp", "min": (1, 20), "max": (2, 0),
            "tested": "1.28.1"},
}


def _ver(mod: Any, dist_name: str) -> tuple[int, ...]:
    raw = getattr(mod, "__version__", None)
    if raw is None:
        from importlib.metadata import version as _v
        try:
            raw = _v(dist_name)
        except Exception:
            raw = None
    if raw is None:
        return ()
    return tuple(int(p) for p in raw.split(".")[:2] if p.isdigit())


def check_pins(strict: bool = False) -> dict[str, dict[str, Any]]:
    """-> {framework: {installed, supported, reason}} for every SUPPORTED
    framework present in the environment."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in SUPPORTED.items():
        try:
            mod = importlib.import_module(spec["module"])
        except ImportError:
            out[name] = {"installed": None, "supported": False,
                         "reason": "not installed"}
            continue
        v = _ver(mod, name)
        ok = bool(v) and spec["min"] <= v < spec["max"]
        out[name] = {"installed": ".".join(map(str, v)) if v else "unknown",
                     "supported": ok,
                     "reason": ("ok" if ok else
                                f"outside tested range [{spec['min']},{spec['max']})")}
        if strict and not ok:
            raise RuntimeError(f"{name} {out[name]['installed']} "
                               f"is outside the tested range")
    return out
