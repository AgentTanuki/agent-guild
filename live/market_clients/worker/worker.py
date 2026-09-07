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
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime, timezone
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
    """Persist identity and pending work before the next remote side effect.

    Atomic replacement preserves the previous journal on a failed write. The
    file contains credentials, so replacements are always owner-readable only.
    A storage error must stop this iteration, never silently drop accepted work.
    """
    destination = Path(STATE_PATH)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", dir=destination.parent, prefix=".worker-", delete=False) as f:
            temporary = f.name
            json.dump(_state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def ensure_identity() -> None:
    if os.path.exists(STATE_PATH):
        # A corrupt/unreadable journal is not permission to create a new
        # identity and abandon the old identity's accepted jobs.
        with open(STATE_PATH) as f:
            _state.update(json.load(f))
    with _client() as c:
        if not _state.get("agent_id"):
            r = c.post("/agents/register",
                       json={"name": NAME, "capabilities": [CAPABILITY],
                             "metadata": {"framework": "fastapi",
                                          "operator": "agent-guild (first-party demo supply)",
                                          "first_party": True,
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


def _json(response) -> dict[str, Any]:
    response.raise_for_status()
    return response.json()


def _receipt_matches(task: dict, receipt: dict) -> bool:
    return (task.get("deliverable_hash") == receipt["deliverable_hash"]
            and task.get("deliverable_url") == receipt["deliverable_url"])


def _deliver_offer(c, offer_id: str) -> None:
    pending = _state["pending"]
    job = pending[offer_id]
    # An earlier failed save may have left an in-memory pending entry. Retry
    # persistence before acting on it, even without a process restart.
    _save_state()
    offer = _json(c.get(f"/offers/{offer_id}"))
    core = offer["core"]
    if (offer["id"] != offer_id or offer["core_hash"] != job["offer_hash"]
            or core["worker_id"] != _state["agent_id"]
            or core["capability"] != CAPABILITY):
        raise ValueError("offer binding changed; refusing execution")
    if offer["status"] not in ("open", "accepted"):
        del pending[offer_id]
        _save_state()
        return
    headers = {"X-API-Key": _state["api_key"]}
    if offer["status"] == "open":
        # The journal already contains this ID. If this response is lost, the
        # next iteration fetches the accepted offer instead of accepting twice.
        _json(c.post(f"/offers/{offer_id}/accept", headers=headers, json={}))
        offer = _json(c.get(f"/offers/{offer_id}"))
    task_id = offer.get("task_id")
    if not task_id or offer["status"] != "accepted":
        raise ValueError("offer has no accepted task")
    if job.get("task_id") and job["task_id"] != task_id:
        raise ValueError("accepted task binding changed")
    job["task_id"] = task_id
    task = _json(c.get(f"/tasks/{task_id}"))
    if (task["id"] != task_id or task["worker_agent_id"] != _state["agent_id"]
            or task["requester_agent_id"] != core["requester_id"]
            or task["task_type"] != CAPABILITY):
        raise ValueError("task does not match the accepted offer")
    receipt = job.get("receipt")
    if receipt is None:
        if datetime.fromisoformat(core["deadline_at"]) <= datetime.now(timezone.utc):
            del pending[offer_id]
            _save_state()
            return
        text = (core.get("terms") or {}).get("input")
        if not isinstance(text, str):
            raise ValueError("text.stats requires a string input")
        payload = json.dumps(text_stats(text), sort_keys=True, separators=(",", ":"))
        receipt = {
            "deliverable_hash": "0x" + hashlib.sha256(payload.encode()).hexdigest(),
            "deliverable_url": "data:application/json;base64," + base64.b64encode(
                payload.encode()).decode(),
            "outcome": "delivered",
        }
        job["receipt"] = receipt
        _save_state()
    if not _receipt_matches(task, receipt):
        if task.get("deliverable_hash") or task["outcome"] != "open":
            raise ValueError("conflicting or terminal task; refusing to overwrite")
        if datetime.fromisoformat(core["deadline_at"]) <= datetime.now(timezone.utc):
            del pending[offer_id]
            _save_state()
            return
        delivered = _json(c.post(f"/tasks/{task_id}/receipt", headers=headers, json=receipt))
        if delivered.get("id") != task_id or not _receipt_matches(delivered, receipt):
            raise ValueError("receipt acknowledgment does not match delivery")
    # Reconcile a lost acknowledgment through the authoritative task. This is
    # evidence that Guild retained the result, not that the buyer accepted it.
    delivered = _state.setdefault("delivered", [])
    if not any(d["task_id"] == task_id for d in delivered):
        delivered.append({"offer_id": offer_id, "task_id": task_id,
                          "deliverable_hash": receipt["deliverable_hash"], "at": time.time()})
    _state["delivered"] = delivered[-100:]
    del pending[offer_id]
    _save_state()


def poll_once(c) -> None:
    pending = _state.setdefault("pending", {})
    # Resume persisted IDs first. Recovery does not depend on an accepted job
    # remaining in the open feed (or fitting within that feed's page size).
    for offer_id in list(pending):
        try:
            _deliver_offer(c, offer_id)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            print(f"pending offer {offer_id}: {type(error).__name__}", flush=True)
    offers = _json(c.get("/offers", params={
        "worker_id": _state["agent_id"], "status": "open"}))
    for offer in offers.get("offers", []):
        core = offer.get("core") or {}
        if (core.get("worker_id") != _state["agent_id"]
                or core.get("capability") != CAPABILITY
                or not isinstance((core.get("terms") or {}).get("input"), str)):
            continue
        offer_id = offer["id"]
        if offer_id in pending:
            continue
        pending[offer_id] = {"offer_hash": offer["core_hash"]}
        try:
            _deliver_offer(c, offer_id)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            print(f"pending offer {offer_id}: {type(error).__name__}", flush=True)


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
                poll_once(c)
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
        else:
            return  # no polling with an unproven or unpersisted identity
        work_loop()
    threading.Thread(target=_init, daemon=True).start()


@app.get("/")
def info():
    return {"role": "market-worker", "framework": "fastapi",
            "git_sha": os.environ.get("RENDER_GIT_COMMIT"),
            "handoff_recovery": "pending-offers-v1",
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
