import fs from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const expansionDir = path.join(root, "artifacts/community-expansion-2026-08-25");
const pilotTargetsPath = path.join(root, "artifacts/community-pilot-2026-08-25/targets.json");

const sourceFiles = [
  ["lead_independent_audit", "lead-independent-audit.json", 100],
  ["claude_protocol_markets", "claude-protocol-markets.json", 80],
  ["claude_social_spaces", "claude-social-spaces.json", 70],
  ["claude_framework_onchain_worlds", "claude-framework-onchain-worlds.json", 60]
];

const classOrder = {
  qualifies_high: 5,
  qualifies_gated: 4,
  probationary: 3,
  watch: 2,
  reject: 1
};

const finalClass = {
  // Social, messaging and labour spaces.
  sociobot: "qualifies_high",
  krawler: "qualifies_gated",
  moye: "probationary",
  rine: "qualifies_gated",
  agenc: "qualifies_high",
  "ai-gram": "qualifies_gated",
  "tiny-place": "qualifies_gated",
  youam: "qualifies_high",
  "agent-republic": "qualifies_gated",
  "nostr-nip90-dvms": "qualifies_high",
  buzz: "qualifies_gated",
  "said-protocol": "qualifies_gated",
  "agentgram-professional": "probationary",
  "agentgram-visual": "qualifies_high",
  "agentx-market": "probationary",
  agentum: "probationary",
  "okx-ai": "watch",
  "near-agent-market": "watch",
  signet: "probationary",
  openclab: "probationary",
  botverse: "watch",
  "nexus-0": "probationary",
  matrixagentnet: "probationary",
  "circuit-ai": "probationary",
  airc: "probationary",
  "screeps-world": "qualifies_gated",
  botzone: "qualifies_gated",
  "openclaw-arena": "qualifies_gated",
  "glee-competition": "qualifies_gated",
  bittensor: "qualifies_gated",
  allora: "qualifies_gated",
  "gaia-network": "qualifies_gated",
  "wikidata-bots": "qualifies_gated",
  "openstreetmap-automated-edits": "qualifies_gated",
  zilligon: "watch",

  // Framework-hosted hubs, worlds and machine economies.
  band: "qualifies_gated",
  "tournamental-open-bot-arena": "qualifies_gated",
  "null-epoch": "probationary",
  agentleague: "qualifies_high",
  einsteinarena: "qualifies_gated",
  "kamibench-kamigotchi": "qualifies_gated",
  spacetraders: "qualifies_gated",
  "aeterna-open-ai-collaboration-platform": "probationary",
  "otra-city": "probationary",
  agentstown: "probationary",
  chirper: "qualifies_gated",
  "delysium-ykily-ai-agent-network": "probationary",
  "holoworld-agent-market": "qualifies_gated",
  "myshell-aipp-store": "probationary",
  "creator-bid-agent-battles": "qualifies_gated",
  "morpheus-compute-network": "probationary",
  "singularitynet-ai-marketplace": "probationary",
  "robonomics-network": "probationary",
  "peaq-machine-economy": "probationary",
  "ai-arena-nrn": "qualifies_gated",
  robocup: "watch",
  agentown: "watch",
  "agent-sports-league": "probationary",
  "wayfinder-shells-and-paths": "probationary",
  "kite-agent-passport-ecosystem": "watch",
  "do-agent-teams": "reject",
  theoriq: "watch",

  // Protocol, registry, commerce and compute passes. A large directory is not
  // promoted unless machines actually share identity, interaction or value.
  "erc-8004-8004scan": "qualifies_gated",
  "hol-universal-agentic-registry": "qualifies_gated",
  "x402-foundation-ecosystem": "qualifies_high",
  x402scan: "qualifies_high",
  "agentic-resource-discovery": "probationary",
  payanagent: "qualifies_high",
  "machine-payments-protocol-services": "probationary",
  glama: "reject",
  "allora-network-forge": "qualifies_gated",
  "docker-mcp-catalog": "reject",
  pulsemcp: "reject",
  smithery: "reject",
  "github-agent-finder": "reject",
  "hugging-face-discover": "reject",
  "mcp-so": "reject",
  "modelscope-mcp-plaza": "reject",
  "chaoschain-proof-of-agency": "probationary",
  "universal-commerce-protocol": "watch",
  ap2: "watch",
  "akash-network": "probationary",
  "render-network": "probationary",
  "io-net": "probationary",
  "fluence-gpu-marketplace": "probationary",
  "skyfire-service-directory": "probationary",
  "ampersend-marketplace": "watch",
  the402: "probationary",
  "stripe-agentic-commerce-protocol": "watch",
  "mastercard-agent-pay-for-machines": "watch",
  "livepeer-ai-network": "probationary"
};

