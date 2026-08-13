from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_repository_skill_is_discoverable_and_routes_high_value_payments():
    skill = (ROOT / "SKILL.md").read_text()
    readme = (ROOT / "README.md").read_text()

    assert skill.startswith("---\nname: agent-guild\n")
    assert "npx skills add AgentTanuki/agent-guild --skill agent-guild" in readme
    assert "https://codex-autonomous-worker.rwdburley.chatgpt.site/SKILL.md" in readme
    assert "/.well-known/agent-skills/index.json" in readme
    assert "GET https://agent-guild-5d5r.onrender.com/wallet-binding/protected-decision/tiers" in skill
    assert "kh743b8n09qnxq2tqnwyb4bc6d8camnh" in skill
    assert "This is a signed authorization" in skill
    assert "not insurance or escrow" in skill
