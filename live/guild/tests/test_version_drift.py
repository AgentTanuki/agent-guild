"""Machine-visible release identity: every surface a machine reads must
report the SAME version, and a breaking payment-enforcement change must never
silently reuse a version that was already published.

Context (A3, 2026-07-15): MCP/A2A payment enforcement (previously-free
guild_check & co now answer payment challenges) shipped while 1.2.0 was
already live on the MCP Registry. For machines that is a breaking contract
change → deliberate MAJOR bump to 2.0.0, with the x402 payment mechanism and
priced operations declared in the publisher-provided registry metadata.
"""
import asyncio
import json
import pathlib

from fastapi.testclient import TestClient

from app import __version__, pricing
from app.billing import PRICING

REPO = pathlib.Path(__file__).resolve().parents[3]
GUILD = pathlib.Path(__file__).resolve().parents[1]

# every version that has EVER been published to the MCP Registry — all of
# them describe the pre-payment-enforcement contract and may never be reused.
PUBLISHED_PRE_ENFORCEMENT_VERSIONS = {"1.0.0", "1.1.0", "1.2.0"}

#: version -> sha256 of the registry-visible metadata AS PUBLISHED under it.
#: Changing already-published listing metadata without a version bump means the
#: registry keeps serving the old blob under the same number, and the automated
#: publish + exact-version readback (.github/workflows/publish-mcp.yml, which
#: triggers on server.json) has nothing new to publish. Recording the hash is
#: what makes that a test failure rather than something noticed months later.
#: Add an entry when a version is published; never edit one.
PUBLISHED_REGISTRY_FINGERPRINTS = {
    "2.0.2": "205fdfd11fdce92d5b96685df96e377bb413d96c6c70e3f696a50621ca150d09",
    "2.1.0": "6f877f0ff8a937297b0d17d4cad09592f9a48e3aba282b7671a728e0f6b2ddb8",
    "2.1.1": "2837b086e33b2037fd10b1b25cd934eccb75bbf590fe3cfd2f8dbc6252438a74",
    "2.1.4": "d89c3be7d8f73015e6a47299160ce371578697231957b440022821e9b6266729",
    "2.1.5": "d89c3be7d8f73015e6a47299160ce371578697231957b440022821e9b6266729",
}