const aliases = {
  "agenc-marketplace": "agenc",
  "rine-network": "rine",
  "ai-gram": "ai-gram",
  "null-epoch": "null-epoch",
  "gaia-decentralized-agent-nodes": "gaia-network",
  "allora-network": "allora",
  "allora-network-forge": "allora",
  "screeps-world": "screeps-world",
  "x402scan": "x402-ecosystem",
  "x402-foundation-ecosystem": "x402-ecosystem",
  "agentgram": "agentgram-professional",
  "chirp-community-edition": "chirp-oss",
  "chirp-oss-community-edition": "chirp-oss",
  "near-ai-agent-market": "near-agent-market",
  "agent-exchange": "agent-exchange",
  "the402-marketplace": "the402",
  "the-agent-registry": "the-agent-registry"
};

const readJson = file => JSON.parse(fs.readFileSync(file, "utf8"));

function slug(value) {
  return String(value || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function stringValue(value) {
  if (Array.isArray(value)) return value.map(stringValue).filter(Boolean).join("; ");
  if (value && typeof value === "object") return value.detail || value.reason || JSON.stringify(value);
  return value == null ? "" : String(value);
}

function sourceUrls(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(item => typeof item === "string" ? item : item?.url).filter(Boolean))];
}

function inferClass(raw, id) {
  if (raw.decision_class) return raw.decision_class;
  if (finalClass[id]) return finalClass[id];
  const confidence = String(raw.confidence || "").toLowerCase();
  const gates = stringValue(raw.blockers_terms_spend_safety || raw.gates_terms_spend_safety || raw.access_join_mechanics || raw.join_access).toLowerCase();
  if (confidence.includes("low")) return "watch";
  if (confidence.includes("medium") && !confidence.includes("high")) return "probationary";
  if (confidence.includes("high")) {
    return /(wallet|account|terms|sign-in|login|required|registration|stake|gas|api key|human)/.test(gates)
      ? "qualifies_gated"
      : "qualifies_high";
  }
  return "probationary";
}

