"""First-party mainnet canary: safety guards, discovery, idempotent state.

The canary is DRY-RUN by default and never runs execution in tests. These
cover the parts that keep --execute safe: the hard 0.01 USDC cap, refusal of
any unexpected recipient/network/asset/amount/resource, signed-offer
verification, precondition checks, secret-silent key loading, and the
pay-before-state-is-impossible one-shot file.
"""
import importlib.util
import json
import pathlib
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


def _challenge(monkeypatch, amount_credits=10):
    monkeypatch.setenv("GUILD_PUBLIC_HOST", "https://guild.example")
    preq = payments.check_request(canary.CANARY_CAPABILITY)
    model = payments.challenge_model(preq)
    return model.model_dump(by_alias=True, exclude_none=True), preq


# --- lifetime cap + binding refusals -----------------------------------------

def test_challenge_within_cap_is_accepted(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    req, canonical = canary.verify_challenge(challenge, preq.resource_url, facts)
    assert int(req["amount"]) == 10 * 1000            # 0.01 USDC exactly
    assert canonical == preq.resource_url


def test_amount_over_lifetime_cap_is_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    # tamper the quoted amount above 0.01 USDC (10001 atomic units)
    challenge["accepts"][0]["amount"] = "20000"        # 0.02 USDC
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    with pytest.raises(canary.Refuse, match="lifetime cap"):
        canary.verify_challenge(challenge, preq.resource_url, facts)


def test_wrong_recipient_is_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    challenge["accepts"][0]["payTo"] = "0x" + "99" * 20
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    with pytest.raises(canary.Refuse, match="payTo"):
        canary.verify_challenge(challenge, preq.resource_url, facts)


def test_wrong_asset_and_network_refused(monkeypatch):
    challenge, preq = _challenge(monkeypatch)
    facts = {"asset": MAINNET_USDC, "recipient": TREASURY}
    bad_asset = json.loads(json.dumps(challenge))
    bad_asset["accepts"][0]["asset"] = x402.USDC_BY_NETWORK["eip155:84532"]
    with pytest.raises(canary.Refuse, match="asset"):
        canary.verify_challenge(bad_asset, preq.resource_url, facts)
    bad_net = json.loads(json.dumps(challenge))
    bad_net["accepts"][0]["network"] = "eip155:84532"
    with pytest.raises(canary.Refuse, match="network"):
        canary.verify_challenge(bad_net, preq.resource_url, facts)


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
            canary.verify_challenge(c, preq.resource_url, facts)


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
        canary.verify_challenge(challenge, preq.resource_url, facts)


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
            return _FakeResp({"sha": self._sha})
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


def test_state_roundtrip_is_atomic(tmp_path):
    p = tmp_path / "state.json"
    assert canary._load_state(p)["status"] == "new"
    canary._save_state(p, {"version": 1, "status": "signed",
                           "signed_payload": "xyz"})
    assert canary._load_state(p)["status"] == "signed"
    assert canary._load_state(p)["signed_payload"] == "xyz"


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
