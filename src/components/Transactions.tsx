import { useMemo } from "react";
import { useGuild } from "../lib/store";
import { FEE_BPS } from "../lib/marketplace";

/** Transaction history + revenue dashboard: how Guild fees scale with volume. */
export function Transactions() {
  const { market, guild, simulateTransactions } = useGuild();
  const handle = (id?: string) =>
    id === "guild-treasury" ? "Guild treasury" : guild.agents.find((a) => a.id === id)?.handle ?? id ?? "—";

  // Cumulative fee curve over settled transactions (fee ledger entries in order).
  const series = useMemo(() => {
    const pts: { i: number; volume: number; fees: number }[] = [];
    let volume = 0;
    let fees = 0;
    let i = 0;
    for (const e of market.ledger) {
      if (e.type === "payment_release") volume += findJobPrice(market, e.jobId);
      if (e.type === "fee") {
        fees += e.amount;
        i += 1;
        pts.push({ i, volume, fees });
      }
    }
    return pts;
  }, [market]);

  const t = market.totals;
  const effectiveRate = t.volume > 0 ? (t.fees / t.volume) * 100 : 0;
  const recent = market.ledger.slice(-14).reverse();

  return (
    <div className="panel">
      <h2 style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>Revenue &amp; transactions</span>
        <span style={{ display: "flex", gap: 6 }}>
          <button onClick={() => simulateTransactions(10)}>+10 txns</button>
          <button onClick={() => simulateTransactions(50)}>+50 txns</button>
          <button className="primary" onClick={() => simulateTransactions(200)}>+200 txns</button>
        </span>
      </h2>

      <div className="stats">
        <Stat k="Transactions" v={t.count} />
        <Stat k="Volume (credits)" v={Math.round(t.volume).toLocaleString()} />
        <Stat k="Guild revenue" v={t.fees.toFixed(2)} accent />
        <Stat k="Fee rate" v={`${effectiveRate.toFixed(2)}%`} />
        <Stat k="Avg fee / txn" v={t.count ? (t.fees / t.count).toFixed(3) : "0"} />
        <Stat k="Treasury" v={market.treasury.toFixed(2)} accent />
      </div>

      <h3>Cumulative Guild revenue vs volume</h3>
      {series.length < 2 ? (
        <div className="hint">
          Run some transactions to chart how revenue scales. The Guild takes {FEE_BPS / 100}% of every
          settled transaction — revenue is a linear function of marketplace volume.
        </div>
      ) : (
        <RevenueChart series={series} />
      )}

      <h3>Fee at scale (0.1% of volume)</h3>
      <table>
        <thead>
          <tr><th>Monthly marketplace volume</th><th className="right">Guild revenue / month</th><th className="right">/ year</th></tr>
        </thead>
        <tbody>
          {[100_000, 1_000_000, 10_000_000, 100_000_000].map((v) => (
            <tr key={v}>
              <td>{v.toLocaleString()} credits</td>
              <td className="right">{(v * 0.001).toLocaleString()} </td>
              <td className="right">{(v * 0.001 * 12).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="hint">
        The fee is deliberately tiny (0.1%) so using the Guild is cheaper than the expected loss from
        hiring an unvetted agent. Revenue is a pure function of volume — the incentive is to grow the
        transaction graph, which is the same thing as growing the moat.
      </div>

      <h3>Transaction history</h3>
      <div className="list">
        {recent.map((e) => (
          <div className="item" key={e.id}>
            <span>
              <span className={`txtype ${e.type}`}>{labelFor(e.type)}</span>{" "}
              {e.label}
              <span className="muted">
                {" "}· {handle(e.from)}{e.to ? ` → ${handle(e.to)}` : ""}
              </span>
            </span>
            <span className={e.type === "fee" ? "verified" : "muted"}>
              {e.type === "fee" ? "+" : ""}{e.amount.toFixed(3)}c
            </span>
          </div>
        ))}
        {recent.length === 0 && <div className="item muted">No transactions yet.</div>}
      </div>
    </div>
  );
}

function labelFor(type: string): string {
  switch (type) {
    case "escrow_lock": return "ESCROW";
    case "payment_release": return "PAYMENT";
    case "fee": return "FEE";
    case "refund": return "REFUND";
    default: return type.toUpperCase();
  }
}

function findJobPrice(market: { jobs: { id: string; price?: number }[] }, jobId: string): number {
  return market.jobs.find((j) => j.id === jobId)?.price ?? 0;
}

function Stat({ k, v, accent }: { k: string; v: string | number; accent?: boolean }) {
  return (
    <div className="stat">
      <div className="k">{k}</div>
      <div className="v" style={{ color: accent ? "var(--good)" : undefined }}>{v}</div>
    </div>
  );
}

function RevenueChart({ series }: { series: { i: number; volume: number; fees: number }[] }) {
  const W = 760;
  const H = 240;
  const pad = { l: 54, r: 16, t: 12, b: 28 };
  const maxFee = Math.max(...series.map((s) => s.fees), 1);
  const maxI = Math.max(...series.map((s) => s.i), 1);
  const x = (i: number) => pad.l + (i / maxI) * (W - pad.l - pad.r);
  const y = (f: number) => H - pad.b - (f / maxFee) * (H - pad.t - pad.b);
  const path = series.map((s, k) => `${k === 0 ? "M" : "L"}${x(s.i).toFixed(1)},${y(s.fees).toFixed(1)}`).join(" ");
  const area = `${path} L${x(maxI).toFixed(1)},${(H - pad.b).toFixed(1)} L${x(0).toFixed(1)},${(H - pad.b).toFixed(1)} Z`;

  const yticks = 4;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H, background: "var(--panel)" }}>
      {Array.from({ length: yticks + 1 }, (_, k) => {
        const fv = (maxFee / yticks) * k;
        const yy = y(fv);
        return (
          <g key={k}>
            <line x1={pad.l} y1={yy} x2={W - pad.r} y2={yy} stroke="var(--border)" strokeWidth={1} />
            <text x={pad.l - 8} y={yy + 3} textAnchor="end" fontSize={10} fill="var(--muted)">
              {fv.toFixed(1)}
            </text>
          </g>
        );
      })}
      <path d={area} fill="rgba(52,211,153,0.12)" />
      <path d={path} fill="none" stroke="var(--good)" strokeWidth={2} />
      <text x={pad.l} y={H - 8} fontSize={10} fill="var(--muted)">transactions →</text>
      <text x={10} y={pad.t + 4} fontSize={10} fill="var(--muted)" transform={`rotate(-90 10 ${H / 2})`}>
        cumulative revenue (credits)
      </text>
    </svg>
  );
}
