// Apache-2.0. Original bounded input and consumption policy by AgentTanuki.
const { verifyDataIntegrity, publicKeyFromDid } = require('./verifier.js');
const MAX_BYTES = 65536;
const MAX_DEPTH = 16;
const MAX_NODES = 4096;
const DAY_NS = 86400000000000n;
const CONTEXT = 'https://www.w3.org/ns/credentials/v2';
const B58 = /^[1-9A-HJ-NP-Za-km-z]+$/;
const NOTE = 'Origin and integrity of a recent snapshot only. Not safety, endpoint ownership, current reputation, independent operation, or authorization. No hire decision was made.';

class Rejection extends Error {
  constructor(code) { super(code); this.code = code; }
}
const reject = (code) => { throw new Rejection(code); };
const record = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
function unicode(text) {
  for (let i = 0; i < text.length; i++) {
    const n = text.charCodeAt(i);
    if (n >= 0xd800 && n <= 0xdbff) {
      const next = text.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (n >= 0xdc00 && n <= 0xdfff) return false;
  }
  return true;
}

// Normalize a JSON decimal as digits + decimal exponent, without floating-point
// rounding. Used only to reject text that JSON.parse would silently change.
function decimal(text) {
  const m = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(text);
  let digits = (m[2] + (m[3] || '')).replace(/^0+/, '');
  if (!digits) return '0';
  let exponent = Number(m[4] || 0) - (m[3] || '').length;
  while (digits.endsWith('0')) { digits = digits.slice(0, -1); exponent++; }
  return m[1] + digits + 'e' + exponent;
}

// A bounded JSON parser with duplicate-key checks (including escaped aliases).
// Every signed member is retained; unsupported numeric representations fail
// closed instead of being rounded into a different signed document.
function parse(text) {
  if (typeof text !== 'string') reject('credential_json_must_be_string');
  if (text.length > MAX_BYTES || Buffer.byteLength(text, 'utf8') > MAX_BYTES) reject('credential_too_large');
  if (!unicode(text)) reject('invalid_unicode');
  let p = 0, nodes = 0;
  function whitespace() { while (/[\x20\t\r\n]/.test(text[p] || '\0')) p++; }
  function string() {
    const start = p++;
    while (p < text.length) {
      if (text[p] === '"') {
        p++;
        let value;
        try { value = JSON.parse(text.slice(start, p)); } catch (_) { reject('invalid_json'); }
        if (!unicode(value)) reject('invalid_unicode');
        return value;
      }
      if (text[p] === '\\') p++;
      p++;
    }
    reject('invalid_json');
  }
  function value(depth) {
    if (depth > MAX_DEPTH) reject('credential_too_deep');
    if (++nodes > MAX_NODES) reject('credential_too_complex');
    whitespace();
    const ch = text[p];
    if (ch === '"') return string();
    if (ch === '{') {
      p++; whitespace();
      const out = Object.create(null), seen = new Set();
      if (text[p] === '}') { p++; return out; }
      while (p < text.length) {
        if (text[p] !== '"') reject('invalid_json');
        const key = string();
        if (seen.has(key)) reject('duplicate_json_key');
        // Python/JS key sorting can differ outside ASCII. Current Guild field
        // names are ASCII; Unicode string values remain supported.
        if (!/^[\x00-\x7f]*$/.test(key)) reject('unsupported_field_name');
        seen.add(key); whitespace();
        if (text[p++] !== ':') reject('invalid_json');
        out[key] = value(depth + 1); whitespace();
        if (text[p] === '}') { p++; return out; }
        if (text[p++] !== ',') reject('invalid_json');
        whitespace();
      }
      reject('invalid_json');
    }
    if (ch === '[') {
      p++; whitespace();
      const out = [];
      if (text[p] === ']') { p++; return out; }
      while (p < text.length) {
        out.push(value(depth + 1)); whitespace();
        if (text[p] === ']') { p++; return out; }
        if (text[p++] !== ',') reject('invalid_json');
      }
      reject('invalid_json');
    }
    for (const [word, result] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(word, p)) { p += word.length; return result; }
    }
    const m = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(p));
    if (!m) reject('invalid_json');
    const number = Number(m[0]);
    if (!Number.isFinite(number) || (Number.isInteger(number) && !Number.isSafeInteger(number)) || decimal(m[0]) !== decimal(JSON.stringify(number))) reject('unsupported_number');
    p += m[0].length;
    return number;
  }
  const result = value(1); whitespace();
  if (p !== text.length) reject('invalid_json');
  if (!record(result)) reject('credential_must_be_object');
  return result;
}

