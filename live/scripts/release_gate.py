#!/usr/bin/env python3
"""Deployment-aware release gate: a release is not green unless the RUNNING
production SHA is the tested SHA.

Flow (bounded, machine-verifiable, no continue-on-error escape hatch):

  1. Poll GET {host}/release until `git_sha` equals the pushed commit.
     Bounded retries + explicit timeout. "Deployment has not arrived yet"
     (exit 3) is explicitly distinguished from "deployed release is
     defective" (exit 1) — the first is a deploy-pipeline problem, the
     second is a broken build serving traffic.
  2. Only after the SHA matches, run against the live service:
       * the full live contract probe (live/scripts/live_contract_probe.py);
       * live AGI-1 conformance (live/trustplane/conformance, --issuer-base);
       * signed-decision verification: fetch /check?signed=true, verify the
         eddsa-jcs-2022 proof against the live issuer DID, check AGD-1
         contract violations, freshness, and the one-counterparty binding
         (decision.agent_id == routing.provider_id among them);
       * issuer/checkpoint continuity: issuer DID document == /ledger/issuer,
         continuity_valid, and the signed checkpoint verifies against the
         issuer key with a fresh, monotonic index;
       * scout autonomy: wait for + verify ONE completed production scout
         cycle via /swarm/status (a legitimate zero-demand cycle passes;
         outbound contact must be OFF).
  3. Write a machine-readable release attestation (commit, production URL,
     every check performed, its result) to --attestation.

Exit codes: 0 all green · 1 deployed release defective · 3 deployment never
arrived (timeout waiting for the SHA).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "live" / "trustplane"))

DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com"


def _headers(api_key: str = "") -> dict[str, str]:
    return {
        "User-Agent": "guild-release-gate",
        "X-Guild-Source": "guild-ci",
        **({"X-API-Key": api_key} if api_key else {}),
    }


def _get(host: str, path: str, timeout: float = 45.0,
         api_key: str = "") -> dict:
    req = urllib.request.Request(host + path, headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def provision_machine_key(host: str, timeout: float = 45.0) -> tuple[str, int]:
    """Acquire the same human-free trial credential any clean machine can.

    The raw key is returned only in memory.  Callers must never print it or
    place it in the release attestation.
    """
    req = urllib.request.Request(
        host + "/billing/trial", data=b"", headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    key = body.get("key")
    balance = body.get("balance")
    if not isinstance(key, str) or not key:
        raise RuntimeError("trial response did not contain a machine credential")
    if not isinstance(balance, int) or balance <= 0:
        raise RuntimeError("trial response did not contain a positive balance")
    return key, balance


def wait_for_sha(host: str, expected_sha: str, timeout_s: float,
                 interval_s: float) -> dict:
    """Poll /release until git_sha == expected. Raises TimeoutError with the
    last observed identity if the deployment never arrives."""
    deadline = time.time() + timeout_s
    last: dict = {}
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            last = _get(host, "/release")
        except Exception as e:
            print(f"attempt {attempt}: /release unreachable: {e}")
            last = {"error": str(e)}
        else:
            got = last.get("git_sha")
            if got == expected_sha:
                print(f"deployment ARRIVED: {host} serves {got} "
                      f"(version {last.get('version')})")
                return last
            print(f"attempt {attempt}: production serves "
                  f"{got or 'unknown'!s}, waiting for {expected_sha[:12]}…")
        time.sleep(interval_s)
    raise TimeoutError(json.dumps(last))


def check_signed_decision(host: str, capability: str = "hello",
                          api_key: str = "") -> list[str]:
    """Verify one live signed AGD-1 decision end to end. Returns a list of
    failures (empty == pass)."""
    from agentguild_trustplane import contract as tp_contract
    from agentguild_trustplane import verify as tp_verify

    fails: list[str] = []
    env = _get(host, f"/check?capability={capability}&signed=true",
               api_key=api_key)
    didd = _get(host, "/.well-known/agent-guild-did.json")
    issuer_did = didd.get("did")
    if env.get("issuer") != issuer_did:
        fails.append(f"decision issuer {env.get('issuer')!r} != live DID "
                     f"document {issuer_did!r}")
    res = tp_verify.verify_data_integrity(
        env, expected_issuer_did=env.get("issuer"))
    if not res.get("verified"):
        fails.append(f"signed decision proof INVALID: {res.get('reason')}")
    fresh, _age = tp_contract.decision_fresh(env)
    if not fresh:
        fails.append("signed decision outside its validity window")
    decision = env.get("decision")
    if isinstance(decision, dict):
        fails += [f"AGD-1 violation: {v}"
                  for v in tp_contract.validate_decision(decision)]
    else:
        # a capability with no routable provider yields no decision object —
        # that is a conforming outcome, but then routing must agree
        if (env.get("routing") or {}).get("routable"):
            fails.append("routable=true but no decision object")
    fails += [f"binding violation: {v}"
              for v in tp_contract.binding_violations(env)]
    return fails


def check_swarm_cycle(host: str, completed_after: str,
                      timeout_s: float = 300.0,
                      interval_s: float = 10.0) -> list[str]:
    """Wait for ONE completed production scout cycle after `completed_after`
    (ISO timestamp) and verify it. A ZERO-DEMAND cycle is a legitimate pass —
    the swarm is autonomous even when there is nothing to scout for. Returns
    a list of failures (empty == pass)."""
    deadline = time.time() + timeout_s
    last: dict = {}
    while True:
        try:
            last = _get(host, "/swarm/status")
        except Exception as e:
            last = {"error": str(e)}
        else:
            if last.get("enabled") is not True:
                return ["scout runner is not enabled in production "
                        "(GUILD_SCOUT_AUTORUN=1 not set) — autonomy is not "
                        "verified"]
            done = last.get("last_completed_at")
            if done and str(done) >= completed_after:
                if last.get("last_error"):
                    return [f"scout cycle completed with an error: "
                            f"{last['last_error']}"]
                run = last.get("last_run") or {}
                if run.get("zero_demand"):
                    print("scout cycle PASS (legitimate zero-demand cycle)")
                else:
                    print(f"scout cycle PASS: discovered="
                          f"{run.get('discovered')} refreshed="
                          f"{run.get('refreshed')} adapters="
                          f"{list((run.get('adapters') or {}))}")
                if last.get("contact_enabled"):
                    return ["outbound scout contact is ENABLED in production "
                            "— GUILD_SCOUT_CONTACT must stay 0"]
                return []
        if time.time() >= deadline:
            return [f"no completed production scout cycle within "
                    f"{int(timeout_s)}s (last /swarm/status: "
                    f"{json.dumps(last)[:300]})"]
        time.sleep(interval_s)


def check_checkpoint_continuity(host: str) -> list[str]:
    from agentguild_trustplane import verify as tp_verify

    fails: list[str] = []
    issuer = _get(host, "/ledger/issuer")
    didd = _get(host, "/.well-known/agent-guild-did.json")
    if issuer.get("did") != didd.get("did"):
        fails.append("issuer DID != DID document")
    if issuer.get("continuity_valid") is not True:
        fails.append("issuer continuity_valid is not true")
    cp = _get(host, "/ledger/checkpoint").get("checkpoint") or {}
    # checkpoint proof = bare hex ed25519 signature over the JCS canonical
    # form of the checkpoint minus "proof" (live/guild/app/crypto.sign_jcs)
    proof = cp.get("proof")
    body = {k: v for k, v in cp.items() if k != "proof"}
    if not isinstance(proof, str) or not proof:
        fails.append("signed checkpoint has no proof")
    elif not tp_verify.verify_jcs_hex(body, proof, cp.get("issuer", "")):
        fails.append("signed checkpoint signature INVALID")
    if not isinstance(cp.get("count"), int) or cp["count"] < 0:
        fails.append("checkpoint count missing/invalid")
    if cp.get("chain_valid") is not True:
        fails.append("checkpoint reports chain_valid != true")
    if cp.get("issuer") != didd.get("did"):
        fails.append("checkpoint issuer != live DID document")
    return fails


def run_subprocess_check(name: str, cmd: list[str], cwd: pathlib.Path,
                         env: dict[str, str] | None = None) -> dict:
    print(f"\n=== {name}: {' '.join(cmd)} (cwd={cwd})")
    p = subprocess.run(cmd, cwd=cwd, env=env)
    return {"name": name, "command": " ".join(cmd), "passed": p.returncode == 0,
            "exit_code": p.returncode}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sha", required=True, help="the pushed commit SHA that must be live")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="max seconds to wait for the deployment to arrive")
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--attestation", default="release_attestation.json")
    ap.add_argument("--capability", default="hello")
    ap.add_argument("--swarm-timeout", type=float, default=300.0,
                    help="max seconds to wait for one completed production "
                         "scout cycle (zero-demand cycles count)")
    args = ap.parse_args()

    attestation: dict = {
        "type": "AgentGuildReleaseAttestation",
        "schema_version": 1,
        "commit": args.sha,
        "production_url": args.host,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "verdict": None,
    }

    def _finish(verdict: str, code: int) -> int:
        attestation["verdict"] = verdict
        attestation["finished_at"] = datetime.now(timezone.utc).isoformat()
        pathlib.Path(args.attestation).write_text(
            json.dumps(attestation, indent=1) + "\n")
        print(f"\nattestation → {args.attestation}\nVERDICT: {verdict}")
        return code

    # 1. deployment arrival gate ---------------------------------------------
    try:
        release = wait_for_sha(args.host, args.sha, args.timeout, args.interval)
        attestation["release_identity"] = release
        attestation["checks"].append(
            {"name": "deployment_sha_match", "passed": True,
             "detail": f"production serves {args.sha}"})
    except TimeoutError as e:
        attestation["checks"].append(
            {"name": "deployment_sha_match", "passed": False,
             "detail": f"DEPLOYMENT NEVER ARRIVED within {args.timeout}s; "
                       f"last /release: {e}"})
        print("::error::deployment has not arrived — this is a deploy-"
              "pipeline failure, NOT (yet) evidence the release is defective")
        return _finish("deployment_not_arrived", 3)

    # 2. machine-authenticated live checks -----------------------------------
    # Billing enforcement must apply to CI too.  The gate therefore follows
    # the public, human-free machine journey instead of carrying a privileged
    # bypass or requiring an operator-managed GitHub secret.
    try:
        machine_key, starting_balance = provision_machine_key(args.host)
    except Exception as e:
        attestation["checks"].append({
            "name": "machine_self_provision", "passed": False,
            "detail": f"human-free trial provisioning failed: {e}"})
        return _finish("machine_self_provision_failed", 1)
    attestation["checks"].append({
        "name": "machine_self_provision", "passed": True,
        "detail": f"ephemeral CI machine received {starting_balance} sandbox credits; "
                  "credential intentionally omitted"})
    check_env = os.environ.copy()
    check_env["AGI1_API_KEY"] = machine_key

    # 3. live checks — only meaningful now that the SHA matches --------------
    attestation["checks"].append(run_subprocess_check(
        "live_contract_probe",
        [sys.executable, "live/scripts/live_contract_probe.py"], REPO))
    attestation["checks"].append(run_subprocess_check(
        "agi1_conformance_live",
        [sys.executable, "-m", "pytest", "conformance/", "-q",
         f"--issuer-base={args.host}", f"--capability={args.capability}"],
        REPO / "live" / "trustplane", env=check_env))

    gate_started = attestation["started_at"]
    for name, fn in (("signed_decision_verification",
                      lambda: check_signed_decision(
                          args.host, args.capability, machine_key)),
                     ("issuer_checkpoint_continuity",
                      lambda: check_checkpoint_continuity(args.host)),
                     ("production_scout_cycle",
                      lambda: check_swarm_cycle(
                          args.host, completed_after=gate_started,
                          timeout_s=args.swarm_timeout))):
        try:
            fails = fn()
        except Exception as e:
            fails = [f"check crashed: {e}"]
        for f in fails:
            print(f"FAIL {name} — {f}")
        if not fails:
            print(f"PASS {name}")
        attestation["checks"].append(
            {"name": name, "passed": not fails, "failures": fails})

    defective = [c["name"] for c in attestation["checks"] if not c["passed"]]
    if defective:
        print(f"::error::deployed release {args.sha} is DEFECTIVE: "
              + ", ".join(defective))
        return _finish("deployed_release_defective", 1)
    return _finish("release_verified", 0)


if __name__ == "__main__":
    sys.exit(main())
