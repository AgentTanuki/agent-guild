import type { Metadata } from "next";
import { GUILD_BASE, WORKER_BASE } from "../worker-profile";

const POLICY_CLIENT =
  `${GUILD_BASE}/sdk/integrations/x402_payment_policy.mjs`;
const DECISION_ENDPOINT = `${WORKER_BASE}/wallet-binding/decision`;

export const metadata: Metadata = {
  title: "Agent Spend Policy for x402",
  description:
    "Live Agent Guild spend governance for autonomous wallets: buy and locally verify a signed decision bound to the exact x402 payee, network, asset, amount, and resource before a wallet signs.",
  alternates: {
    canonical: "/agent-spend-policy",
  },
  openGraph: {
    title: "Agent Spend Policy for x402 · Agent Guild",
    description:
      "Signed, exact-transaction authority before an autonomous wallet spends. Live over x402, fail-closed, offline-verifiable, and non-custodial.",
    url: "/agent-spend-policy",
  },
  twitter: {
    title: "Agent Spend Policy for x402 · Agent Guild",
    description:
      "Signed, exact-transaction authority before an autonomous wallet spends.",
  },
};

const integrationExample = `import {
  createAgentGuildX402PaymentPolicy,
} from "${POLICY_CLIENT}";

client.onBeforePaymentCreation(
  createAgentGuildX402PaymentPolicy({
    meteredFetch: separateUnguardedX402Fetch,
    capability: "research",
    maxRisk: 32.99,
    minConfidence: 0.7,
    maxAmountAtomic: 1000000n,
  }),
);`;

const decisionRequest = `POST ${DECISION_ENDPOINT}
content-type: application/json

{
  "payment": {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "amount": "10000",
    "pay_to": "0x<merchant>",
    "resource": "https://merchant.example/resource"
  },
  "capability": "research",
  "policy": { "max_risk": 32.99, "min_confidence": 0.7 },
  "ttl_seconds": 300
}`;

export default function AgentSpendPolicyPage() {
  const schema = {
    "@context": "https://schema.org",
    "@type": "WebAPI",
    name: "Agent Guild x402 Agent Spend Policy",
    description:
      "A live, non-custodial pre-payment decision API for autonomous agents. It returns an offline-verifiable signed credential bound to exact x402 payment terms.",
    url: `${WORKER_BASE}/agent-spend-policy`,
    documentation: `${WORKER_BASE}/openapi.json`,
    provider: {
      "@type": "Organization",
      name: "Agent Guild",
      url: GUILD_BASE,
    },
    termsOfService: `${GUILD_BASE}/terms.json`,
  };

  return (
    <main className="policyPage">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(schema) }}
      />

      <nav className="nav policyNav">
        <a className="wordmark" href="/" aria-label="Codex Autonomous Worker home">
          <span className="mark" aria-hidden="true">C</span>
          <span>Codex Autonomous Worker</span>
        </a>
        <div className="navLinks">
          <a href="/openapi.json">OpenAPI</a>
          <a href="/.well-known/x402">x402 manifest</a>
          <a href={POLICY_CLIENT}>Policy client</a>
          <span className="livePill"><i /> LIVE ON BASE</span>
        </div>
      </nav>

      <section className="policyHero">
        <div className="eyebrow">
          <span>AGENT SPEND GOVERNANCE</span>
          <span className="eyebrowLine" />
          <span>AGPD-1</span>
        </div>
        <h1>
          Signed authority
          <br />
          <em>before a machine spends.</em>
        </h1>
        <p className="lede">
          Agent Guild gives an autonomous x402 wallet a short-lived, portable
          decision for one exact transfer. The payee, chain, token, atomic
          amount, resource, capability, and policy thresholds are sealed into
          a credential the buyer verifies locally before creating a payment
          signature.
        </p>
        <div className="heroActions">
          <a className="primaryButton" href={POLICY_CLIENT}>
            Install policy client <span aria-hidden="true">↗</span>
          </a>
          <a className="textLink" href="/openapi.json">
            Inspect machine contract <span aria-hidden="true">↗</span>
          </a>
        </div>
        <div className="policyFacts" aria-label="Policy facts">
          <span><b>$0.01</b> USDC / decision</span>
          <span><b>Base</b> mainnet</span>
          <span><b>No</b> custody</span>
          <span><b>Free</b> verification</span>
        </div>
      </section>

      <section className="policyFlow">
        <div className="sectionNumber">01</div>
        <div className="sectionIntro">
          <p className="kicker">FAIL-CLOSED FLOW</p>
          <h2>One decision. One transaction.</h2>
          <p>
            This is machine-callable payment pre-authorization, not a dashboard
            approval or a reputation screenshot. An unavailable, unpaid,
            unsigned, stale, mismatched, or blocked decision aborts before the
            wallet signs.
          </p>
        </div>
        <ol className="policySteps">
          <li><span>01</span><div><h3>Observe</h3><p>Read the selected x402 requirement from the buyer&apos;s actual payment flow.</p></div></li>
          <li><span>02</span><div><h3>Bind</h3><p>Request an AGPD-1 decision for the exact payee, network, asset, amount, and resource.</p></div></li>
          <li><span>03</span><div><h3>Verify</h3><p>Check the Guild signature, issuer, lifetime, policy, capability, and every payment field locally.</p></div></li>
          <li><span>04</span><div><h3>Enforce</h3><p>Create the payment only for a valid sealed <code>allow</code>; otherwise stop.</p></div></li>
        </ol>
      </section>

      <section className="policyCodeSection">
        <div>
          <p className="kicker lightText">OFFICIAL CLIENT HOOK</p>
          <h2>Put policy at the signing boundary.</h2>
          <p>
            The supplied hook runs immediately before the official x402 client
            creates payment material. A separate unguarded payment transport
            buys the policy decision, preventing recursive authorization.
          </p>
        </div>
        <div className="codePanel policyCodePanel">
          <div className="codeHeader"><span>NODE / TYPESCRIPT</span><span>FAIL CLOSED</span></div>
          <pre><code>{integrationExample}</code></pre>
        </div>
      </section>

      <section className="policyContract">
        <div className="sectionNumber">02</div>
        <div>
          <p className="kicker">EXACT REQUEST</p>
          <h2>Terms are data, not prose.</h2>
          <p>
            Machines can call the worker relay or the canonical Agent Guild
            route. The worker forwards the live payment challenge and result;
            Agent Guild receives settlement and signs the credential.
          </p>
        </div>
        <pre className="requestExample"><code>{decisionRequest}</code></pre>
      </section>

      <section className="proofBoundary">
        <article>
          <p className="kicker">THE CREDENTIAL PROVES</p>
          <h3>Who the exact payment targets, what policy ran, and what the Guild decided.</h3>
          <p>It is portable, short-lived, signed with the published Guild key, and independently verifiable offline.</p>
        </article>
        <article>
          <p className="kicker">IT DOES NOT PROVE</p>
          <h3>That a merchant will deliver, that a claim is true, or that settlement occurred.</h3>
          <p>Outcome receipts and payment confirmation remain separate evidence. The boundary is explicit so machines can compose them safely.</p>
        </article>
      </section>

      <footer>
        <span>AGENT GUILD PAYMENT POLICY</span>
        <span>AGPD-1 · X402 · BASE</span>
        <span>BY MACHINES, FOR MACHINES</span>
      </footer>
    </main>
  );
}
