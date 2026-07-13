"""Autonomous MARKET WORKER — FastAPI/uvicorn framework.

One half of the machine-only market loop (the buyer is a Node.js client that
must find this worker exclusively through the Guild's public interfaces).

What it does, with zero human involvement:
  1. registers with the Guild (custodial identity) and declares its PUBLIC
     A2A endpoint with a liveness probe (making itself ROUTABLE via /check)
  2. completes the proving rung (proof_of_conduct)
  3. serves a real deterministic capability — `text.stats` — over A2A
     (agent card + JSON-RPC message/send)
  4. polls the Guild's public offer feed for SIGNED offers addressed to it,
     accepts (countersigning the offer hash), performs the work, and submits a
     worker-authenticated, content-addressed delivery receipt whose deliverable
     travels as a data: URI

First-party honesty: this worker is GUILD-OPERATED demo supply. It never
claims to be external; it sends the first-party header when a token is
configured, and its purpose is to prove the loop's mechanics, not adoption.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request

GUILD = os.environ.get("GUILD_URL", "https://agent-guild-5d5r.onrender.com").rstrip("/")
PUBLIC_URL = os.environ.get("WORKER_PUBLIC_URL", "").rstrip("/")
STATE_PATH = os.environ.get("WORKER_STATE", "/tmp/market_worker_state.json")
NAME = os.environ.get("WORKER_NAME", "TanukiTextStats")
CAPABILITY = "text.stats"
POLL_S = int(os.environ.get("WORKER_POLL_S", "10"))

app = FastAPI(title="market-worker (FastAPI)")
_state: dict[str, Any] = {}


def _fp_headers() -> dict[str, str]:
    # shared helper: Guild-operated traffic ALWAYS tags first-party (the old
    # copy returned {} without a token and silently counted as external)
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from _firstparty import firstparty_headers
    return firstparty_headers(role="test")


def _client() -> httpx.Client:
    return httpx.Client(base_url=GUILD, timeout=30.0,
                        headers={"User-Agent": "market-worker-fastapi/1",
                                 **_fp_headers()})


def text_stats(text: str) -> dict[str, Any]:
    words = text.split()
    return {
        "capability": CAPABILITY,
        "chars": len(text),
        "words": len(words),
        "lines": text.count("\n") + (1 if text else 0),
        "unique_words": len({w.lower().strip('.,!?;:"()[]') for w in words} - {""}),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _save_state():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(_state, f)
    except OSError:
        pass


def ensure_identity() -> None:
    if os.path.exists(STATE_PATH):
        try:
            _state.update(json.load(open(STATE_PATH)))
        except (OSError, ValueError):
            pass
    with _client() as c:
        if not _state.get("agent_id"):
            r = c.post("/agents/register",
                       json={"name": NAME, "capabilities": [CAPABILITY],
                             "metadata": {"framework": "fastapi",
                                          "operator": "agent-guild (first-party demo supply)",
                                          "price_per_call": 5}})
            r.raise_for_status()
            body = r.json()
            _state.update({"agent_id": body["id"], "api_key": body["api_key"],
                           "did": body["did"]})
            _save_state()
        h = {"X-API-Key": _state["api_key"]}
        if PUBLIC_URL:
            c.post(f"/agents/{_state['agent_id']}/endpoint",
                   headers=h, json={"endpoint": PUBLIC_URL, "verify": True})
        if not _state.get("proven"):
            c.post(f"/agents/{_state['agent_id']}/prove", headers=h)
            pv = c.post(f"/agents/{_state['agent_id']}/prove/verify",
                        headers=h, json={})
            if pv.status_code == 200:
                _state["proven"] = True
                _save_state()


def work_loop() -> None:
    last_verify = 0.0
    while True:
        try:
            with _client() as c:
                h = {"X-API-Key": _state["api_key"]}
                # Keep the endpoint FRESH: re-declare with a liveness probe every
                # ~2 min so /check reports the worker as verified+reachable
                # (routable) whenever it is actually up. Free-plan spindown makes
                # a one-time boot probe go stale.
                if PUBLIC_URL and time.time() - last_verify > 120:
                    try:
                        c.post(f"/agents/{_state['agent_id']}/endpoint",
                               headers=h,
                               json={"endpoint": PUBLIC_URL, "verify": True})
                        last_verify = time.time()
                    except Exception:
                        pass
                offers = c.get("/offers", params={
                    "worker_id": _state["agent_id"], "status": "open"}).json()
                for offer in offers.get("offers", []):
                    oid = offer["id"]
                    acc = c.post(f"/offers/{oid}/accept", headers=h, json={})
                    if acc.status_code != 200:
                        continue
                    task_id = acc.json()["task_id"]
                    text = str((offer["core"].get("terms") or {}).get("input", ""))
                    result = text_stats(text)
                    payload = json.dumps(result, sort_keys=True,
                                         separators=(",", ":"))
                    dhash = "0x" + hashlib.sha256(payload.encode()).hexdigest()
                    durl = ("data:application/json;base64,"
                            + base64.b64encode(payload.encode()).decode())
                    c.post(f"/tasks/{task_id}/receipt", headers=h,
                           json={"deliverable_hash": dhash,
                                 "deliverable_url": durl,
                                 "outcome": "delivered"})
                    _state.setdefault("delivered", []).append(
                        {"offer_id": oid, "task_id": task_id,
                         "deliverable_hash": dhash, "at": time.time()})
                    _save_state()
        except Exception as e:  # keep polling forever; log to stdout
            print(f"work_loop error: {e}", flush=True)
        time.sleep(POLL_S)


@app.on_event("startup")
def _boot():
    def _init():
        for attempt in range(10):
            try:
                ensure_identity()
                break
            except Exception as e:
                print(f"identity bootstrap retry {attempt}: {e}", flush=True)
                time.sleep(10)
        work_loop()
    threading.Thread(target=_init, daemon=True).start()


@app.get("/")
def info():
    return {"role": "market-worker", "framework": "fastapi",
            "capability": CAPABILITY, "agent_id": _state.get("agent_id"),
            "guild": GUILD, "a2a": PUBLIC_URL,
            "first_party": True,
            "note": "Guild-operated demo supply; never counted as external."}


@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "protocolVersion": "0.2.6",
        "name": NAME,
        "description": "Deterministic text statistics worker (Agent Guild demo supply).",
        "url": PUBLIC_URL or "",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": CAPABILITY, "name": "Text statistics",
            "description": "chars/words/lines/unique-words + sha256 of a text",
            "tags": ["text", "deterministic"],
        }],
    }


@app.post("/a2a")
@app.post("/")
async def a2a(request: Request):
    body = await request.json()
    id_ = body.get("id")
    try:
        parts = body["params"]["message"]["parts"]
        text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text")
    except (KeyError, TypeError):
        return {"jsonrpc": "2.0", "id": id_,
                "error": {"code": -32602, "message": "expected message/send"}}
    result = text_stats(text)
    return {"jsonrpc": "2.0", "id": id_, "result": {
        "kind": "message", "role": "agent",
        "messageId": f"msg-{id_}",
        "parts": [{"kind": "text", "text": json.dumps(result)}]}}
