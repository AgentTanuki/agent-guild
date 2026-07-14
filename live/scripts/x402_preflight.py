#!/usr/bin/env python3
"""x402 mainnet PREFLIGHT — read-only, zero-value, secret-silent.

Run this locally (where the CDP API key lives) BEFORE flipping any Render
switches. It proves, without settling anything or printing any secret:

  1. the CDP credentials load and produce a valid request-bound JWT;
  2. the AUTHENTICATED CDP facilitator accepts them
     (GET /platform/v2/x402/supported — a read-only endpoint) and supports
     exact/eip155:8453;
  3. the pinned agent-guild-treasury address matches the code's pin;
  4. the independent confirmation RPC answers (eth_chainId == 8453 and the
     treasury's current USDC balance is readable — public data);
  5. the local mainnet configuration would pass the service's fail-closed
     validation.

Credential sources (in order; NEVER printed, logged or persisted by this
script):
  * CDP_API_KEY_ID / CDP_API_KEY_SECRET environment variables, or
  * --key-file <path> pointing at a CDP key JSON file (the portal download
    format: {"id"|"name": ..., "privateKey": ...}).

Exit 0 = every check passed. Non-zero = the first failing check, with a
non-secret reason. Nothing here moves money: /supported is a metadata read
and the RPC calls are public chain reads.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "live" / "guild"))

PASS, FAIL = "PASS", "FAIL"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def load_credentials(key_file: str | None) -> bool:
    """Load CDP credentials into the process env (never echoed)."""
    if os.environ.get("CDP_API_KEY_ID") and os.environ.get("CDP_API_KEY_SECRET"):
        return True
    if not key_file:
        return False
    try:
        data = json.loads(pathlib.Path(key_file).expanduser().read_text())
    except Exception as e:
        check("credential file readable", False, f"{type(e).__name__}")
        return False
    key_id = data.get("id") or data.get("name") or ""
    secret = data.get("privateKey") or data.get("private_key") or ""
    if not (key_id and secret):
        check("credential file shape", False,
              "expected CDP portal JSON with id/name + privateKey")
        return False
    os.environ["CDP_API_KEY_ID"] = key_id
    os.environ["CDP_API_KEY_SECRET"] = secret
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key-file", default=None,
                    help="CDP key JSON (portal download format); "
                         "alternative to CDP_API_KEY_ID/CDP_API_KEY_SECRET env")
    ap.add_argument("--rpc", default=None,
                    help="override GUILD_X402_BASE_RPC for the RPC checks")
    args = ap.parse_args()
    if args.rpc:
        os.environ["GUILD_X402_BASE_RPC"] = args.rpc

    import httpx

    from app import x402, x402_cdp, x402_confirm

    # --- 1. credentials load + JWT self-check (offline) ---------------------
    if not load_credentials(args.key_file):
        check("CDP credentials available", False,
              "set CDP_API_KEY_ID/CDP_API_KEY_SECRET or pass --key-file")
        return _finish()
    try:
        headers = x402_cdp.create_cdp_headers()
        ok = all(headers[e]["Authorization"].startswith("Bearer ")
                 for e in ("verify", "settle", "supported"))
        check("CDP JWT generation (EdDSA/ES256, request-bound)", ok)
    except Exception as e:
        check("CDP JWT generation", False, type(e).__name__)
        return _finish()

    # --- 2. authenticated facilitator accepts the key (read-only) -----------
    try:
        r = httpx.get(f"{x402_cdp.CDP_FACILITATOR_URL}/supported",
                      headers=headers["supported"], timeout=30)
        if r.status_code in (401, 403):
            check("CDP facilitator authentication", False,
                  f"HTTP {r.status_code} — key rejected (not printed)")
        else:
            r.raise_for_status()
            kinds = r.json().get("kinds", [])
            base_exact = any(k.get("network") == "eip155:8453"
                             and k.get("scheme") == "exact" for k in kinds)
            check("CDP facilitator authentication (GET /supported)", True)
            check("facilitator supports exact @ eip155:8453", base_exact,
                  f"{len(kinds)} kinds advertised")
    except Exception as e:
        check("CDP facilitator reachable", False, type(e).__name__)

    # --- 3. treasury pin ------------------------------------------------------
    configured = os.environ.get("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    check("recipient == pinned agent-guild-treasury",
          configured.lower() == x402.MAINNET_TREASURY.lower(),
          x402.MAINNET_TREASURY)

    # --- 4. independent confirmation RPC (public reads) ----------------------
    rpc = x402_confirm.rpc_url()
    try:
        cid = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                    "method": "eth_chainId", "params": []},
                         timeout=15).json().get("result")
        check("confirmation RPC answers eth_chainId", cid is not None, rpc)
        check("confirmation RPC chain is Base mainnet (8453)",
              cid is not None and int(cid, 16) == 8453,
              f"chainId={int(cid, 16) if cid else 'n/a'}")
        # treasury USDC balance — public data, proves the read path end-to-end
        usdc = x402.USDC_BY_NETWORK["eip155:8453"]
        data = "0x70a08231" + x402.MAINNET_TREASURY[2:].lower().rjust(64, "0")
        bal = httpx.post(rpc, json={
            "jsonrpc": "2.0", "id": 2, "method": "eth_call",
            "params": [{"to": usdc, "data": data}, "latest"]},
            timeout=15).json().get("result")
        if bal is not None:
            atomic = int(bal, 16)
            check("treasury USDC balance readable", True,
                  f"{atomic} atomic units (${atomic / 1e6:.6f}) — public info")
        else:
            check("treasury USDC balance readable", False)
    except Exception as e:
        check("confirmation RPC reachable", False, f"{type(e).__name__} — "
              "independent confirmation would FAIL CLOSED")

    # --- 5. the service's own fail-closed validation -------------------------
    os.environ.setdefault("GUILD_X402_ENABLED", "1")
    os.environ.setdefault("GUILD_X402_PAY_TO", configured)
    os.environ["GUILD_X402_NETWORK"] = "eip155:8453"
    errs = x402.config_errors()
    check("service mainnet config validation (fail-closed)", not errs,
          "; ".join(errs) if errs else "config_valid")

    return _finish()


def _finish() -> int:
    print("\n" + ("PREFLIGHT CLEAN — proceed to the next runbook stage"
                  if not _failures else
                  "PREFLIGHT FAILED: " + ", ".join(_failures)))
    print("note: this preflight is read-only and settles nothing; a clean "
          "preflight is NOT a payment and NOT revenue.")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
