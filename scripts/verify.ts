// ---------------------------------------------------------------------------
// Headless smoke test: runs the simulation + scoring in Node and prints a
// report. Also checks that (a) tampering breaks signature verification and
// (b) the reputation system separates honest agents from bad/colluding ones.
//   Run:  npm run verify
// ---------------------------------------------------------------------------
import { runSimulation, DEFAULT_SIM } from "../src/lib/simulation";
import { scoreAgents } from "../src/lib/reputation";
import { verifyCredential } from "../src/lib/vc";
import { evaluateMint } from "../src/lib/badges";
import { initMarket, runTransactions, feeOf, FEE_BPS } from "../src/lib/marketplace";
import type { Archetype } from "../src/lib/types";

const guild = runSimulation(DEFAULT_SIM);
const { scores, flags } = scoreAgents(guild.agents, guild.attestations, guild.seedAgentIds);

console.log("=".repeat(78));
console.log("AGENT GUILD — verification report");
console.log("=".repeat(78));
console.log(
  `Agents: ${guild.agents.length} | Tasks: ${guild.tasks.length} | ` +
    `Attestations: ${guild.attestations.length} | Seeds: ${guild.seedAgentIds.length}`,
);

// 1. All attestations should verify cryptographically.
const verifiedCount = guild.attestations.filter((a) => a.verified).length;
console.log(`\n[1] Signature verification: ${verifiedCount}/${guild.attestations.length} valid`);
console.assert(verifiedCount === guild.attestations.length, "FAIL: some signatures invalid");

// 2. Tampering must break verification.
const sample = guild.attestations[0];
const tampered = {
  ...sample.credential,
  credentialSubject: { ...sample.credential.credentialSubject, rating: 1 },
};
const stillValid = verifyCredential(tampered);
console.log(`[2] Tampered credential verifies? ${stillValid}  (expected: false)`);
console.assert(stillValid === false, "FAIL: tamper not detected");

// 3. Ranking table.
const rows = guild.agents
  .map((a) => ({ a, s: scores.get(a.id)!, f: flags.get(a.id) }))
  .sort((x, y) => x.s.rank - y.s.rank);

console.log("\n[3] Ranked directory (top to bottom):");
console.log(
  "rank  trust  conf  susp  archetype     handle",
);
for (const { a, s, f } of rows) {
  console.log(
    `${String(s.rank).padStart(4)}  ${s.trust.toFixed(1).padStart(5)}  ` +
      `${s.confidence.toFixed(2)}  ${(f?.suspicion ?? 0).toFixed(2)}  ` +
      `${a.archetype.padEnd(12)}  ${a.handle}${guild.seedAgentIds.includes(a.id) ? "  [SEED]" : ""}`,
  );
}

// 4. Aggregate: mean trust by archetype (system should rank honest > others).
const meanBy = (arch: Archetype) => {
  const xs = rows.filter((r) => r.a.archetype === arch).map((r) => r.s.trust);
  return xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : NaN;
};
console.log("\n[4] Mean trust by archetype:");
for (const arch of ["honest", "newcomer", "incompetent", "colluder", "sybil"] as Archetype[]) {
  console.log(`   ${arch.padEnd(12)} ${meanBy(arch).toFixed(1)}`);
}
console.assert(meanBy("honest") > meanBy("incompetent"), "FAIL: honest !> incompetent");
console.assert(meanBy("honest") > meanBy("colluder"), "FAIL: honest !> colluder");

// 5. Collusion detection: are ring/sybil members flagged?
const flaggedColluders = rows.filter(
  (r) => (r.a.archetype === "colluder" || r.a.archetype === "sybil") && (r.f?.suspicion ?? 0) > 0.4,
).length;
const totalBadActors = rows.filter(
  (r) => r.a.archetype === "colluder" || r.a.archetype === "sybil",
).length;
console.log(`\n[5] Collusion/Sybil flagged: ${flaggedColluders}/${totalBadActors} (suspicion > 0.4)`);

// 6. Minting: who qualifies?
const eligible = rows.filter((r) => {
  const e = evaluateMint(r.a, r.s, r.f, guild.tasks, guild.attestations);
  return e.eligible;
});
console.log(`\n[6] Badge-eligible agents: ${eligible.length}`);
for (const r of eligible) {
  const e = evaluateMint(r.a, r.s, r.f, guild.tasks, guild.attestations);
  console.log(`   ${r.a.handle.padEnd(20)} ${e.tier?.toUpperCase()}  (${r.a.archetype})`);
}

// 7. Marketplace: run transactions and check fee math + attestation feedback.
console.log("\n[7] Marketplace simulation (200 transactions):");
const trustOf = (id: string) => scores.get(id)?.trust ?? 0;
const market0 = initMarket(guild.agents);
const N = 200;
const { market, tasks: mtasks, attestations: matts } = runTransactions(
  market0,
  guild.agents,
  trustOf,
  N,
  guild.step,
  0,
  123,
);
console.log(`   settled: ${market.totals.count} | volume: ${market.totals.volume.toFixed(0)} | revenue: ${market.totals.fees.toFixed(3)}`);
const expectedFees = Math.round(market.totals.volume * (FEE_BPS / 10000) * 1000) / 1000;
const effRate = (market.totals.fees / market.totals.volume) * 100;
console.log(`   effective fee rate: ${effRate.toFixed(3)}%  (target ${FEE_BPS / 100}%)`);
console.log(`   treasury == fees: ${Math.abs(market.treasury - market.totals.fees) < 1e-6}`);
console.log(`   per-tx fee example: 200 credits → ${feeOf(200).toFixed(3)} fee`);
console.assert(Math.abs(effRate - FEE_BPS / 100) < 0.02, "FAIL: effective fee rate off target");
console.assert(Math.abs(market.treasury - market.totals.fees) < 1e-6, "FAIL: treasury != fees");
console.assert(Math.abs(market.totals.fees - expectedFees) < 0.5, "FAIL: fee total mismatch");

// Attestations from settlement must all be cryptographically valid.
const validMatts = matts.filter((a) => a.verified).length;
console.log(`   settlement attestations valid: ${validMatts}/${matts.length}`);
console.assert(validMatts === matts.length, "FAIL: invalid settlement attestation");

// Feed marketplace attestations back into reputation; honest should stay on top.
const fedAttestations = [...guild.attestations, ...matts];
const { scores: scores2 } = scoreAgents(guild.agents, fedAttestations, guild.seedAgentIds);
const meanByFed = (arch: Archetype) => {
  const xs = guild.agents.filter((a) => a.archetype === arch).map((a) => scores2.get(a.id)!.trust);
  return xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : NaN;
};
console.log(
  `   post-market mean trust — honest ${meanByFed("honest").toFixed(1)}, ` +
    `colluder ${meanByFed("colluder").toFixed(1)}, incompetent ${meanByFed("incompetent").toFixed(1)}`,
);
console.assert(meanByFed("honest") > meanByFed("incompetent"), "FAIL: honest !> incompetent post-market");
console.log(`   new tasks recorded: ${mtasks.length}`);

console.log("\nDone.");
