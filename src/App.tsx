import { useEffect, useState } from "react";
import { useGuild } from "./lib/store";
import { Directory } from "./components/Directory";
import { AgentDetail } from "./components/AgentDetail";
import { TrustGraph } from "./components/TrustGraph";
import { Marketplace } from "./components/Marketplace";
import { Transactions } from "./components/Transactions";
import { GUILD_DID } from "./lib/badges";
import { shortDid } from "./ui/format";

type Tab = "directory" | "market" | "revenue" | "graph" | "about";

export function App() {
  const { guild, derived, market, initialize, tamperRandomAttestation, recompute } = useGuild();
  const [tab, setTab] = useState<Tab>("directory");

  useEffect(() => {
    initialize();
  }, [initialize]);

  if (guild.agents.length === 0) {
    return <div className="app"><div className="empty">Spinning up the guild…</div></div>;
  }

  const flagged = [...derived.flags.values()].filter((f) => f.suspicion > 0.4).length;
  const verified = guild.attestations.filter((a) => a.verified).length;

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>🛡 Agent Guild</h1>
          <div className="tagline">
            Portable, cryptographic reputation for AI agents — the token is just the container.
          </div>
        </div>
        <div className="small muted">
          Guild authority: <span className="mono">{shortDid(GUILD_DID, 16)}</span>
        </div>
      </header>

      <div className="stats">
        <Stat k="Agents" v={guild.agents.length} />
        <Stat k="Tasks" v={guild.tasks.length} />
        <Stat k="Attestations" v={guild.attestations.length} />
        <Stat k="Signatures valid" v={`${verified}/${guild.attestations.length}`} />
        <Stat k="Badges minted" v={guild.badges.length} />
        <Stat k="Flagged" v={flagged} warn={flagged > 0} />
        <Stat k="Guild revenue" v={`${market.totals.fees.toFixed(2)}c`} />
      </div>

      <div className="toolbar">
        <button onClick={() => initialize()}>↻ Re-run simulation</button>
        <button onClick={() => { tamperRandomAttestation(); }}>
          ✎ Tamper with an attestation
        </button>
        <button onClick={() => recompute()}>Recompute scores</button>
      </div>

      <div className="tabs">
        <div className={`tab ${tab === "directory" ? "active" : ""}`} onClick={() => setTab("directory")}>
          Directory
        </div>
        <div className={`tab ${tab === "market" ? "active" : ""}`} onClick={() => setTab("market")}>
          Hire / marketplace
        </div>
        <div className={`tab ${tab === "revenue" ? "active" : ""}`} onClick={() => setTab("revenue")}>
          Revenue
        </div>
        <div className={`tab ${tab === "graph" ? "active" : ""}`} onClick={() => setTab("graph")}>
          Trust graph
        </div>
        <div className={`tab ${tab === "about" ? "active" : ""}`} onClick={() => setTab("about")}>
          How it works
        </div>
      </div>

      {tab === "about" ? (
        <About />
      ) : tab === "revenue" ? (
        <Transactions />
      ) : (
        <div className="layout">
          {tab === "directory" ? <Directory /> : tab === "market" ? <Marketplace /> : <TrustGraph />}
          <AgentDetail />
        </div>
      )}
    </div>
  );
}

function Stat({ k, v, warn }: { k: string; v: string | number; warn?: boolean }) {
  return (
    <div className="stat">
      <div className="k">{k}</div>
      <div className="v" style={{ color: warn ? "var(--bad)" : undefined }}>{v}</div>
    </div>
  );
}

function About() {
  return (
    <div className="panel" style={{ maxWidth: 820, lineHeight: 1.6 }}>
      <h2>How Agent Guild works</h2>
      <p className="small">
        Every agent owns a <strong>persistent decentralized identity</strong> (a{" "}
        <span className="mono">did:key</span> backed by a real ed25519 keypair). When an agent
        completes a task, other agents review it and issue a{" "}
        <strong>signed W3C Verifiable Credential</strong> — a cryptographically non-repudiable
        attestation of work quality. The directory shows the reputation that emerges from these
        attestations.
      </p>
      <h3>Reputation scoring</h3>
      <p className="small">
        Scores are computed with an <strong>EigenTrust-style recursive algorithm</strong>: trust
        flows outward from a small set of pre-trusted seed agents, so attestations from already-
        trusted agents count more. On top of that we layer reviewer-weighted quality, an{" "}
        <strong>endorsement-accuracy penalty</strong> (rubber-stamping bad work costs you), a{" "}
        <strong>collusion penalty</strong>, and <strong>confidence shrinkage</strong> so thin
        evidence (new or Sybil identities) can't shortcut to the top.
      </p>
      <h3>Sybil &amp; collusion resistance</h3>
      <p className="small">
        Because trust must originate from seeds, a ring of agents endorsing each other cannot
        manufacture standing in a vacuum. A structural detector additionally flags{" "}
        <strong>reciprocal rings</strong> that point most endorsements inward, inflate each other
        above outside consensus, or attract little external validation. Flagged agents are blocked
        from minting.
      </p>
      <h3>Soulbound credentials</h3>
      <p className="small">
        Once an agent clears trust / task / distinct-reviewer thresholds, the Guild authority issues
        a <strong>non-transferable</strong> badge — a Verifiable Credential bound to the agent's DID
        with no transfer semantics. It is a portable machine CV, not a tradeable asset. (ERC-6551-
        style token-bound accounts are a natural future home for this credential's on-chain history.)
      </p>
      <h3>Try it</h3>
      <p className="small">
        Use <strong>Tamper with an attestation</strong> to corrupt a signed credential — it
        immediately fails verification, stops counting toward reputation, and scores recompute.
        Open a colluder or Sybil agent to see the warning panel, and a top honest agent to mint a
        badge.
      </p>
      <p className="small muted">
        This is a local prototype: no blockchain, no network. All signing and verification happen
        in your browser.
      </p>
    </div>
  );
}
