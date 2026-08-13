from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_repository_skill_is_discoverable_and_routes_high_value_payments():
    skill = (ROOT / "SKILL.md").read_text()
    readme = (ROOT / "README.md").read_text()

    assert skill.startswith("---\nname: agent-guild\n")
    assert "npx skills add AgentTanuki/agent-guild --skill agent-guild" in readme
    assert "npx skills add https://agent-guild-5d5r.onrender.com" in readme
    assert "https://agent-guild-5d5r.onrender.com/.well-known/agent-skills/index.json" in readme
    assert "GET https://agent-guild-5d5r.onrender.com/wallet-binding/protected-decision/tiers" in skill
    assert "kh743b8n09qnxq2tqnwyb4bc6d8camnh" in skill
    assert "This is a signed authorization" in skill
    assert "not insurance or escrow" in skill


def test_clawhub_skill_is_a_source_tagged_policy_parity_copy():
    """The registry bundle is one auditable file and cannot silently drift
    from the repository policy. Only its install name and explicit acquisition
    source differ."""
    canonical = (ROOT / "SKILL.md").read_text()
    published = (ROOT / "skills" / "agent-guild-trust" / "SKILL.md").read_text()
    expected = canonical.replace(
        "name: agent-guild\n",
        "name: agent-guild-trust\n",
        1,
    ).replace(
        "https://agent-guild-5d5r.onrender.com/.well-known/agent-guild.json",
        "https://agent-guild-5d5r.onrender.com/.well-known/agent-guild.json?src=paid_offer:clawhub_skill",
        1,
    )
    assert published == expected
    assert (ROOT / "skills" / "agent-guild-trust" / ".clawhubignore").read_text() == \
        "*\n!SKILL.md\n"


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