function normalizeCandidate(raw, sourcePass, sourcePriority) {
  const rawId = slug(raw.id || raw.canonical_name || raw.name);
  const id = aliases[rawId] || rawId;
  const explicitClass = raw.decision_class || finalClass[rawId] || finalClass[id];
  const decisionClass = explicitClass || inferClass(raw, rawId);
  const confidence = raw.confidence || (
    decisionClass === "qualifies_high" || decisionClass === "qualifies_gated" || decisionClass === "reject"
      ? "high"
      : decisionClass === "probationary" ? "medium" : "low"
  );
  const sources = sourceUrls(raw.primary_sources || raw.source_urls || raw.sources);
  const currentEvidence = raw.current_evidence || raw.activity_population_evidence || raw.current_activity_population_evidence || raw.current_population_activity_evidence || "";
  const interactionEvidence = raw.community_evidence || raw.why_community || raw.community_qualification || raw.community_type || "";
  const machineSurface = raw.machine_surface || raw.machine_native_surface || raw.machine_native_interface || "";
  const gates = raw.access_gate || raw.gates_terms_spend_safety || raw.blockers_terms_spend_safety || "";

  return {
    id,
    canonical_name: raw.canonical_name || raw.name || raw.id,
    canonical_url: raw.canonical_url || sources[0] || "",
    cohort: "expansion_research",
    decision_class: decisionClass,
    confidence,
    community_type: stringValue(raw.community_type || raw.category),
    durable_identity_and_interaction_evidence: stringValue(interactionEvidence),
    machine_native_interface: stringValue(machineSurface),
    current_activity_population_evidence: stringValue(currentEvidence),
    join_access_mechanics: stringValue(raw.join_access_mechanics || raw.access_join_mechanics || raw.join_access || ""),
    gates_terms_spend_safety: stringValue(gates),
    smallest_useful_guild_action: stringValue(raw.smallest_guild_action || raw.plausible_useful_ag_action || raw.plausible_agent_guild_action || ""),
    machine_expressed_need_or_friction: stringValue(raw.machine_expressed_need_or_friction || ""),
    duplicate_adjacency_notes: stringValue(raw.duplicate_adjacency_notes || raw.adjacency_duplicate_notes || raw.duplicate_notes || ""),
    primary_sources: sources,
    source_passes: [sourcePass],
    source_records_merged: 1,
    _source_priority: sourcePriority,
    _explicit_lead_decision: sourcePass === "lead_independent_audit" && Boolean(raw.decision_class)
  };
}

function mergeRecords(existing, incoming) {
  const incomingLeads = incoming._explicit_lead_decision && !existing._explicit_lead_decision;
  const replace = incomingLeads || (!existing._explicit_lead_decision && incoming._source_priority > existing._source_priority);
  const primary = replace ? incoming : existing;
  const secondary = replace ? existing : incoming;
  const merged = { ...primary };
  for (const field of [
    "community_type",
    "durable_identity_and_interaction_evidence",
    "machine_native_interface",
    "current_activity_population_evidence",
    "join_access_mechanics",
    "gates_terms_spend_safety",
    "smallest_useful_guild_action",
    "machine_expressed_need_or_friction",
    "duplicate_adjacency_notes"
  ]) {
    if (!merged[field] && secondary[field]) merged[field] = secondary[field];
  }
  merged.primary_sources = [...new Set([...primary.primary_sources, ...secondary.primary_sources])];
  merged.source_passes = [...new Set([...primary.source_passes, ...secondary.source_passes])];
  merged.source_records_merged = primary.source_records_merged + secondary.source_records_merged;
  merged._source_priority = Math.max(primary._source_priority, secondary._source_priority);
  merged._explicit_lead_decision = primary._explicit_lead_decision || secondary._explicit_lead_decision;
  return merged;
}

function normalizeReject(raw, sourcePass) {
  const rawId = slug(raw.id || raw.canonical_name || raw.name);
  return {
    id: aliases[rawId] || rawId,
    canonical_name: raw.canonical_name || raw.name || raw.id,
    canonical_url: raw.canonical_url || raw.url || "",
    cohort: "expansion_research",
    decision_class: "reject",
    confidence: "high",
    community_type: "rejected discovery lead",
    durable_identity_and_interaction_evidence: "",
    machine_native_interface: "",
    current_activity_population_evidence: stringValue(raw.reason || raw.disqualification_reason || ""),
    join_access_mechanics: "",
    gates_terms_spend_safety: stringValue(raw.revisit_trigger || ""),
    smallest_useful_guild_action: "None unless the rejection condition changes.",
    machine_expressed_need_or_friction: "",
    duplicate_adjacency_notes: "Discovery lead rejected by the final community qualification standard.",
    primary_sources: sourceUrls(raw.source_urls || raw.sources).concat(raw.canonical_url || raw.url || []).filter(Boolean),
    source_passes: [sourcePass],
    source_records_merged: 1,
    _source_priority: 20,
    _explicit_lead_decision: false
  };
}

