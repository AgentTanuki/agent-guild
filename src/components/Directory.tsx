import { useGuild } from "../lib/store";
import { ARCHETYPE_LABEL, trustColor } from "../ui/format";

/** The marketplace / directory: agents ranked by trust score. */
export function Directory() {
  const { guild, derived, selectedId, select } = useGuild();
  const rows = guild.agents
    .map((a) => ({
      agent: a,
      score: derived.scores.get(a.id)!,
      flag: derived.flags.get(a.id),
    }))
    .sort((x, y) => x.score.rank - y.score.rank);

  return (
    <div className="panel">
      <h2>Agent Directory — ranked by trust</h2>
      <table>
        <thead>
          <tr>
            <th style={{ width: 36 }}>#</th>
            <th>Agent</th>
            <th>Type</th>
            <th>Trust</th>
            <th className="right">Conf.</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ agent, score, flag }) => {
            const isSeed = guild.seedAgentIds.includes(agent.id);
            const badge = guild.badges.find((b) => b.subjectId === agent.id);
            const suspicious = (flag?.suspicion ?? 0) > 0.4;
            return (
              <tr
                key={agent.id}
                className={selectedId === agent.id ? "selected" : ""}
                onClick={() => select(agent.id)}
              >
                <td className="muted">{score.rank}</td>
                <td>
                  <strong>{agent.handle}</strong>
                  {isSeed && <span className="pill seed" style={{ marginLeft: 6 }}>seed</span>}
                  {badge && (
                    <span className="pill" style={{ marginLeft: 6 }}>
                      {badge.tier} badge
                    </span>
                  )}
                </td>
                <td>
                  <span className={`pill ${agent.archetype}`}>
                    {ARCHETYPE_LABEL[agent.archetype]}
                  </span>
                </td>
                <td>
                  <span className="trustbar">
                    <i style={{ width: `${score.trust}%`, background: trustColor(score.trust) }} />
                  </span>{" "}
                  <span style={{ color: trustColor(score.trust), fontWeight: 600 }}>
                    {score.trust.toFixed(1)}
                  </span>
                </td>
                <td className="right muted">{Math.round(score.confidence * 100)}%</td>
                <td>
                  {suspicious ? (
                    <span className="warn-badge" title={flag?.reasons.join(" ")}>
                      ⚠ collusion {Math.round((flag?.suspicion ?? 0) * 100)}%
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="hint">
        Click any agent to inspect identity, task history, attestations and the mint flow.
      </div>
    </div>
  );
}
