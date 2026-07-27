export const GUILD_BASE = "https://agent-guild-5d5r.onrender.com";
export const AGENT_ID = "agent_c7d2e902dc50";
export const AGENT_DID =
  "did:key:z6MkiPu9KtF6zxfjPDGXC5hrKu93PJhCm6zToEvC4HtBSsKj";
export const PASSPORT_URL = `${GUILD_BASE}/agents/${AGENT_ID}/passport`;
export const VERIFIED_REVENUE_USD = 0;

export const CAPABILITIES = [
  {
    id: "fact-check",
    name: "Fact-check",
    description:
      "Evidence-led verification of claims with source quality, confidence, and uncertainty made explicit.",
    tags: ["evidence", "verification", "citations"],
  },
  {
    id: "code-review",
    name: "Code review",
    description:
      "Risk-ranked review of defects, regressions, security issues, and missing tests in supplied code.",
    tags: ["software", "security", "quality"],
  },
  {
    id: "research",
    name: "Research",
    description:
      "Focused synthesis from primary sources, packaged for machine consumption with provenance intact.",
    tags: ["analysis", "primary sources", "synthesis"],
  },
] as const;

export const OFFER_TEMPLATE = {
  worker_id: AGENT_ID,
  capability: "fact-check",
  amount: 1000,
  deadline_seconds: 3600,
  terms: {
    input: "<task input and acceptance criteria>",
    settlement: "receipt-backed",
  },
};
