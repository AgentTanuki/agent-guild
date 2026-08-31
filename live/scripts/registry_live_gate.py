#!/usr/bin/env python3
"""Focused live gate for publishing Agent Guild's two MCP Registry entries.

The general release gate certifies the whole service. Registry publication has
a narrower question: does production serve this exact SHA, and do the two MCP
surfaces/cards exactly match the manifests being published? Unrelated analytics
reads must not block distribution, while MCP drift must always fail closed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request
from typing import Any


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "live" / "scripts"))
import live_contract_probe as live  # noqa: E402

CONTRACT = json.loads(
    (REPO / "live" / "guild" / "contract" / "contract.json").read_text())
HOST = CONTRACT["service"]["host"]


def card_failures(card: dict[str, Any], *, expected_tools: list[str],
                  endpoint: str) -> list[str]:
    failures: list[str] = []
    tools = sorted(tool.get("name") for tool in card.get("tools", [])
                   if tool.get("name"))
    if tools != sorted(expected_tools):
        failures.append(
            f"server card tools differ: live={len(tools)} "
            f"contract={len(expected_tools)}")
    if (card.get("serverInfo") or {}).get("version") != \
            CONTRACT["service"]["version"]:
        failures.append("server card version differs from contract")
    if (card.get("transport") or {}).get("endpoint") != endpoint:
        failures.append("server card transport endpoint differs from contract")
    return failures


def _rpc(url: str, method: str, params: dict[str, Any], *,
         session: str | None = None, id_: int | None = None):
    message: dict[str, Any] = {
        "jsonrpc": "2.0", "method": method, "params": params}
    if id_ is not None:
        message["id"] = id_
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Guild-Source": "guild-ci",
        "User-Agent": "guild-registry-live-gate",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        url, data=json.dumps(message).encode(), method="POST", headers=headers)
    response = live.open_request(request, timeout=60)
    raw = response.read().decode()
    payload = raw
    if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        payload = next(line[5:].strip() for line in raw.splitlines()
                       if line.startswith("data:"))
    return response.headers.get("mcp-session-id"), (
        json.loads(payload) if payload.strip() else None)


def _surface_failures(label: str, url: str, expected_tools: list[str]) -> list[str]:
    try:
        session, _ = _rpc(url, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "guild-registry-live-gate", "version": "1.0"},
        }, id_=1)
        _rpc(url, "notifications/initialized", {}, session=session)
        _, response = _rpc(url, "tools/list", {}, session=session, id_=2)
        tools = sorted(tool["name"] for tool in response["result"]["tools"])
    except Exception as exc:  # noqa: BLE001 - gate reports exact live failure
        return [f"{label} MCP handshake failed: {exc}"]
    if tools != sorted(expected_tools):
        return [f"{label} MCP tools differ: live={len(tools)} "
                f"contract={len(expected_tools)}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    failures: list[str] = []

    try:
        release = json.load(live.get(HOST + "/release"))
        if release.get("git_sha") != args.sha:
            failures.append(
                f"production SHA {release.get('git_sha')!r} != workflow {args.sha!r}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"release identity unavailable: {exc}")

    surfaces = (
        ("canonical", "/.well-known/mcp/server-card.json",
         CONTRACT["service"]["mcp_url"], CONTRACT["mcp_tools"], "/mcp/"),
        ("focused payment-safety",
         "/.well-known/mcp/payment-safety-server-card.json",
         CONTRACT["service"]["payment_safety_mcp_url"],
         CONTRACT["payment_safety_mcp_tools"], "/mcp/payment-safety/"),
    )
    for label, card_path, mcp_url, expected, endpoint in surfaces:
        try:
            card = json.load(live.get(HOST + card_path))
            failures.extend(
                f"{label}: {failure}" for failure in
                card_failures(card, expected_tools=expected, endpoint=endpoint))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label} server card unavailable: {exc}")
        failures.extend(_surface_failures(label, mcp_url, expected))

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print(f"PASS production {args.sha} serves both exact MCP Registry surfaces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
