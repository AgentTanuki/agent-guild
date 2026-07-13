"""Agent Guild Trust Plane — local delegation gateway + framework interceptors.

The trust plane makes Agent Guild the default layer machines consult
automatically when delegating to other machines:

* ``verify``    — standalone eddsa-jcs-2022 / did:key verifier (no Guild deps)
* ``contract``  — the AGD-1 decision contract (validate, freshness)
* ``policy``    — caller-owned risk policies (thresholds + fail modes by tier)
* ``cache``     — signed offline decision/passport cache with freshness metrics
* ``client``    — Guild client with outage fallback to the signed cache
* ``engine``    — policy evaluation: AGD-1 decision × caller policy → PolicyResult
* ``outcomes``  — signed outcome records, reported back to the Guild
* ``gateway``   — the facade every integration calls: gate() / report()
* ``sidecar``   — local HTTP daemon exposing the gateway to any process
* ``mcp_proxy`` — MCP stdio proxy that gates downstream tools/call
* ``integrations`` — real lifecycle interceptors for CrewAI, LangChain/
  LangGraph, OpenAI Agents

Design rule: the Guild presents evidence; the CALLER owns thresholds. The
gateway never receives a verdict from the network — it computes ``PolicyResult``
locally from the caller's own policy over verifiable evidence.
"""
__version__ = "0.1.0"

from .contract import validate_decision, decision_fresh  # noqa: F401
from .policy import RiskPolicy, PolicyResult, TierRule    # noqa: F401
from .engine import evaluate                              # noqa: F401
from .gateway import Gateway, GateDenied                  # noqa: F401
