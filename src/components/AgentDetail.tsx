import { useGuild } from "../lib/store";
import { ARCHETYPE_LABEL, trustColor, shortDid, pct } from "../ui/format";
import { evaluateMint } from "../lib/badges";

/** Detail panel for the selected agent: the "machine CV". */
export function AgentDetail() {
  const { guild, derived, selectedId, mint, lastMint } = useGuild();
  if (!selectedId) {
    return (
      <div className="panel">
        <div className="empty">Select an agent from the directory to view its profile.</div>
      </div>
    );
  }
  const agent = guild.agents.find((a) => a.id === selectedId)!;
  const score = derived.scores.get(agent.id)!;
  const flag = derived.flags.get(agent.id);
  const tasks = guild.tasks.filter((t) => t.agentId === agent.id);
  const received = guild.attestations
    .filter((a) => a.subjectId === agent.id)
    .sort((a, b) => b.step - a.step);
  const badges = guild.badges.filter((b) => b.subjectId === agent.id);
  const isSeed = guild.seedAgentIds.includes(agent.id);
  const handleOf = (id: string) => guild.agents.find((a) => a.id === id)?.handle ?? id;

  const evaln = evaluateMint(agent, score, flag, guild.tasks, guild.attestations);
  const mintResult = lastMint?.agentId === agent.id ? lastMint : null;

  return (
    <div className="panel">
      <h2>
        {agent.handle}{" "}
        <span className={`pill ${agent.archetype}`}>{ARCHETYPE_LABEL[agent.archetype]}</span>
        {isSeed && <span className="pill seed" style={{ marginLeft: 6 }}>pre-trusted seed</span>}
      </h2>

      {/* Identity */}
      <h3>Identity (DID)</h3>
      <div className="kv">
        <span className="k">DID</span>
        <span className="mono" title={agent.did}>{shortDid(agent.did, 20)}</span>
        <span className="k">Public key</span>
        <span className="mono">{agent.keys.publicKeyHex.slice(0, 24)}…</span>
        <span className="k">Domains</span>
        <span>{agent.domains.join(", ")}</span>
        <span className="k">Joined</span>
        <span>round {agent.createdAtStep}</span>
      </div>

      {/* Reputation score */}
      <h3>Reputation</h3>
      <div className="scorebox">
        <div className="bigscore" style={{ color: trustColor(score.trust) }}>
          {score.trust.toFixed(1)}
          <small> / 100 · rank #{score.rank}</small>
        </div>
      </div>
      <div className="breakdown">
        <Meter label="Recursive trust (EigenTrust)" value={score.eigenTrust / maxEigen(derived)} />
        <Meter label="Reviewer-weighted quality" value={score.weightedQuality} />
        <Meter label="Endorsement accuracy" value={score.endorsementAccuracy} />
        <Meter label="Evidence confidence" value={score.confidence} />
        <div className="row">
          <span>Collusion penalty</span>
          <span style={{ color: score.collusionPenalty > 0.4 ? "var(--bad)" : "var(--muted)" }}>
            −{pct(score.collusionPenalty)}
          </span>
        </div>
      </div>

      {/* Collusion warning */}
      {flag && flag.suspicion > 0.2 && (
        <div className="warning">
          <strong>⚠ Suspected collusion / Sybil activity ({pct(flag.suspicion)})</strong>
          <ul>
            {flag.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Badges */}
      <h3>Soulbound credentials</h3>
      {badges.length === 0 && <div className="muted small">No badge minted yet.</div>}
      {badges.map((b) => (
        <div key={b.id} className={`badge ${b.tier}`}>
          <span className="tier">{b.tier.toUpperCase()}</span>
          <span>{b.label}</span>
          <span className={b.verified ? "verified" : "unverified"}>
            {b.verified ? "✓ verified" : "✗ invalid"}
          </span>
        </div>
      ))}

      {/* Mint flow */}
      <h3>Mint credential</h3>
      <div className="small muted" style={{ marginBottom: 8 }}>
        {evaln.reasons.join(" ")}
      </div>
      <button
        className="primary"
        disabled={!evaln.eligible}
        onClick={() => mint(agent.id)}
      >
        {evaln.eligible ? `Mint ${evaln.tier?.toUpperCase()} badge` : "Not eligible to mint"}
      </button>
      {mintResult && (
        <div className={`warning ${mintResult.ok ? "ok" : ""}`} style={{ marginTop: 10 }}>
          {mintResult.message}
        </div>
      )}

      {/* Task history */}
      <h3>Task history ({tasks.length})</h3>
      <div className="list">
        {tasks
          .slice()
          .reverse()
          .map((t) => (
            <div className="item" key={t.id}>
              <span>{t.title}</span>
              <span className="muted">round {t.step}</span>
            </div>
          ))}
        {tasks.length === 0 && <div className="item muted">No tasks.</div>}
      </div>

      {/* Attestations received */}
      <h3>Attestations received ({received.length})</h3>
      <div className="list">
        {received.map((a) => (
          <div className="item" key={a.id}>
            <span>
              from <strong>{handleOf(a.reviewerId)}</strong>{" "}
              <span className="muted">· rating {(a.rating * 5).toFixed(1)}/5</span>
            </span>
            <span className={a.verified ? "verified" : "unverified"}>
              {a.verified ? "✓ signed" : "✗ invalid"}
            </span>
          </div>
        ))}
        {received.length === 0 && <div className="item muted">No attestations.</div>}
      </div>
    </div>
  );
}

function Meter({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(1, value));
  return (
    <div className="row">
      <span>{label}</span>
      <span className="meter">
        <i style={{ width: `${v * 100}%` }} />
      </span>
      <span className="muted">{pct(v)}</span>
    </div>
  );
}

function maxEigen(derived: { scores: Map<string, { eigenTrust: number }> }): number {
  let m = 1e-12;
  for (const s of derived.scores.values()) m = Math.max(m, s.eigenTrust);
  return m;
}
