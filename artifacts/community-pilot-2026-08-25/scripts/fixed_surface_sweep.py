#!/usr/bin/env python3
"""Read-only fixed-thread sweep. Response bodies stay in memory and stdout only."""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse


CLAWSTR_IDS = {
    "launch": "8f16657b51ce01932f3a635dabf8a399eb2fea17cb0355c01cc09ed5cbcf314d",
    "ag_feedback_reply": "bad785a722b8a33212da6a7d70c086d4b9b241c874d7d121c21579964e4a0b65",
}
RELAYS = [
    "wss://relay.ditto.pub",
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
]
FOURCLAW_THREAD = "35376835-852f-4960-867c-fb0de67eb8a0"
FOURCLAW_POST = "97297224-cce3-4746-93ee-7c97af958f01"
AGENTCHAN_BOARD = "apol"
AGENTCHAN_THREAD = "30"
AGENTCHAN_POST = "8321"
AFTER = "2026-08-25T16:38:52Z"

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] != b[1] or a[1] == 0):
        return None
    if a[0] == b[0]:
        slope = (3 * a[0] * a[0]) * pow(2 * a[1], P - 2, P) % P
    else:
        slope = (b[1] - a[1]) * pow((b[0] - a[0]) % P, P - 2, P) % P
    x = (slope * slope - a[0] - b[0]) % P
    return x, (slope * (a[0] - x) - a[1]) % P


def point_mul(k, point=G):
    result = None
    addend = point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def tagged_hash(tag, data):
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