const strategicBoost = {
  sociobot: 90,
  payanagent: 85,
  agenc: 85,
  aiim: 80,
  "agent-exchange": 75,
  "the-agent-registry": 70,
  "vertical-marketplace": 70,
  t2000: 70,
  "x402-ecosystem": 65,
  "nostr-nip90-dvms": 60,
  krawler: 55,
  rine: 55,
  "agentgram-visual": 50,
  youam: 50,
  "erc-8004-8004scan": 50,
  "hol-universal-agentic-registry": 50,
  nexagora: 50,
  "concordium-agent-registry": 50,
  masumi: 50,
  "agent-community": 50,
  band: 45,
  "agent-republic": 45,
  "ai-gram": 45,
  "tiny-place": 45,
  agentleague: 40,
  "wikidata-bots": -45,
  "openstreetmap-automated-edits": -45,
  botzone: -35,
  "glee-competition": -35,
  "screeps-world": -35
};

function priorityScore(record) {
  const confidence = String(record.confidence).toLowerCase();
  const confidenceScore = confidence.includes("high") ? 30 : confidence.includes("medium") ? 20 : 10;
  const numericEvidence = /\b\d[\d,.]*\b/.test(record.current_activity_population_evidence) ? 8 : 0;
  const machineNeed = record.machine_expressed_need_or_friction ? 8 : 0;
  const sources = Math.min(record.primary_sources.length, 4) * 2;
  const text = `${record.community_type} ${record.machine_native_interface} ${record.smallest_useful_guild_action}`.toLowerCase();
  let fit = 0;
  if (/(marketplace|commerce|labor|work network|task|service market|settlement|escrow)/.test(text)) fit += 25;
  if (/(social|messag|professional network|community|room|forum)/.test(text)) fit += 20;
  if (/(identity|registry|trust|reputation|passport|credential)/.test(text)) fit += 18;
  if (/(x402|payment)/.test(text)) fit += 12;
  if (/(game|arena|competition|wiki|map|robot|gpu|compute network)/.test(text)) fit -= 18;
  fit += strategicBoost[record.id] || 0;
  return classOrder[record.decision_class] * 100 + confidenceScore + numericEvidence + machineNeed + sources + fit;
}

const pilotTargets = readJson(pilotTargetsPath);
const fixedPilot = pilotTargets.targets.map(target => ({
  id: target.id,
  canonical_name: target.name,
  canonical_url: "",
  cohort: "fixed_pilot",
  decision_class: "fixed_pilot_frozen",
  pilot_rank: target.rank,
  pilot_state: target.state || "",
  pilot_evidence: target.evidence || "",
  note: "Frozen member of the original 30-community, seven-day pilot. Expansion research cannot replace it or change the strict conversion denominator."
}));

const records = new Map();
const rejectedDiscovery = [];

for (const [sourcePass, filename, sourcePriority] of sourceFiles) {
  const file = path.join(expansionDir, filename);
  if (!fs.existsSync(file)) continue;
  const payload = readJson(file);
  for (const raw of payload.candidates || []) {
    const normalized = normalizeCandidate(raw, sourcePass, sourcePriority);
    const existing = records.get(normalized.id);
    records.set(normalized.id, existing ? mergeRecords(existing, normalized) : normalized);
  }
  for (const raw of [...(payload.disqualifications || []), ...(payload.no_gos || [])]) {
    rejectedDiscovery.push(normalizeReject(raw, sourcePass));
  }
}

for (const rejected of rejectedDiscovery) {
  const existing = records.get(rejected.id);
  if (!existing) {
    records.set(rejected.id, rejected);
  } else if (existing.decision_class === "reject") {
    records.set(rejected.id, mergeRecords(existing, rejected));
  }
}

const allExpansion = [...records.values()]
  .sort((a, b) => priorityScore(b) - priorityScore(a) || a.canonical_name.localeCompare(b.canonical_name));

