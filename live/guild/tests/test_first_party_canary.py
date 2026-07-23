"""First-party mainnet canary: safety guards, discovery, idempotent state.

The canary is DRY-RUN by default and never runs execution in tests. These
cover the parts that keep --execute safe: the hard 0.01 USDC cap, refusal of
any unexpected recipient/network/asset/amount/resource, signed-offer
verification, precondition checks, secret-silent key loading, and the
pay-before-state-is-impossible one-shot file.
"""
import importlib.util
import base64
import json
import pathlib
import stat
import types

import pytest

from app import payments, x402
from app import x402_artifacts as artifacts

REPO = pathlib.Path(__file__).resolve().parents[3]
CANARY = REPO / "live" / "scripts" / "first_party_canary.py"
TREASURY = x402.MAINNET_TREASURY
MAINNET_USDC = x402.USDC_BY_NETWORK["eip155:8453"]


def _load_canary():
    spec = importlib.util.spec_from_file_location("first_party_canary", CANARY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


canary = _load_canary()


@pytest.fixture(autouse=True)
def _mainnet_env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", TREASURY)
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv("GUILD_X402_ASSET", MAINNET_USDC)
    monkeypatch.setenv("CDP_API_KEY_ID", "id")
    from tests.test_x402_cdp_settlement import FAKE_SECRET
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.setenv("GUILD_X402_FACILITATOR",
                       "https://api.cdp.coinbase.com/platform/v2/x402")
    monkeypatch.setenv("GUILD_X402_BASE_RPC", "https://mainnet.base.org")
    yield


def _resource():
    return f"https://guild.example/check?capability={canary.CANARY_CAPABILITY}"


class _DidDocHTTP:
    """Fake http that serves the trusted origin's did:web document — what the
    canary fetches to resolve the offer kid."""

    def __init__(self, doc):
        self._doc = doc

    def get(self, url, timeout=0):
        class R:
            def __init__(self, p):
                self._p = p

            def json(self):
                return self._p
        if url.endswith("/.well-known/did.json"):
            return R(self._doc)
        return R({})


def _did_http():
    from app.state import store
    return _DidDocHTTP(artifacts.did_web_document(
        store.guild_identity(), origin="https://guild.example"))


def _challenge(monkeypatch, amount_credits=10):
    monkeypatch.setenv("GUILD_PUBLIC_HOST", "https://guild.example")
    preq = payments.check_request(canary.CANARY_CAPABILITY)
    model = payments.challenge_model(preq)
    return model.model_dump(by_alias=True, exclude_none=True), preq


# --- lifetime cap + binding refusals -----------------------------------------

def test_challenge_within_cap_is_accepted(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    req, canonical = canary.verify_challenge(challenge, preq.resource_url, facts, http=_did_http())
    assert int(req["amount"]) == 10 * 1000            # 0.01 USDC exactly
    assert canonical == preq.resource_url


def test_amount_over_lifetime_cap_is_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    # tamper the quoted amount above 0.01 USDC (10001 atomic units)
    challenge["accepts"][0]["amount"] = "20000"        # 0.02 USDC
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    with pytest.raises(canary.Refuse, match="lifetime cap"):
        canary.verify_challenge(challenge, preq.resource_url, facts, http=_did_http())


def test_wrong_recipient_is_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    challenge["accepts"][0]["payTo"] = "0x" + "99" * 20
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    with pytest.raises(canary.Refuse, match="payTo"):
        canary.verify_challenge(challenge, preq.resource_url, facts, http=_did_http())


def test_wrong_asset_and_network_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    bad_asset = json.loads(json.dumps(challenge))
    bad_asset["accepts"][0]["asset"] = x402.USDC_BY_NETWORK["eip155:84532"]
    with pytest.raises(canary.Refuse, match="asset"):
        canary.verify_challenge(bad_asset, preq.resource_url, facts, http=_did_http())
    bad_net = json.loads(json.dumps(challenge))
    bad_net["accepts"][0]["network"] = "eip155:84532"
    with pytest.raises(canary.Refuse, match="network"):
        canary.verify_challenge(bad_net, preq.resource_url, facts, http=_did_http())


def test_wrong_resource_is_refused(monkeypatch):
    # a challenge whose quoted resource is NOT the canonical /check?capability=
    # form on the trusted origin (wrong path / wrong capability / hostile host)
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    for bad in ("https://evil.example/check?capability=code-review",
                "https://guild.example/search?capability=code-review",
                "https://guild.example/check?capability=other"):
        c = json.loads(json.dumps(challenge))
        c["resource"]["url"] = bad
        with pytest.raises(canary.Refuse, match="resource"):
            canary.verify_challenge(c, preq.resource_url, facts, http=_did_http())


# --- signed offer verification -----------------------------------------------

def test_signed_offer_must_verify(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    # tamper the offer JWS payload → verification fails → refuse
    offers = challenge["extensions"]["offer-receipt"]["info"]["offers"]
    import base64
    parts = offers[0]["signature"].split(".")
    body = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    body["amount"] = "1"
    parts[1] = base64.urlsafe_b64encode(
        json.dumps(body).encode()).rstrip(b"=").decode()
    offers[0]["signature"] = ".".join(parts)
    with pytest.raises(canary.Refuse, match="signed offer"):
        canary.verify_challenge(challenge, preq.resource_url, facts, http=_did_http())


# --- preconditions -----------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeHTTP:
    def __init__(self, readiness, chain_id="0x2105", release_sha="abc"):
        self._readiness = readiness
        self._chain = chain_id
        self._sha = release_sha

    def get(self, url, timeout=0):
        if url.endswith("/x402/readiness"):
            return _FakeResp(self._readiness)
        if url.endswith("/release"):
            # REAL /release schema (app/main.py::release). The field is
            # `git_sha`; a hand-mocked `{"sha": ...}` here is exactly what
            # concealed the broken reader until 2026-07-22 — never regress
            # this back to an invented shape.
            return _FakeResp({"service": "Agent Guild", "version": "0.0.0",
                              "git_sha": self._sha,
                              "deployed_at": "2026-01-01T00:00:00+00:00",
                              "build": {}})
        return _FakeResp({})

    def post(self, url, json=None, timeout=0):
        return _FakeResp({"result": self._chain})


def _good_readiness():
    return {"enabled": True, "config_valid": True, "network": "eip155:8453",
            "mainnet": True, "recipient": TREASURY,
            "recipient_is_pinned_treasury": True, "asset": MAINNET_USDC,
            "facilitator_authenticated": True,
            "facilitator_host": "api.cdp.coinbase.com"}


def test_preconditions_pass_on_healthy_mainnet():
    http = _FakeHTTP(_good_readiness())
    facts = canary.verify_preconditions(http, "https://x", "abc")
    assert facts["chain_id"] == 8453 and facts["recipient"] == TREASURY


def test_preconditions_refuse_wrong_chain_id():
    http = _FakeHTTP(_good_readiness(), chain_id="0x1")   # Ethereum, not Base
    with pytest.raises(canary.Refuse, match="chain id"):
        canary.verify_preconditions(http, "https://x", None)


def test_preconditions_refuse_unpinned_recipient():
    r = _good_readiness()
    r["recipient"] = "0x" + "88" * 20
    r["recipient_is_pinned_treasury"] = False
    with pytest.raises(canary.Refuse, match="pinned treasury"):
        canary.verify_preconditions(_FakeHTTP(r), "https://x", None)


def test_preconditions_refuse_disabled_rail():
    r = _good_readiness()
    r["enabled"] = False
    with pytest.raises(canary.Refuse, match="not enabled"):
        canary.verify_preconditions(_FakeHTTP(r), "https://x", None)


def test_production_sha_mismatch_refused():
    http = _FakeHTTP(_good_readiness(), release_sha="deadbeef")
    with pytest.raises(canary.Refuse, match="production SHA"):
        canary.verify_preconditions(http, "https://x", "expected-different-sha")


def test_production_sha_reads_the_real_release_endpoint(monkeypatch):
    """REGRESSION (2026-07-22): the reader must work against the ACTUAL
    /release response served by the app — not a hand-mocked schema. The
    pre-fix reader read `sha` (a key /release never served), so every capture
    was silently empty and the settlement evidence recorded
    production_sha: ""."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("RENDER_GIT_COMMIT",
                       "1234567890abcdef1234567890abcdef12345678")
    client = TestClient(app)
    assert canary.production_sha(client, "") == (
        "1234567890abcdef1234567890abcdef12345678")


def test_production_sha_normalizes_unknown_to_unverifiable(monkeypatch):
    """/release honestly serves git_sha='unknown' when the platform env is
    absent; the canary must treat that as UNVERIFIABLE (''), never as a
    comparable value."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("GUILD_GIT_SHA", raising=False)
    client = TestClient(app)
    assert canary.production_sha(client, "") == ""


def test_expect_sha_fails_closed_when_unverifiable():
    """An EXPLICIT SHA expectation that cannot be verified is a refusal, not
    a silent pass. (Pre-fix, the broken reader returned '' on every call, so
    --expect-sha never refused anything — a no-op safety flag.)"""
    class _NoShaHTTP(_FakeHTTP):
        def get(self, url, timeout=0):
            if url.endswith("/release"):
                return _FakeResp({"service": "Agent Guild",
                                  "git_sha": "unknown"})
            return super().get(url, timeout)
    with pytest.raises(canary.Refuse, match="unverifiable"):
        canary.verify_preconditions(_NoShaHTTP(_good_readiness()),
                                    "https://x", "expected-sha")


def test_repair_evidence_is_idempotent_and_paymentless(tmp_path, monkeypatch):
    """--repair-evidence: fills a labelled repair block on an artifact whose
    capture was empty, leaves the captured field untouched, never pays, and
    is byte-stable on a second run."""
    ev = {"label": "first_party_mainnet_canary", "production_sha": "",
          "transaction": "0x1052fa51aa1412119581194acc1011c51786a59538f46bb"
                         "5f9d593f1ad16d802",
          "target": "https://guild.example"}
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(ev))

    class _ReleaseOnlyClient:
        calls = []

        def __init__(self, *a, **k):
            pass

        def get(self, url, timeout=0):
            _ReleaseOnlyClient.calls.append(url)
            assert url.endswith("/release"), "repair must be read-only"
            return _FakeResp({"git_sha": "livesha123"})

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "Client", _ReleaseOnlyClient)
    args = types.SimpleNamespace(evidence=str(path),
                                 base="https://guild.example")
    assert canary.repair_evidence(args) == 0
    repaired = json.loads(path.read_text())
    assert repaired["production_sha"] == ""          # capture NOT rewritten
    rep = repaired["production_sha_repair"]
    assert rep["payment_made"] is False and rep["read_only"] is True
    assert rep["live_git_sha_at_repair"] == "livesha123"
    # the known settlement derivation attaches by tx hash, labelled derived
    assert rep["settlement_time_sha"]["sha"].startswith("7b09548")
    assert "derived" in rep["settlement_time_sha"]["confidence"]
    first_bytes = path.read_bytes()
    assert canary.repair_evidence(args) == 0          # idempotent no-op
    assert path.read_bytes() == first_bytes


# --- secret-silent key loading + one-shot state ------------------------------

def test_key_loading_env_and_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CANARY_PRIVATE_KEY", "0x" + "42" * 32)
    assert canary.load_private_key(None) == "0x" + "42" * 32
    monkeypatch.delenv("CANARY_PRIVATE_KEY")
    kf = tmp_path / "k.json"
    kf.write_text(json.dumps({"privateKey": "ab" * 32}))
    key = canary.load_private_key(str(kf))
    assert key == "0x" + "ab" * 32
    # never printed
    out = capsys.readouterr()
    assert "ab" * 32 not in out.out and "42" * 32 not in out.out


def test_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("CANARY_PRIVATE_KEY", raising=False)
    assert canary.load_private_key(None) is None


# --- secret-silent preflight (A4) ---------------------------------------------

def test_default_key_file_is_the_protected_canary_key():
    assert str(canary.DEFAULT_KEY_FILE).endswith(
        "live/secrets/x402_mainnet_canary.key")


def test_key_file_facts_never_expose_the_key(tmp_path, capsys):
    secret = "ab" * 32
    kf = tmp_path / "x402_mainnet_canary.key"
    kf.write_text("0x" + secret + "\n")
    kf.chmod(0o600)
    facts = canary.key_file_facts(str(kf))
    assert facts["exists"] is True
    assert facts["permissions_private"] is True
    assert facts["permissions_octal"] == "0o600"
    assert facts["format"] == "hex" and facts["format_accepted"] is True
    # the secret never appears in the facts or on stdout
    assert secret not in json.dumps(facts)
    assert secret not in capsys.readouterr().out


def test_key_file_facts_flag_bad_permissions_and_format(tmp_path):
    kf = tmp_path / "loose.key"
    kf.write_text("0x" + "ab" * 32)
    kf.chmod(0o644)
    facts = canary.key_file_facts(str(kf))
    assert facts["permissions_private"] is False
    bad = tmp_path / "bad.key"
    bad.write_text("not a key at all")
    bad.chmod(0o600)
    facts2 = canary.key_file_facts(str(bad))
    assert facts2["format_accepted"] is False
    missing = canary.key_file_facts(str(tmp_path / "nope.key"))
    assert missing["exists"] is False


def test_key_file_facts_accepts_json_shape(tmp_path):
    kf = tmp_path / "k.json"
    kf.write_text(json.dumps({"privateKey": "cd" * 32}))
    kf.chmod(0o600)
    facts = canary.key_file_facts(str(kf))
    assert facts["format"] == "json_private_key"
    assert facts["format_accepted"] is True


def test_the_protected_key_exists_with_private_perms_and_accepted_format():
    """Operability check of the REAL protected key — via key_file_facts only
    (existence, permissions, format); the key material is never read into
    the test, printed, or asserted on."""
    real = canary.DEFAULT_KEY_FILE
    if not real.exists():
        pytest.skip("protected canary key not present in this checkout")
    facts = canary.key_file_facts(str(real))
    assert facts["permissions_private"] is True
    assert facts["format_accepted"] is True


def test_state_roundtrip_is_atomic(tmp_path):
    p = tmp_path / "state.json"
    assert canary._load_state(p)["status"] == "new"
    canary._save_state(p, {"version": 1, "status": "signed",
                           "signed_payload": "xyz"})
    assert canary._load_state(p)["status"] == "signed"
    assert canary._load_state(p)["signed_payload"] == "xyz"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_signed_receipt_resolves_trusted_did_web(monkeypatch):
    monkeypatch.setenv("GUILD_PUBLIC_HOST", "https://guild.example")
    from app.state import store
    identity = store.guild_identity()
    resource = _resource()
    payer = "0x" + "12" * 20
    tx = "0x" + "34" * 32
    payload = artifacts.receipt_payload(
        network="eip155:8453", resource_url=resource, payer=payer,
        transaction=tx, issued_at=1,
    )
    receipt = artifacts.signed_receipt(identity, payload)
    raw = {
        "extensions": {
            "offer-receipt": artifacts.offer_receipt_settle_extension(receipt)
        }
    }

    class Response:
        headers = {
            x402.PAYMENT_RESPONSE_HEADER:
                base64.b64encode(json.dumps(raw).encode()).decode()
        }

    assert canary._verify_settle_receipt(
        Response(), resource, payer, tx, http=_did_http(),
    )
    assert not canary._verify_settle_receipt(
        Response(), resource, "0x" + "99" * 20, tx, http=_did_http(),
    )


def test_revenue_transition_allows_idempotent_recovery_only():
    tx = "0x" + "56" * 32
    before = {"transactions": 1, "transaction_hashes": [tx]}
    unchanged = {"transactions": 1, "transaction_hashes": [tx]}
    increased = {"transactions": 2, "transaction_hashes": [tx]}

    canary._verify_revenue_transition(
        before, unchanged, tx, replayed_signed_payload=True,
    )
    canary._verify_revenue_transition(
        before, increased, tx, replayed_signed_payload=True,
    )
    canary._verify_revenue_transition(
        before, increased, tx, replayed_signed_payload=False,
    )
    with pytest.raises(canary.Refuse, match="exactly one"):
        canary._verify_revenue_transition(
            before, unchanged, tx, replayed_signed_payload=False,
        )
    with pytest.raises(canary.Refuse, match="not in real_settlement"):
        canary._verify_revenue_transition(
            before, {"transactions": 2, "transaction_hashes": []}, tx,
            replayed_signed_payload=False,
        )


def test_discovery_falls_back_to_canonical_check():
    class H:
        def get(self, url, timeout=0):
            raise RuntimeError("manifest unavailable")
    url = canary.discover_resource(H(), "https://guild.example")
    assert url == f"https://guild.example/check?capability={canary.CANARY_CAPABILITY}"


def test_evidence_is_labelled_first_party_and_secretless(tmp_path):
    ev = {"label": "first_party_mainnet_canary", "amount_usdc": 0.01}
    p = tmp_path / "ev.json"
    canary._write_evidence(p, ev)
    loaded = json.loads(p.read_text())
    assert loaded["label"] == "first_party_mainnet_canary"


def test_execute_refuses_without_first_party_tagging(monkeypatch, tmp_path):
    """--execute without GUILD_FIRST_PARTY_TOKEN would settle a payment the
    server can only classify unverified_payer (the 2026-07-21 mislabel) —
    refuse BEFORE any network traffic unless --allow-untagged."""
    monkeypatch.delenv("GUILD_FIRST_PARTY_TOKEN", raising=False)
    monkeypatch.setattr(canary, "DEFAULT_FP_TOKEN_FILE",
                        tmp_path / "absent_token")
    args = types.SimpleNamespace(
        base="https://guild.example", execute=True, dry_run=False,
        allow_untagged=False, state=str(tmp_path / "state.json"),
        evidence=str(tmp_path / "ev.json"), key_file=None, expect_sha=None)
    with pytest.raises(canary.Refuse, match="first-party tagging"):
        canary.run(args)
    assert not (tmp_path / "state.json").exists()


def test_first_party_headers_env_then_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GUILD_FIRST_PARTY_TOKEN", raising=False)
    missing = tmp_path / "absent_token"
    monkeypatch.setattr(canary, "DEFAULT_FP_TOKEN_FILE", missing)
    assert canary._first_party_headers() == {}
    token_file = tmp_path / "first_party_token"
    token_file.write_text("file-secret\n")
    monkeypatch.setattr(canary, "DEFAULT_FP_TOKEN_FILE", token_file)
    assert canary._first_party_headers() == {
        "X-Agent-Guild-First-Party": "file-secret"}
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "env-secret")  # env wins
    assert canary._first_party_headers() == {
        "X-Agent-Guild-First-Party": "env-secret"}
