#!/usr/bin/env python3
"""Generate the CANONICAL machine contract (contract.json) plus the derived
artifacts (repo-root server.json, docs/INTERFACE.md) from the running code.

One source of truth, enforced two ways:
  * `contract.json` is exported from the actual FastAPI routes, MCP tool
    registry and A2A agent card — it cannot disagree with the code that
    generates it.
  * CI regenerates everything and fails on any uncommitted diff
    (tests/test_contract_conformance.py + `make contract`), so REST, MCP,
    A2A, the registry manifest and the interface docs can never drift apart
    silently: a surface change forces a reviewed contract diff.

Run from live/guild:  python contract/generate.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

os.environ.setdefault("GUILD_DATA", "")
os.environ.setdefault("GUILD_ALLOW_WEAK_KDF", "1")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

HOST = "https://agent-guild-5d5r.onrender.com"
REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent

# Framework-provided routes that are not part of the Guild contract surface.
FRAMEWORK_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _iter_routes(routes):
    for r in routes:
        methods = getattr(r, "methods", None)
        if methods:
            yield r
        sub = getattr(r, "routes", None)
        if sub:
            yield from _iter_routes(sub)


def build_contract() -> dict:
    from app.main import app
    from app.mcp_server import mcp
    from app import __version__

    rest = {}
    for r in _iter_routes(app.routes):
        if r.path in FRAMEWORK_PATHS:
            continue
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            rest.setdefault(r.path, []).append(m)
    rest_list = [{"path": p, "methods": sorted(set(ms))}
                 for p, ms in sorted(rest.items())]

    tools = sorted(t.name for t in asyncio.run(mcp.list_tools()))

    # A2A: static skills are fixed contract; ag.* skills mirror the published
    # swarm capabilities (fixture-gated at boot, one per capability id).
    a2a_static = ["guild.check", "guild.capabilities", "guild.invoke"]
    from app.swarm.capabilities import CAPABILITIES
    swarm_caps = sorted(CAPABILITIES)

    return {
        "contract_version": 2,
        "service": {
            "name": "Agent Guild",
            "version": __version__,
            "host": HOST,
            "mcp_url": f"{HOST}/mcp/",
            "a2a_endpoint": f"{HOST}/a2a",
            "agent_card": f"{HOST}/.well-known/agent-card.json",
            "issuer_did_document": f"{HOST}/.well-known/agent-guild-did.json",
            "repository": "https://github.com/AgentTanuki/agent-guild",
        },
        "proof_suites": {
            "current": {"type": "DataIntegrityProof",
                        "cryptosuite": "eddsa-jcs-2022",
                        "spec": "https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022"},
            "legacy": {"type": "Ed25519Signature2020",
                       "status": "verify-only historical AGI-1 format; never issued",
                       "doc": "docs/PROOF_SUITES.md"},
        },
        "provenance": {
            "tiers": ["guild_mediated", "verifiable_outcome", "mutual_attestation",
                      "external_import", "one_party_claim", "first_party_bootstrap"],
            "rule": ("guild_mediated requires two-party cryptographic participation, "
                     "a Guild-observed bound invocation, or independent escrow "
                     "settlement; signers lists only DIDs that actually signed"),
            "rules_version": "prov-v2",
        },
        "rest": rest_list,
        "mcp_tools": tools,
        "a2a_skills_static": a2a_static,
        "a2a_dynamic_skills": [f"ag.{c}" for c in swarm_caps],
    }


def derived_server_json(contract: dict) -> dict:
    s = contract["service"]
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        # MUST match the OIDC-granted namespace EXACTLY (case-sensitive):
        # GitHub OIDC grants `io.github.<repository_owner>/*` with the owner's
        # canonical GitHub casing (registry github_oidc.go:293 + jwt.go
        # isResourceMatch). There is NO lowercase canonicalization in the
        # registry; the 2026-07-13 lowercase change caused the 403 in run
        # 29274449452. Same identity as the published 1.0.0/1.1.0 listing.
        # See docs/CORRECTIONS_2026-07-14.md.
        "name": "io.github.AgentTanuki/agent-guild",
        # registry schema caps description at 100 chars — keep this short
        "description": ("Trust layer for AI agents: signed delegation "
                        "decisions, passports, and 16 verified guest tools."),
        "version": s["version"],
        "repository": {"url": s["repository"], "source": "github"},
        "websiteUrl": s["host"],
        "remotes": [{"type": "streamable-http", "url": s["mcp_url"]}],
        # Publisher-provided trust metadata. The official registry serves back
        # ONLY `_meta["io.modelcontextprotocol.registry/publisher-provided"]`
        # (pkg/api/v0/types.go — any other top-level _meta key is silently
        # dropped; 4KB limit). Our trust block therefore nests under it.
        "_meta": {
            "io.modelcontextprotocol.registry/publisher-provided": {
                "ai.agent-guild/trust": _trust_meta(s),
            },
        },
    }


def _trust_meta(s: dict) -> dict:
    """Machine-readable pointer set: how a consumer obtains SIGNED,
    offline-verifiable delegation evidence about agents before trusting them.
    Kept well under the registry's 4KB publisher-provided limit."""
    return {
                "contract": "AGD-1/1.0",
                "proof_suite": "eddsa-jcs-2022",
                "decision_endpoint": (s["host"] + "/check?capability="
                                      "{capability}&signed=true"),
                "passport_endpoint": s["host"] + "/agents/{id}/passport",
                "checkpoint_feed": s["host"] + "/ledger/checkpoints",
                "a2a_extension": "https://agent-guild.ai/ext/trust/v1",
                "conformance": (s["repository"] + "/blob/main/live/"
                                "trustplane/conformance/AGI1_CONFORMANCE.md"),
                "note": ("Signed AGD-1 delegation decisions and offline-"
                         "verifiable Agent Passports; callers own thresholds. "
                         "Delegation gateway + framework interceptors: "
                         "live/trustplane in the repository."),
    }


