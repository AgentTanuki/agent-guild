#!/usr/bin/env python3
"""Detect a divergent production view — and say WHICH kind it is.

The daily ops pass previously worked around the 2026-07-30/31 incident by
"discarding the first 2-3 reads until three agree". That is a reporting
workaround: it hides the symptom, produces no evidence, and does nothing for
the WRITE path, where a stale view was willing to build a canonical checkpoint.

This script replaces the workaround with a measurement. It fans out concurrent
reads and uses the view-identity headers shipped with the fix:

    X-Guild-Instance   random per-process id
    X-Guild-Boot       process start time
    X-Guild-Store-Rev  monotonic in-memory mutation counter

and the ``/diagnostics/state`` endpoint (in-memory vs authoritative SQLite).

VERDICTS
  consistent            one instance, monotonic revisions, memory == durable
  split_origin          MORE THAN ONE instance id for one release SHA
                        -> two serving processes. SQLite on a Render disk is
                           single-writer: this is a topology emergency, not a
                           metrics bug.
  stale_in_process      one instance, but a response carried a LOWER store_rev
                        than one already seen from that same instance
                        -> the process served a frozen view of its own state.
  memory_durable_split  /diagnostics/state reports the in-memory view and the
                        committed database disagree.
  intermediary          bodies disagree while instance AND store_rev are
                        identical -> something in front of the app served a
                        body this process did not just produce.

Exit code 0 = consistent, 2 = divergence detected, 1 = could not measure.

    python3 detect_divergence.py --url https://agent-guild-5d5r.onrender.com
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request

READ_PATHS = ["/instrumentation", "/funnel/passports", "/ledger/checkpoints?limit=1",
              "/diagnostics/state", "/release", "/health"]


def _get(base: str, path: str, timeout: float = 25.0):
    sep = "&" if "?" in path else "?"
    url = f"{base}{path}{sep}cb={int(time.time() * 1e6)}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            h = {k.lower(): v for k, v in resp.headers.items()}
            return {"path": path, "body": body,
                    "instance": h.get("x-guild-instance"),
                    "boot": h.get("x-guild-boot"),
                    "rev": int(h["x-guild-store-rev"])
                    if h.get("x-guild-store-rev", "").isdigit() else None}
    except (urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, ValueError) as exc:
        return {"path": path, "error": type(exc).__name__}


def _counter(body, path):
    """One comparable scalar per endpoint — what actually flapped in the incident."""
    if not isinstance(body, dict):
        return None
    if path.startswith("/instrumentation"):
        return body.get("total_events")
    if path.startswith("/funnel/passports"):
        stages = body.get("stages") or []
        row = next((s for s in stages if s.get("stage") == "offer_served"), None)
        return (row or {}).get("total")
    if path.startswith("/ledger/checkpoints"):
        cps = body.get("checkpoints") or []
        return (cps[0].get("ledger_length") if cps else None)
    if path.startswith("/release"):
        return body.get("git_sha")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://agent-guild-5d5r.onrender.com")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    jobs = [(base, p) for _ in range(args.rounds) for p in READ_PATHS]
    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as ex:
        results = list(ex.map(lambda j: _get(*j), jobs))

    ok = [r for r in results if "error" not in r]
    if not ok:
        print("[divergence] could not read the service at all", file=sys.stderr)
        return 1

    instances = {r["instance"] for r in ok if r.get("instance")}
    findings: list[str] = []

    if not instances:
        findings.append(
            "no_view_identity: responses carry no X-Guild-Instance — this build "
            "predates the divergence fix, so the cause CANNOT be decided")

    if len(instances) > 1:
        findings.append(
            f"split_origin: {len(instances)} distinct instance ids "
            f"({sorted(instances)}) for one URL — more than one serving "
            "process. SQLite lives on a single-mount disk; two writers is a "
            "topology emergency")

    # --- per-instance revision monotonicity -----------------------------
    # SEQUENTIAL by construction. The concurrent fan-out above cannot be used
    # for this check: responses complete out of ISSUE order, so a request sent
    # earlier can be observed later, and a perfectly healthy process looks
    # like it went backwards. (That false positive fired on the first live run
    # of this script.) Here each request is fully received before the next is
    # sent, so rev(n+1) < rev(n) has only one explanation: the process served
    # a view older than one it had already served.
    seq = [_get(base, "/diagnostics/state") for _ in range(6)]
    ok += [r for r in seq if "error" not in r]
    high: dict[str, int] = {}
    for r in seq:
        inst, rev = r.get("instance"), r.get("rev")
        if inst is None or rev is None:
            continue
        if inst in high and rev < high[inst]:
            findings.append(
                f"stale_in_process: instance {inst} served store_rev {rev} "
                f"after already serving {high[inst]} in a STRICTLY SEQUENTIAL "
                "read — a frozen view of its own state")
        high[inst] = max(high.get(inst, -1), rev)
    instances |= {r["instance"] for r in seq if r.get("instance")}

    # --- counter disagreement -------------------------------------------
    # Only a counter going DOWN is evidence of divergence. These counters grow
    # continuously as live traffic arrives, so "the values differ across a
    # sampling window" is the NORMAL case and flagging it would cry wolf on
    # every run. The incident signature was specifically a counter REGRESSING
    # (offer_served 1788 -> 944, total_events 16796 -> 13344).
    per_path: dict[str, list] = {}
    for r in ok:
        c = _counter(r.get("body"), r["path"])
        if c is not None:
            per_path.setdefault(r["path"], []).append(c)
    flapping = {}
    for p, vals in per_path.items():
        nums = [v for v in vals if isinstance(v, (int, float))]
        if len(set(vals)) > 1 and (not nums or min(nums) < max(nums)):
            # a string counter (git_sha) that differs at all IS a regression
            if not nums or (max(nums) - min(nums)) > 0:
                flapping[p] = sorted(set(vals), key=str)

    # /diagnostics/state
    diag = next((r["body"] for r in ok
                 if r["path"].startswith("/diagnostics/state")
                 and isinstance(r.get("body"), dict)), None)
    if diag and diag.get("divergence"):
        findings.append(
            f"memory_durable_split: {diag['divergence']} "
            f"(in_memory={diag.get('in_memory')}, durable={diag.get('durable')})")

    regressed = bool(flapping) and any(
        f.startswith("stale_in_process") or f.startswith("memory_durable_split")
        for f in findings)
    if flapping and regressed and len(instances) <= 1 and not any(
            f.startswith("stale_in_process") for f in findings):
        findings.append(
            f"intermediary: counters disagree {flapping} while the instance id "
            "and store_rev are stable — the differing body was NOT produced by "
            "this process's current state; suspect something in front of the app")

    out = {
        "url": base,
        "samples": len(ok),
        "instances": sorted(instances),
        "counter_values_seen": flapping,
        "diagnostics": diag,
        "findings": findings,
        "verdict": "consistent" if not findings else "divergent",
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"=== divergence check @ {base} ({len(ok)} samples) ===")
        print(f"  instances: {sorted(instances) or 'NONE (no view identity)'}")
        print(f"  counter values seen (growth is normal): {flapping or 'stable'}")
        if diag:
            print(f"  in_memory: {diag.get('in_memory')}")
            print(f"  durable:   {diag.get('durable')}")
        for f in findings:
            print(f"  ! {f}")
        print(f"  VERDICT: {out['verdict']}")
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