def _registry_fingerprint(server: dict) -> str:
    """Hash of exactly what the registry serves to a machine: the description
    plus the publisher-provided blob. Version itself is excluded — it is the
    thing being checked."""
    import hashlib
    payload = json.dumps(
        {"description": server["description"],
         "publisher_provided": server["_meta"][
             "io.modelcontextprotocol.registry/publisher-provided"]},
        sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def test_changed_registry_metadata_requires_a_version_bump():
    """The 2026-08-01 failure mode: server.json was byte-identical to what was
    already published, so the paid-operations block could have been added
    without anyone noticing the listing would never carry it.

    Now: if the current version is one already published, the registry-visible
    metadata must be BYTE-IDENTICAL to what went out under it. Changing it
    requires a new version, which is what makes the publish workflow fire and
    the exact-version readback meaningful."""
    server = json.loads((REPO / "server.json").read_text())
    assert server["version"] == __version__
    published = PUBLISHED_REGISTRY_FINGERPRINTS.get(__version__)
    if published is None:
        return                      # a new, not-yet-published version: fine
    assert _registry_fingerprint(server) == published, (
        f"registry metadata changed but the version is still {__version__}, "
        "which is already published — the registry would keep serving the old "
        "blob under this number. Bump the patch version and add the new "
        "fingerprint to PUBLISHED_REGISTRY_FINGERPRINTS once it is live.")


def test_the_paid_discovery_release_is_a_new_version():
    """This release changes already-published registry metadata (it adds
    ai.agent-guild/paid-operations), so it may not reuse 2.0.2."""
    server = json.loads((REPO / "server.json").read_text())
    pp = server["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]
    if "ai.agent-guild/paid-operations" in pp:
        assert __version__ not in ("2.0.2",), (
            "paid-operations discovery is a change to already-published "
            "registry metadata; it needs its own version")


def test_breaking_payment_enforcement_never_reuses_a_published_version():
    assert __version__ not in PUBLISHED_PRE_ENFORCEMENT_VERSIONS, (
        "paid MCP/A2A behaviour changed after this version was published — "
        "bump the version (semver MAJOR for machine-breaking changes)")
    major = int(__version__.split(".")[0])
    assert major >= 2, (
        "payment enforcement on previously-free MCP/A2A operations is a "
        "breaking change for machine consumers; it requires a MAJOR bump")


def test_every_machine_surface_reports_the_same_version():
    from app.main import app
    from app.mcp_server import mcp
    from app.a2a import _agent_card
    with TestClient(app) as client:
        assert client.get("/release").json()["version"] == __version__
        assert client.get("/openapi.json").json()["info"]["version"] == \
            __version__
        manifest = client.get("/.well-known/agent-guild.json").json()
        assert manifest["version"] == __version__
        card = client.get("/.well-known/agent-card.json").json()
        assert card["version"] == __version__
    assert _agent_card("https://x.example")["version"] == __version__
    # FastMCP serverInfo
    assert mcp.version == __version__
    # server.json + contract.json (committed, generated artifacts)
    server = json.loads((REPO / "server.json").read_text())
    assert server["version"] == __version__
    contract = json.loads((GUILD / "contract" / "contract.json").read_text())
    assert contract["service"]["version"] == __version__


def test_registry_metadata_sells_signed_messages_and_preserves_free_passport():
    """The searchable listing names the paid signed-message product while the
    first executable acquisition block preserves the FREE self-serve passport.

    This test used to assert the listing was "payment-free". That was right for
    the 2026-07-23 acquisition release and wrong from 2026-08-01: the MCP
    Registry is our one already-live external listing, and omitting the paid
    layer from it made the revenue pivot invisible on the only surface machines
    were already reading. `paidcatalog.SOURCE_IDS` even declared
    `paid_offer:registry` while no route could produce it.

    What is asserted now:
      * signed machine messages and x402 are searchable in the description;
      * passport remains the first publisher block and stays free;
      * the one-call envelope client is executable from registry metadata;
      * paid discovery EXISTS;
      * its catalog URL is a real callable route with a producible attribution;
      * NO copied prices (they drift when experiments run; a listing is
        republished rarely, so a copied number would be stale and a stale price
        is a lie);
      * the blob stays under the registry's 4KB cap.
    """
    server = json.loads((REPO / "server.json").read_text())
    pp = server["_meta"][
        "io.modelcontextprotocol.registry/publisher-provided"]
    host = "https://agent-guild-5d5r.onrender.com"

    # --- search copy exposes the paid product without hiding the free path -
    description = server["description"].lower()
    for phrase in ("signed machine messages", "x402", "free", "passports"):
        assert phrase in description
    assert len(server["description"]) <= 100

    # --- passport remains the first executable acquisition block ----------
    assert list(pp)[0] == "ai.agent-guild/passport"
    passport = pp["ai.agent-guild/passport"]
    assert "No human involved" in passport["offer"]
    assert "passport_offer:mcp_registry" in passport["register"]
    assert passport["register"].startswith("POST " + host + "/agents/register")
    assert passport["prove_start"] == "POST " + host + "/agents/{id}/prove"
    assert passport["prove_verify"].startswith(
        "POST " + host + "/agents/{id}/prove/verify")
    assert passport["passport"].startswith(
        "GET " + host + "/agents/{id}/passport")
    assert passport["verify_credential"].startswith(
        "POST " + host + "/credentials/verify")
    assert passport["badge"].startswith(
        "GET " + host + "/agents/{id}/badge.svg")
    assert passport["next_evidence"].startswith("POST " + host + "/attestations")

    # --- one-call paid signed-message path is explicit --------------------
    envelope = pp["ai.agent-guild/machine-envelope"]
    assert "signed machine message" in envelope["offer"].lower()
    assert envelope["client"] == (
        host + "/sdk/agentguild_envelope_client.mjs")
    assert envelope["factory"] == (
        "createEvmMachineEnvelopeClient({evmSigner})")
    assert envelope["issue"].startswith("client.issue({payload")
    assert "One caller-owned Base EOA" in envelope["identity_payment"]
    assert "no registration" in envelope["identity_payment"]
    assert "x402" in envelope["payment"] and "Base mainnet" in envelope["payment"]
    assert envelope["catalog"].endswith("?src=paid_offer:registry")
    assert "offline" in envelope["verify"]
    assert "caller-controlled" in envelope["custody"]

    # --- exact-wallet payment policy is executable from the listing -------
    wallet = pp["ai.agent-guild/wallet-policy"]
    assert "fail-closed" in wallet["offer"].lower()
    assert "EVM" in wallet["offer"]
    assert wallet["resolve"] == (
        "GET " + host +
        "/wallet-binding/resolve?address={0x...}&network=eip155:8453")
    assert wallet["virtuals"] == (
        host + "/sdk/integrations/virtuals_acp_fund_policy.mjs")
    assert wallet["x402"] == (
        host + "/sdk/integrations/x402_payment_policy.mjs")

    # --- paid discovery exists and names the real operations -------------
    paid = pp["ai.agent-guild/paid-operations"]
    from app import paidcatalog
    assert set(paid["operations"]) == {
        o["operation"] for o in paidcatalog.operations()}
    assert paid["free_alternative_exists_for_every_paid_operation"] is True
    assert all(o["free_alternative"].strip()
               for o in paidcatalog.operations())

    # --- NO STALE COPIED PRICES ------------------------------------------
    # Checked on the STRUCTURE, not by substring: "20" occurs inside
    # "eip155:8453" and would give a false positive. A price would have to
    # arrive either as a numeric leaf or as a currency literal, so both are
    # banned outright — the listing is republished rarely and a copied number
    # would be stale the moment an experiment moved it.
    def _leaves(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from _leaves(v)
        elif isinstance(node, list):
            for v in node:
                yield from _leaves(v)
        else:
            yield node

    numeric = [v for v in _leaves(paid)
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert not numeric, (
        f"numeric leaves in the registry paid block {numeric} — a price copied "
        "into the listing is stale the moment an experiment moves it")
    blob = json.dumps(paid)
    assert "$" not in blob, "no currency literals in the listing"
    for op in paidcatalog.operations():
        assert op["price_usd"] not in blob

    # --- the catalog URL is a REAL callable route with a REAL producer ----
    from urllib.parse import urlparse, parse_qs
    method, _, url = paid["catalog"].partition(" ")
    assert method == "GET"
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == host
    assert parsed.path == "/.well-known/agent-guild.json"
    src = parse_qs(parsed.query)["src"][0]
    assert src == "paid_offer:registry"
    assert src in paidcatalog.SOURCE_IDS

    from app.main import app as _app
    with TestClient(_app) as c:
        assert parsed.path in c.get("/openapi.json").json()["paths"], (
            "the listing advertises a catalog URL that is not a live route")
        # THE SOURCE MUST HAVE A PRODUCER. Declaring a source id that nothing
        # can emit is how paid_offer:registry sat unused: the funnel would have
        # reported a surface that could never move.
        body = c.get(f"{parsed.path}?src={src}").json()
        assert body["paid_operations"]["source"] == src

        # The registry's wallet policy points only at live machine surfaces.
        assert "/wallet-binding/resolve" in c.get(
            "/openapi.json").json()["paths"]
        adapter = c.get("/sdk/integrations/virtuals_acp_fund_policy.mjs")
        assert adapter.status_code == 200
        assert "createAgentGuildFundPolicy" in adapter.text
        assert "createAgentGuildAcpPaymentPolicy" in adapter.text

    for advertised in ("/agents/register", "/agents/{agent_id}/prove",
                       "/agents/{agent_id}/prove/verify",
                       "/agents/{agent_id}/passport", "/credentials/verify",
                       "/agents/{agent_id}/badge.svg", "/attestations"):
        with TestClient(_app) as c:
            assert advertised in c.get("/openapi.json").json()["paths"], (
                f"listing advertises a dead endpoint: {advertised}")

    # the whole publisher-provided blob stays under the registry's 4KB cap
    assert len(json.dumps(pp).encode()) < 4096


def test_contract_payments_block_matches_billing_and_gateway():
    contract = json.loads((GUILD / "contract" / "contract.json").read_text())
    pay = contract["payments"]
    assert pay["mechanism"] == "x402" and pay["x402_version"] == 2
    expected = {**PRICING,
                **{op: cost for op, cost in pricing.DEFAULTS.items()
                   if cost > 0}}
    assert set(pay["priced_operations"]) == set(expected)
    for op, row in pay["priced_operations"].items():
        assert row["credits"] == expected[op]
        assert row["usdc_atomic"] == expected[op] * 1000
    # the priced MCP tools declared in the contract exist in the MCP tool list
    assert set(pay["priced_mcp_tools"]) <= set(contract["mcp_tools"])
    assert "guild.check" in pay["priced_a2a_skills"]


def test_generated_artifacts_have_zero_drift_from_the_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "contract_generate", GUILD / "contract" / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    contract = gen.build_contract()
    committed = json.loads((GUILD / "contract" / "contract.json").read_text())
    assert committed == json.loads(
        json.dumps(contract)), "contract.json drifted — run `make contract`"
    server = json.loads((REPO / "server.json").read_text())
    assert server == json.loads(json.dumps(gen.derived_server_json(contract)))
