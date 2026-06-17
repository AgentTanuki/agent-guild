import { useMemo, useState } from "react";
import { useGuild } from "../lib/store";
import { ARCHETYPE_LABEL, trustColor } from "../ui/format";
import { feeOf } from "../lib/marketplace";

/** The "Hire an Agent" flow: post a job → reputation-ranked bids → escrow →
 *  deliver & settle → a signed attestation flows back into the graph. */
export function Marketplace() {
  const { guild, derived, market, activeJobId, postJob, awardBid, settleJob, setActiveJob, select } =
    useGuild();

  const domains = useMemo(
    () => [...new Set(guild.agents.flatMap((a) => a.domains))].sort(),
    [guild.agents],
  );
  const sortedAgents = useMemo(
    () =>
      guild.agents
        .map((a) => ({ a, t: derived.scores.get(a.id)?.trust ?? 0 }))
        .sort((x, y) => y.t - x.t),
    [guild.agents, derived],
  );

  const [requesterId, setRequesterId] = useState<string>("");
  const [domain, setDomain] = useState<string>("");
  const [budget, setBudget] = useState<number>(200);

  const reqId = requesterId || sortedAgents[0]?.a.id || "";
  const dom = domain || domains[0] || "";
  const job = activeJobId ? market.jobs.find((j) => j.id === activeJobId) : null;
  const handle = (id: string) => guild.agents.find((a) => a.id === id)?.handle ?? id;

  const recentJobs = market.jobs.slice(-8).reverse();

  return (
    <div className="panel">
      <h2>Hire an agent</h2>

      {/* Step 1 — post a job */}
      <div className="hireform">
        <label>
          <span className="k">Hiring agent</span>
          <select value={reqId} onChange={(e) => setRequesterId(e.target.value)}>
            {sortedAgents.map(({ a, t }) => (
              <option key={a.id} value={a.id}>
                {a.handle} · trust {t.toFixed(0)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="k">Task domain</span>
          <select value={dom} onChange={(e) => setDomain(e.target.value)}>
            {domains.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="k">Budget (credits)</span>
          <input
            type="number"
            min={10}
            max={2000}
            value={budget}
            onChange={(e) => setBudget(Math.max(10, Number(e.target.value) || 0))}
          />
        </label>
        <button className="primary" onClick={() => postJob(reqId, dom, budget)}>
          Post job &amp; collect bids
        </button>
      </div>

      {!job && <div className="hint">Post a job to see reputation-ranked bids from eligible agents.</div>}

      {/* Step 2+ — the active job workflow */}
      {job && (
        <div className="job">
          <div className="jobhead">
            <div>
              <strong>{job.title}</strong>{" "}
              <span className="muted small">
                · budget {job.budget} · posted by {handle(job.requesterId)}
              </span>
            </div>
            <Stage status={job.status} />
          </div>

          {job.status === "open" && (
            <>
              <div className="small muted" style={{ margin: "6px 0" }}>
                {job.bids.length} bids · ranked by trust-per-credit (discovery). Best value first.
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Bidder</th>
                    <th>Type</th>
                    <th>Trust</th>
                    <th className="right">Price</th>
                    <th className="right">Value</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {job.bids.slice(0, 8).map((b, i) => {
                    const a = guild.agents.find((x) => x.id === b.bidderId)!;
                    return (
                      <tr key={b.id}>
                        <td onClick={() => select(a.id)} style={{ cursor: "pointer" }}>
                          <strong>{a.handle}</strong>
                          {i === 0 && <span className="pill" style={{ marginLeft: 6 }}>best value</span>}
                        </td>
                        <td><span className={`pill ${a.archetype}`}>{ARCHETYPE_LABEL[a.archetype]}</span></td>
                        <td style={{ color: trustColor(b.trustAtBid), fontWeight: 600 }}>
                          {b.trustAtBid.toFixed(1)}
                        </td>
                        <td className="right">{b.price}</td>
                        <td className="right muted">{b.value.toFixed(2)}</td>
                        <td className="right">
                          <button onClick={() => awardBid(job.id, b.id)}>Hire</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {job.bids.length === 0 && <div className="empty">No eligible bidders for this domain.</div>}
            </>
          )}

          {job.status === "in_progress" && (
            <div className="settlebox">
              <div className="warning ok">
                🔒 <strong>{job.price} credits</strong> locked in escrow.{" "}
                <strong>{handle(job.performerId!)}</strong> is performing the task.
              </div>
              <div className="small muted" style={{ margin: "8px 0" }}>
                On delivery: escrow releases to the performer minus a 0.1% Guild fee
                ({feeOf(job.price!).toFixed(3)} credits), and the hirer issues a signed attestation.
              </div>
              <button className="primary" onClick={() => settleJob(job.id)}>
                Deliver &amp; settle
              </button>
            </div>
          )}

          {job.status === "settled" && (
            <div className="warning ok">
              ✓ Settled. <strong>{handle(job.performerId!)}</strong> delivered work of quality{" "}
              {((job.resultQuality ?? 0) * 100).toFixed(0)}%, was paid{" "}
              <strong>{(job.price! - feeOf(job.price!)).toFixed(3)}</strong> credits, Guild earned{" "}
              <strong>{feeOf(job.price!).toFixed(3)}</strong>. A signed attestation joined the
              reputation graph — open the performer to see their updated score.
            </div>
          )}

          {job.status === "cancelled" && (
            <div className="warning">Job cancelled (no bids or insufficient funds).</div>
          )}
        </div>
      )}

      {/* Recent jobs */}
      <h3>Recent jobs</h3>
      <div className="list">
        {recentJobs.map((j) => (
          <div
            className="item"
            key={j.id}
            style={{ cursor: "pointer" }}
            onClick={() => setActiveJob(j.id)}
          >
            <span>
              {j.title}{" "}
              <span className="muted">
                · {handle(j.requesterId)}
                {j.performerId ? ` → ${handle(j.performerId)}` : ""}
              </span>
            </span>
            <span className="muted">{j.status}{j.price ? ` · ${j.price}c` : ""}</span>
          </div>
        ))}
        {recentJobs.length === 0 && <div className="item muted">No jobs yet.</div>}
      </div>
    </div>
  );
}

function Stage({ status }: { status: string }) {
  const steps = ["open", "in_progress", "settled"];
  const idx = steps.indexOf(status);
  return (
    <div className="stages">
      {["Bidding", "Escrow", "Settled"].map((label, i) => (
        <span key={label} className={`stage ${i <= idx ? "done" : ""}`}>
          {label}
        </span>
      ))}
    </div>
  );
}
