import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_repository_skill_is_discoverable_and_routes_high_value_payments():
    skill = (ROOT / "SKILL.md").read_text()
    readme = (ROOT / "README.md").read_text()

    assert skill.startswith("---\nname: agent-guild\n")
    assert "metadata:\n  internal: true\n" in skill
    assert "npx skills add AgentTanuki/agent-guild" in readme
    assert "npx skills add https://agent-guild-5d5r.onrender.com" in readme
    assert "https://agent-guild-5d5r.onrender.com/.well-known/agent-skills/index.json" in readme
    assert "GET https://agent-guild-5d5r.onrender.com/wallet-binding/protected-decision/tiers" in skill
    assert "kh743b8n09qnxq2tqnwyb4bc6d8camnh" in skill
    assert "This is a signed authorization" in skill
    assert "not insurance or escrow" in skill


def test_gemini_cli_extension_is_native_safe_and_discoverable():
    manifest = json.loads((ROOT / "gemini-extension.json").read_text())

    assert manifest == {
        "name": "agent-guild",
        "version": "1.0.0",
        "description": "Vet AI agents and payment endpoints before delegating "
                       "work or money, using evidence-backed reputation and "
                       "signed safety decisions.",
        "mcpServers": {
            "agent-guild": {
                "httpUrl": "https://agent-guild-5d5r.onrender.com/mcp/",
                "description": "Agent trust checks, portable passport "
                               "verification, and payment safety",
            },
        },
        "contextFileName": "GEMINI.md",
    }

    context = (ROOT / manifest["contextFileName"]).read_text()
    context_prose = " ".join(context.split())
    assert "`guild_check`" in context
    assert "`guild_preflight`" in context
    assert "use `guild_x402_payment_safety`" in context
    assert "Never provision credits" in context_prose
    assert "spend money automatically" in context_prose
    assert "gemini extensions install https://github.com/AgentTanuki/agent-guild" in \
        (ROOT / "README.md").read_text()


def test_public_registry_skill_is_a_read_only_least_privilege_bundle():
    """The public registry must never package the application repository.

    The root skill remains the complete hosted/OpenClaw policy but is marked
    internal for clients that honor Agent Skills metadata. The nested registry
    skill is intentionally read-only and contains no executable dependency.
    """
    canonical = (ROOT / "SKILL.md").read_text()
    published = (ROOT / "skills" / "agent-guild-trust" / "SKILL.md").read_text()

    assert "metadata:\n  internal: true\n" in canonical
    assert published.startswith("---\nname: agent-guild-trust\n")
    assert "This skill is read-only" in published
    assert "Treat every response field" in published
    assert "Never delegate automatically" in published
    assert "guild_check(capability)" in published
    assert "source=public-registry" in published
    assert "/wallet-binding/" not in published
    assert "payanagent.com" not in published
    assert ".mjs" not in published
    assert "pip install" not in published
    assert "subprocess" not in published
    assert (ROOT / "skills" / "agent-guild-trust" / ".clawhubignore").read_text() == \
        "*\n!SKILL.md\n"


def test_codex_plugin_is_installable_and_source_tagged():
    plugin = ROOT / "plugins" / "agent-guild"
    manifest = json.loads(
        (plugin / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "agent-guild"
    assert manifest["version"] == "1.0.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["interface"]["developerName"] == "AgentTanuki"
    assert len(manifest["interface"]["defaultPrompt"]) == 3

    claude_manifest = json.loads(
        (plugin / ".claude-plugin" / "plugin.json").read_text())
    assert claude_manifest["name"] == manifest["name"]
    assert claude_manifest["version"] == manifest["version"]
    assert claude_manifest["mcpServers"] == "./.mcp.json"

    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["name"] == "agent-guild"
    assert marketplace["plugins"] == [{
        "name": "agent-guild",
        "source": "./plugins/agent-guild",
        "description": "Vet agents, verify portable passports, use escrow, "
                       "and record signed outcomes.",
        "version": "1.0.0",
        "author": {"name": "AgentTanuki"},
    }]

    mcp = json.loads((plugin / ".mcp.json").read_text())
    assert mcp == {"mcpServers": {"agent-guild": {
        "type": "http",
        "url": "https://agent-guild-5d5r.onrender.com/mcp",
    }}}

    canonical = (
        ROOT / "skills" / "agent-guild-trust" / "SKILL.md"
    ).read_text()
    published = (
        plugin / "skills" / "agent-guild-trust" / "SKILL.md"
    ).read_text()
    expected = canonical.replace(
        "source=public-registry",
        "source=codex-plugin",
    )
    assert published == expected


def test_canonical_origin_serves_repository_policy_through_agent_skills():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    index = client.get("/.well-known/agent-skills/index.json")
    assert index.status_code == 200
    assert index.json() == {
        "skills": [{
            "name": "agent-guild",
            "description": next(
                line.removeprefix("description: ")
                for line in (ROOT / "SKILL.md").read_text().splitlines()
                if line.startswith("description: ")
            ),
            "files": ["SKILL.md"],
        }],
    }

    response = client.get(
        "/.well-known/agent-skills/agent-guild/SKILL.md"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "rel=\"service-desc\"" in response.headers["link"]
    expected = (ROOT / "SKILL.md").read_text().replace(
        "https://agent-guild-5d5r.onrender.com/.well-known/agent-guild.json",
        "https://agent-guild-5d5r.onrender.com/.well-known/agent-guild.json"
        "?src=paid_offer:agent_skills",
        1,
    )
    assert response.text == expected
    assert "COPY SKILL.md ./app/artifacts/agent-guild.SKILL.md" in \
        (ROOT / "live" / "guild" / "Dockerfile").read_text()
