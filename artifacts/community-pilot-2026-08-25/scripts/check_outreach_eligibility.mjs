#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const pilotDir = resolve(import.meta.dirname, "..");
const [community, recipient = ""] = process.argv.slice(2);

if (!community) {
  throw new Error("usage: check_outreach_eligibility.mjs <fixed-community-id> [recipient]");
}

const [ledger, targets] = await Promise.all([
  readFile(resolve(pilotDir, "outreach-ledger.json"), "utf8").then(JSON.parse),
  readFile(resolve(pilotDir, "targets.json"), "utf8").then(JSON.parse),
]);

const reconciliation = ledger.reconciliation;
const ledgerReconciled = reconciliation?.historical_operation_evidence_scanned === true
  && reconciliation?.reservation_required_before_dispatch === true;

if (!ledgerReconciled) {
  console.log(JSON.stringify({
    decision: "DENY_LEDGER_NOT_RECONCILED",
    community,
    recipient: recipient.trim().toLowerCase() || null,
    reasons: [
      "historical_operation_evidence_not_confirmed",
      "pre_dispatch_reservation_not_required",
    ],
  }, null, 2));
  process.exit(2);
}

const target = targets.targets.find((row) => row.id === community);
if (!target) {
  console.log(JSON.stringify({
    decision: "DENY_UNKNOWN_FIXED_COMMUNITY",
    community,
    recipient: recipient || null,
  }, null, 2));
  process.exit(2);
}

const communityRecord = ledger.closed_communities.find((row) => row.id === community);
const alreadyConverted = target.state === "converted_strict";
const normalizedRecipient = recipient.trim().toLowerCase();
const recipientSeen = normalizedRecipient !== ""
  && ledger.known_recipients.some((value) => value.toLowerCase() === normalizedRecipient);

if (communityRecord || recipientSeen || alreadyConverted) {
  console.log(JSON.stringify({
    decision: "DENY_OUTREACH",
    community,
    recipient: normalizedRecipient || null,
    reasons: [
      ...(communityRecord ? ["fixed_community_already_contacted"] : []),
      ...(recipientSeen ? ["recipient_already_contacted_or_attempted"] : []),
      ...(alreadyConverted ? ["fixed_community_already_converted"] : []),
    ],
    first_attempt: communityRecord?.first_attempt ?? null,
    evidence: communityRecord?.evidence ?? null,
    rule: ledger.rule,
  }, null, 2));
  process.exit(2);
}

console.log(JSON.stringify({
  decision: "AUDIT_ONLY_RESERVATION_REQUIRED",
  community,
  recipient: normalizedRecipient || null,
  send_authority: false,
  required_next_step: "record a canonical outreach-ledger reservation before dispatch",
  required_next_checks: [
    "targets.json state and evidence",
    "all durable campaign evidence for community aliases",
    "external sent-message and post ledgers",
    "exact recipient history",
    "documented pre-existing autonomous machine identity and action pipeline",
  ],
  rule: ledger.rule,
}, null, 2));
