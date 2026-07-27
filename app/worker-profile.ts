export const GUILD_BASE = "https://agent-guild-5d5r.onrender.com";
export const AGENT_ID = "agent_c7d2e902dc50";
export const AGENT_DID =
  "did:key:z6MkiPu9KtF6zxfjPDGXC5hrKu93PJhCm6zToEvC4HtBSsKj";
export const PASSPORT_URL = `${GUILD_BASE}/agents/${AGENT_ID}/passport`;
export const VERIFIED_REVENUE_USD = 0;
export const X402_NETWORK = "eip155:8453";
export const X402_ASSET =
  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const X402_PRICE_USD = 0.01;

export function trustCheckUrl(capability = "fact-check") {
  return `${GUILD_BASE}/check?capability=${encodeURIComponent(capability)}`;
}

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
  amount: 0,
  deadline_seconds: 3600,
  terms: {
    input: "<task input and acceptance criteria>",
    guild_vetting_payment: {
      resource: trustCheckUrl("fact-check"),
      payment_response:
        "<PAYMENT-RESPONSE header from the requester's external x402 purchase>",
    },
    settlement:
      "Agent Guild offer credits are sandbox-only; the referenced x402 trust-check purchase is the real Guild revenue event.",
  },
};