def derived_interface_md(contract: dict) -> str:
    s = contract["service"]
    lines = [
        "# Agent Guild — machine interface (GENERATED)",
        "",
        f"*Generated from `live/guild/contract/contract.json` v{contract['contract_version']} "
        f"(service {s['version']}). Do not edit by hand — run `make contract`.*",
        "",
        f"- Host: {s['host']}",
        f"- MCP (streamable HTTP): {s['mcp_url']}",
        f"- A2A JSON-RPC: {s['a2a_endpoint']} · agent card: {s['agent_card']}",
        f"- Issuer DID: {s['issuer_did_document']}",
        "",
        "## Proof suites",
        "",
        f"- Current: `{contract['proof_suites']['current']['type']}` / "
        f"`{contract['proof_suites']['current']['cryptosuite']}` "
        f"({contract['proof_suites']['current']['spec']})",
        f"- Legacy: {contract['proof_suites']['legacy']['status']} "
        f"({contract['proof_suites']['legacy']['doc']})",
        "",
        "## Provenance tiers",
        "",
        f"`{'` > `'.join(contract['provenance']['tiers'][:4])}`; plus labelled "
        f"`one_party_claim` and `first_party_bootstrap`.",
        "",
        contract["provenance"]["rule"] + ".",
        "",
        "## REST endpoints",
        "",
    ]
    for r in contract["rest"]:
        lines.append(f"- `{' | '.join(r['methods'])} {r['path']}`")
    lines += ["", "## MCP tools", ""]
    for t in contract["mcp_tools"]:
        lines.append(f"- `{t}`")
    lines += ["", "## A2A skills", ""]
    for sk in contract["a2a_skills_static"]:
        lines.append(f"- `{sk}` (static)")
    for sk in contract["a2a_dynamic_skills"]:
        lines.append(f"- `{sk}`")
    return "\n".join(lines) + "\n"


def main(write: bool = True) -> dict:
    contract = build_contract()
    if write:
        (HERE / "contract.json").write_text(
            json.dumps(contract, indent=1, sort_keys=True) + "\n")
        (REPO / "server.json").write_text(
            json.dumps(derived_server_json(contract), indent=2) + "\n")
        (REPO / "docs" / "INTERFACE.md").write_text(derived_interface_md(contract))
        print("wrote contract/contract.json, server.json, docs/INTERFACE.md")
    return contract


if __name__ == "__main__":
    main()