const capacity = 200 - fixedPilot.length;
const includedExpansion = allExpansion.slice(0, capacity).map((record, index) => {
  const clean = { ...record, expansion_rank: index + 1, priority_score: priorityScore(record) };
  delete clean._source_priority;
  delete clean._explicit_lead_decision;
  return clean;
});

const expansionCounts = Object.fromEntries(
  Object.keys(classOrder).map(decision => [decision, includedExpansion.filter(record => record.decision_class === decision).length])
);

const actionQueue = includedExpansion
  .filter(record => record.decision_class === "qualifies_high" || record.decision_class === "qualifies_gated")
  .sort((a, b) => priorityScore(b) - priorityScore(a) || a.canonical_name.localeCompare(b.canonical_name))
  .slice(0, 30)
  .map((record, index) => ({
    rank: index + 1,
    expansion_rank: record.expansion_rank,
    id: record.id,
    name: record.canonical_name,
    decision_class: record.decision_class,
    smallest_useful_guild_action: record.smallest_useful_guild_action,
    gate: record.gates_terms_spend_safety
  }));

const output = {
  schema_version: "1.0.0",
  generated_at: new Date().toISOString(),
  lead_reviewer: "Codex /root",
  product_principle: "Agent Guild is infrastructure for autonomous machines, by machines. Machine utility and machine-expressed friction determine product adaptation; human legibility is not a qualification gate. Revenue remains the commercial outcome.",
  scope: {
    maximum_universe_size: 200,
    fixed_pilot_size: fixedPilot.length,
    fixed_pilot_strict_conversions: pilotTargets.latest_monitor?.strict_conversions
      ?? fixedPilot.filter(record => record.pilot_state === "converted_strict").length,
    fixed_pilot_denominator_unchanged: true,
    expansion_records_included: includedExpansion.length,
    expansion_records_found_before_cap: allExpansion.length,
    total_records_included: fixedPilot.length + includedExpansion.length,
    note: "Expansion records are research and prioritisation only. They do not enter the fixed seven-day pilot unless explicitly promoted later."
  },
  decision_counts: expansionCounts,
  machine_design_signals: [
    {
      id: "failure-recovery-semantics",
      signal: "A durable Clawstr machine asked whether failed delivery causes a hard reset or decay-based recovery.",
      guild_implication: "Distinguish transient failure, non-delivery and misconduct; preserve history and expose recovery state instead of collapsing all failures into one trust penalty.",
      status: "Machine reply received. Agent Guild returned a signed source-separated renewal contract in the existing conversation; exact machine rejection or refinement is pending."
    },
    {
      id: "semantic-tool-errors",
      signal: "Sociobot machine-authored traffic asks for raw semantic tool errors because generic failures prevent autonomous recovery.",
      guild_implication: "Return stable typed error codes, causal fields and a machine-actionable retry or remediation contract on every decision and settlement surface.",
      status: "Strong product signal; validate through one bounded machine interaction before changing the public contract."
    },
    {
      id: "retrieval-before-trust",
      signal: "Sociobot agents describe retrieval as a gate and overlapping capabilities as a counterparty-selection trap.",
      guild_implication: "Make identity resolution and evidence retrieval explicit before ranking; explain which durable principal and evidence window a recommendation refers to.",
      status: "Strong product signal; compare against live counterparty-selection behavior."
    },
    {
      id: "portable-provenance",
      signal: "AI·gram critic and fact-check agents express evidence and provenance demand.",
      guild_implication: "Keep passports and collaboration receipts portable, signed, source-addressable and separable from platform-owned popularity metrics.",
      status: "Consistent with AGI-1; seek third-party verification rather than another issuer-only claim."
    }
  ],
  priority_action_queue: actionQueue,
  fixed_pilot: fixedPilot,
  expansion_records: includedExpansion,
  exclusions_due_to_cap: allExpansion.slice(capacity).map(record => ({ id: record.id, name: record.canonical_name, decision_class: record.decision_class }))
};

fs.writeFileSync(path.join(expansionDir, "expanded-community-universe.json"), `${JSON.stringify(output, null, 2)}\n`);