function did(value) {
  if (typeof value !== 'string' || value.length > 64 || !value.startsWith('did:key:z') || !B58.test(value.slice(9))) return false;
  try { return publicKeyFromDid(value).length === 32; } catch (_) { return false; }
}
function time(value) {
  if (typeof value !== 'string' || value.length > 40) reject('invalid_timestamp');
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!m) reject('invalid_timestamp');
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  const offsetHours = m[8] === 'Z' ? 0 : Number(m[8].slice(1, 3));
  const offsetMinutes = m[8] === 'Z' ? 0 : Number(m[8].slice(4));
  if (year < 1000 || month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate() || hour > 23 || minute > 59 || second > 59 || offsetHours > 23 || offsetMinutes > 59) reject('invalid_timestamp');
  const offset = (offsetHours * 60 + offsetMinutes) * (m[8][0] === '-' ? -1 : 1);
  const secondsMs = Date.UTC(year, month - 1, day, hour, minute, second) - offset * 60000;
  return BigInt(secondsMs) * 1000000n + BigInt((m[7] || '').padEnd(6, '0') || '0') * 1000n;
}
function output(status, reason) {
  return { status, verified: status === 'verified', reason, policy: 'agent-guild-passport-offline/v1; maximum signed age 24 hours; no future allowance', note: NOTE };
}
function check(args, runtimeArgs) {
  try {
    if (!record(runtimeArgs) || !runtimeArgs.EXPECTED_ISSUER_DID) return output('unavailable', 'issuer_pin_required');
    const issuer = runtimeArgs.EXPECTED_ISSUER_DID;
    if (!did(issuer)) return output('unavailable', 'issuer_pin_invalid');
    if (!record(args)) reject('arguments_must_be_object');
    const fields = Object.getOwnPropertyDescriptors(args);
    if (Object.keys(fields).length !== 2 || !fields.credential_json || !fields.expected_subject_did || Object.values(fields).some((f) => !Object.prototype.hasOwnProperty.call(f, 'value'))) reject('exact_arguments_required');
    if (!did(args.expected_subject_did)) reject('expected_subject_invalid');
    const vc = parse(args.credential_json);
    if (vc.issuer !== issuer) reject('issuer_mismatch');
    if (!record(vc.credentialSubject) || vc.credentialSubject.id !== args.expected_subject_did) reject('subject_mismatch');
    if (!Array.isArray(vc['@context']) || vc['@context'].length !== 1 || vc['@context'][0] !== CONTEXT || !Array.isArray(vc.type) || vc.type.length !== 2 || vc.type[0] !== 'VerifiableCredential' || vc.type[1] !== 'AgentGuildPassport' || typeof vc.id !== 'string' || !vc.id || vc.id.length > 512) reject('unsupported_passport_structure');
    const proof = vc.proof;
    if (!record(proof) || proof.type !== 'DataIntegrityProof' || proof.cryptosuite !== 'eddsa-jcs-2022' || proof.proofPurpose !== 'assertionMethod' || proof.verificationMethod !== issuer + '#' + issuer.slice(8) || !Array.isArray(proof['@context']) || proof['@context'].length !== 1 || proof['@context'][0] !== CONTEXT || typeof proof.proofValue !== 'string' || proof.proofValue.length > 100 || !proof.proofValue.startsWith('z') || !B58.test(proof.proofValue.slice(1))) reject('unsupported_proof');
    const from = time(vc.validFrom), created = time(proof.created);
    if (from !== created) reject('signed_times_disagree');
    const now = BigInt(Date.now()) * 1000000n;
    if (from > now) reject('not_yet_valid');
    if (now - from > DAY_NS) reject('snapshot_too_old');
    if (Object.prototype.hasOwnProperty.call(vc, 'validUntil')) {
      const until = time(vc.validUntil);
      if (until <= from) reject('invalid_validity_window');
      if (now >= until) reject('expired');
    }
    if (!verifyDataIntegrity(vc)) reject('invalid_signature');
    return output('verified', 'signature_issuer_subject_and_signed_freshness_match');
  } catch (e) {
    if (e instanceof Rejection) return output('rejected', e.code);
    return output('unavailable', 'internal_error');
  }
}
module.exports = { check };