def verify_schnorr(pubkey_hex, message_hex, signature_hex):
    try:
        pubkey = bytes.fromhex(pubkey_hex)
        message = bytes.fromhex(message_hex)
        signature = bytes.fromhex(signature_hex)
        if len(pubkey) != 32 or len(message) != 32 or len(signature) != 64:
            return False
        px = int.from_bytes(pubkey, "big")
        y2 = (pow(px, 3, P) + 7) % P
        py = pow(y2, (P + 1) // 4, P)
        if pow(py, 2, P) != y2:
            return False
        if py & 1:
            py = P - py
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if r >= P or s >= N:
            return False
        e = int.from_bytes(tagged_hash("BIP0340/challenge", signature[:32] + pubkey + message), "big") % N
        neg_ep = point_mul((N - e) % N, (px, py))
        result = point_add(point_mul(s), neg_ep)
        return result is not None and result[1] % 2 == 0 and result[0] == r
    except Exception:
        return False


def validate_event(event):
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    computed = hashlib.sha256(serialized).hexdigest()
    return computed == event.get("id") and verify_schnorr(event["pubkey"], computed, event.get("sig", ""))


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise EOFError("websocket closed")
        result.extend(chunk)
    return bytes(result)


def send_frame(stream, payload, opcode=1):
    payload = payload if isinstance(payload, bytes) else payload.encode()
    first = 0x80 | opcode
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length < 65536:
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def read_frame(stream):
    first, second = read_exact(stream, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(stream, 8))[0]
    mask = read_exact(stream, 4) if second & 0x80 else None
    payload = read_exact(stream, length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def relay_replies(relay, parent_ids):
    parsed = urlparse(relay)
    port = parsed.port or 443
    raw = socket.create_connection((parsed.hostname, port), timeout=12)
    context = ssl.create_default_context()
    stream = context.wrap_socket(raw, server_hostname=parsed.hostname)
    stream.settimeout(12)
    key = base64.b64encode(os.urandom(16)).decode()
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    stream.sendall(request.encode())
    response = bytearray()
    while b"\r\n\r\n" not in response:
        response.extend(stream.recv(4096))
    if not response.startswith(b"HTTP/1.1 101"):
        raise RuntimeError(response.decode(errors="replace")[:200])
    subscription = "ag-fixed-sweep"
    filters = []
    for parent_id in parent_ids:
        filters.extend(({"kinds": [1111], "#e": [parent_id]}, {"kinds": [1111], "#E": [parent_id]}))
    send_frame(stream, json.dumps(["REQ", subscription, *filters], separators=(",", ":")))
    events = []
    while True:
        opcode, payload = read_frame(stream)
        if opcode == 9:
            send_frame(stream, payload, opcode=10)
            continue
        if opcode == 8:
            break
        if opcode != 1:
            continue
        packet = json.loads(payload.decode())
        if packet[0] == "EVENT" and packet[1] == subscription and validate_event(packet[2]):
            event = packet[2]
            events.append({
                "id": event["id"],
                "pubkey": event["pubkey"],
                "created_at": datetime.fromtimestamp(event["created_at"], timezone.utc).isoformat().replace("+00:00", "Z"),
                "content": event["content"],
                "tags": event["tags"],
                "signature_valid": True,
            })
        elif packet[0] == "EOSE" and packet[1] == subscription:
            send_frame(stream, json.dumps(["CLOSE", subscription], separators=(",", ":")))
            break
    stream.close()
    return events


def keychain(service, account):
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("existing keychain credential unavailable")
    return result.stdout.strip()


def get_json(url, bearer=None):
    headers = {"accept": "application/json", "user-agent": "agent-guild-pilot-readonly-sweep/1.0"}
    if bearer:
        headers["authorization"] = "Bearer " + bearer
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def sweep():
    result = {
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "after": AFTER,
        "mode": "read_only_existing_credentials_in_memory",
        "clawstr": {"relays": [], "events": []},
        "4claw": {},
        "agentchan": {},
        "agent_guild_recent": {},
    }
    by_event = {}
    for relay in RELAYS:
        try:
            events = relay_replies(relay, list(CLAWSTR_IDS.values()))
            result["clawstr"]["relays"].append({"relay": relay, "status": "complete", "valid_event_count": len(events)})
            for event in events:
                row = by_event.setdefault(event["id"], {**event, "observed_on_relays": []})
                row["observed_on_relays"].append(relay)
        except Exception as exc:
            result["clawstr"]["relays"].append({"relay": relay, "status": "error", "detail": str(exc)[:180]})
    result["clawstr"]["events"] = sorted(by_event.values(), key=lambda row: (row["created_at"], row["id"]))

    try:
        four_key = keychain("agent-guild-4claw", "api-key")
        payload = get_json(f"https://www.4claw.org/api/v1/threads/{FOURCLAW_THREAD}", four_key)
        replies = payload.get("replies", payload.get("thread", {}).get("replies", []))
        result["4claw"] = {
            "thread_id": FOURCLAW_THREAD,
            "pilot_post_id": FOURCLAW_POST,
            "replies": [{
                "id": item.get("id"),
                "agent": (item.get("agent") or {}).get("name") if isinstance(item.get("agent"), dict) else item.get("agentName", item.get("author")),
                "content": item.get("content"),
                "created_at": item.get("createdAt", item.get("created_at")),
            } for item in replies],
        }
    except Exception as exc:
        result["4claw"] = {"status": "error", "detail": str(exc)[:180]}

    try:
        chan_key = keychain("agent-guild-agentchan", "jwt")
        payload = get_json(
            f"https://agentchan.org/api/v1/boards/{AGENTCHAN_BOARD}/threads/{AGENTCHAN_THREAD}?page=1&limit=100",
            chan_key,
        )
        posts = payload.get("posts", {})
        posts = posts.get("data", []) if isinstance(posts, dict) else posts
        marker = ">>" + AGENTCHAN_POST
        result["agentchan"] = {
            "board": AGENTCHAN_BOARD,
            "thread_id": AGENTCHAN_THREAD,
            "pilot_post_id": AGENTCHAN_POST,
            "quoted_responses": [{
                "id": item.get("id"),
                "anon_id": item.get("anonId"),
                "content": item.get("content"),
                "created_at": item.get("createdAt"),
            } for item in posts if marker in str(item.get("content", ""))],
            "post_count_read": len(posts),
        }
    except Exception as exc:
        result["agentchan"] = {"status": "error", "detail": str(exc)[:180]}

    try:
        payload = get_json("https://agent-guild-5d5r.onrender.com/instrumentation/recent?limit=500&external_only=true")
        events = payload.get("events", [])
        terms = ("passport_offer:clawstr", "passport_offer:4claw", "passport_offer:agentchan", "clawstr-503f64a6")
        matches = [event for event in events if any(term.lower() in json.dumps(event).lower() for term in terms)]
        result["agent_guild_recent"] = {
            "events_scanned": len(events),
            "newest_event_at": events[0].get("at") if events else None,
            "oldest_event_at": events[-1].get("at") if events else None,
            "matching_events": matches,
            "matching_terms": list(terms),
        }
    except Exception as exc:
        result["agent_guild_recent"] = {"status": "error", "detail": str(exc)[:180]}
    return result


if __name__ == "__main__":
    json.dump(sweep(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
