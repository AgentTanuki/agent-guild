#!/usr/bin/env python3
"""Publish the canonical checkpoint AND PROVE the response was live.

WHY THIS SCRIPT EXISTS (phantom incident 2026-07-31 -> 2026-08-02)
------------------------------------------------------------------
For three days the daily operations pass reported that
``POST /ledger/checkpoint/publish`` returned a STALE checkpoint — index 14 /
ledger_length 834 — while the feed head was 17 / 840. It was escalated as a
write-path integrity incident. Substantial hardening was aimed at it.

Production never did that. The ops pass ran, in effect::

    curl -o /tmp/pub.json -X POST .../ledger/checkpoint/publish
    python3 -c "import json; print(json.load(open('/tmp/pub.json')))"

``/tmp`` was a SHARED, PERSISTENT path holding ``pub.json`` from an ops run four
days earlier, owned by another uid. The write failed (``curl: (23)``). curl's
exit status was discarded because only ``-w '%{http_code}'`` was inspected — and
the HTTP status was a perfectly true 200. The next line then parsed the OLD FILE
and reported its contents as the live response. Re-running the same publish with
a guaranteed-fresh output path returns 17 / 840, idempotently, every time.

So the defect was never in the write path. It was here, in the verification
path: a pipeline that could report a four-day-old file as a live production
response, and a report that could not tell the difference. This script closes
that class of error structurally rather than by remembering to be careful:

  1. THE BODY IS NEVER ROUTED THROUGH A FILE. It is read from the response
     object. There is no path for a stale artefact to be substituted.
  2. TRANSPORT FAILURES ARE FATAL, not cosmetic. Any non-2xx, timeout or
     malformed body exits non-zero.
  3. THE RESPONSE MUST IDENTIFY ITSELF. A publish body without the `view`
     block (instance / observed_at / floor) is REFUSED — a replayed or cached
     body cannot mint one.
  4. `observed_at` MUST MOVE between the two probes. A body that is
     byte-identical across two calls including its timestamp is a cache or a
     recording, not two live commitments.
  5. THE CLAIM IS CROSS-CHECKED against two independent surfaces —
     ``/ledger/checkpoints`` and ``/health.canonical_state`` — and against the
     operator's expected floor. A publish that claims an index below any of
     them exits non-zero and prints the disagreement.

Exit codes: 0 verified · 1 refused/unverified · 2 usage or transport error.

Usage::

    python3 live/scripts/ops_publish_checkpoint.py \
        --url https://agent-guild-5d5r.onrender.com \
        --token-file live/secrets/guild_admin_token \
        --min-index 17 --min-ledger-length 840

The token is read from a file and NEVER echoed, logged or included in output.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 60


class Unverified(RuntimeError):
    """The publish could not be PROVEN live and consistent. Never a warning."""


def _request(url: str, *, token: str | None = None,
             method: str = "GET") -> dict:
    """One HTTP call. Returns the parsed body or raises — never returns a
    partially-understood result, and never reads from disk."""
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:            # 4xx/5xx carry a body
        body = exc.read().decode("utf-8", "replace")[:600]
        raise Unverified(
            f"{method} {_redact(url)} -> HTTP {exc.code}: {body}") from exc
    except Exception as exc:                          # noqa: BLE001
        raise Unverified(
            f"{method} {_redact(url)} failed: {type(exc).__name__}") from exc
    if status // 100 != 2:
        raise Unverified(f"{method} {_redact(url)} -> HTTP {status}")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:                          # noqa: BLE001
        raise Unverified(
            f"{method} {_redact(url)} returned a body that is not JSON "
            f"({type(exc).__name__}, {len(raw)} bytes)") from exc


def _redact(url: str) -> str:
    """URLs are printed on failure; strip any query string in case a caller
    ever puts something sensitive in one."""
    return url.split("?", 1)[0]


def _entry_position(entry: dict) -> tuple[int, int]:
    """(index, ledger_length) of a feed entry, with NO int() coercion of junk.

    A checkpoint index is an ordinal third parties cite. `int()` happily turns
    True, 2.7 and "3" into positions, so a malformed entry could present itself
    as a usable one; this refuses instead."""
    idx = entry.get("index")
    length = entry.get("ledger_length")
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
        raise Unverified(f"checkpoint index is not a valid ordinal: {idx!r}")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise Unverified(f"ledger_length is not a valid count: {length!r}")
    return idx, length


def publish_verified(base: str, token: str, *, min_index: int,
                     min_length: int) -> dict:
    """Publish, then prove the response was live and is not behind canonical."""
    base = base.rstrip("/")
    findings: list[str] = []

    first = _request(f"{base}/ledger/checkpoint/publish", token=token,
                     method="POST")
    second = _request(f"{base}/ledger/checkpoint/publish", token=token,
                      method="POST")

    for label, body in (("first", first), ("second", second)):
        if body.get("status") != "published":
            raise Unverified(
                f"{label} publish did not report success: "
                f"status={body.get('status')!r}")
        if not isinstance(body.get("checkpoint"), dict):
            raise Unverified(f"{label} publish carried no checkpoint object")
        # (3) SELF-IDENTIFICATION. A stale file or a cached body cannot
        # manufacture a view block naming the process that served it.
        if not isinstance(body.get("view"), dict):
            raise Unverified(
                f"{label} publish response carries no `view` block. Either the "
                "service predates the 2026-08-02 diagnostics, or this body did "
                "not come from the service at all. Refusing to report a "
                "publish that cannot identify its own origin.")

    # (4) LIVENESS. Two live commitments always differ in observed_at.
    o1 = first["view"].get("observed_at")
    o2 = second["view"].get("observed_at")
    if not o1 or not o2:
        raise Unverified("publish `view` block carries no observed_at")
    if o1 == o2:
        raise Unverified(
            "two consecutive publish responses carry the SAME observed_at "
            f"({o1}). That is a cached, recorded or replayed body, not two "
            "live commitments.")
    if first["view"].get("instance") != second["view"].get("instance"):
        findings.append(
            "instance changed between the two publishes "
            f"({first['view'].get('instance')} -> "
            f"{second['view'].get('instance')}): more than one serving process, "
            "or a restart mid-run")

    entry = second["checkpoint"]
    idx, length = _entry_position(entry)

    # (5) CROSS-CHECK against two surfaces that are not the publish response.
    feed = _request(f"{base}/ledger/checkpoints?limit=1")
    cps = feed.get("checkpoints") or []
    if not cps:
        raise Unverified("the published feed is empty; nothing to reconcile")
    feed_idx, feed_len = _entry_position(cps[0])
    if (idx, length) != (feed_idx, feed_len):
        findings.append(
            f"publish returned {idx}/{length} but the feed head is "
            f"{feed_idx}/{feed_len}")

    health = _request(f"{base}/health")
    canon = health.get("canonical_state") or {}
    for key, got, name in (
            ("served_checkpoint_index", idx, "checkpoint index"),
            ("floor_checkpoint_index", idx, "checkpoint index"),
            ("served_ledger_length", length, "ledger length"),
            ("floor_ledger_length", length, "ledger length"),
    ):
        want = canon.get(key)
        if isinstance(want, int) and got < want:
            findings.append(
                f"publish returned a {name} ({got}) BELOW health.{key} ({want})")
    if canon.get("ok") is False:
        findings.append(
            f"health reports a degraded canonical state: "
            f"{json.dumps(canon)[:300]}")

    # Operator expectation — the floor the ops pass knows was already published.
    if idx < min_index or length < min_length:
        findings.append(
            f"publish returned {idx}/{length}, BELOW the expected floor "
            f"{min_index}/{min_length}")

    if findings:
        raise Unverified("; ".join(findings))
    return {
        "verified": True,
        "checkpoint_index": idx,
        "ledger_length": length,
        "head_hash": (entry.get("checkpoint") or {}).get("head_hash"),
        "chain_valid": (entry.get("checkpoint") or {}).get("chain_valid"),
        "view": second["view"],
        "reconciled_with": ["/ledger/checkpoints", "/health.canonical_state"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="base URL of the service")
    ap.add_argument("--token-file", required=True,
                    help="path to the admin token (never echoed)")
    ap.add_argument("--min-index", type=int, default=0,
                    help="lowest checkpoint index the operator knows is "
                         "already published")
    ap.add_argument("--min-ledger-length", type=int, default=0,
                    help="lowest ledger length the operator knows is already "
                         "published")
    args = ap.parse_args(argv)

    try:
        with open(args.token_file) as f:
            token = f.read().strip()
    except OSError as exc:
        print(f"UNVERIFIED: admin token unreadable ({type(exc).__name__}). The "
              "Render GUILD_ADMIN_TOKEN is the source of truth.",
              file=sys.stderr)
        return 2
    if not token:
        print("UNVERIFIED: admin token file is empty.", file=sys.stderr)
        return 2

    try:
        out = publish_verified(args.url, token,
                               min_index=args.min_index,
                               min_length=args.min_ledger_length)
    except Unverified as exc:
        print(f"UNVERIFIED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    print(f"VERIFIED: checkpoint {out['checkpoint_index']} / "
          f"{out['ledger_length']} records, live and reconciled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
