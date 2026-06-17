// ---------------------------------------------------------------------------
// W3C Verifiable Credentials for attestations and soulbound badges.
// ---------------------------------------------------------------------------
import { signPayload, verifyPayload, publicKeyFromDid } from "./crypto";
import type { VerifiableCredential, Proof } from "./types";

const VC_CONTEXT = [
  "https://www.w3.org/ns/credentials/v2",
  "https://w3id.org/security/suites/ed25519-2020/v1",
];

interface IssueArgs {
  id: string;
  type: string[];
  issuerDid: string;
  issuerPrivateKeyHex: string;
  subjectDid: string;
  taskId: string;
  rating: number;
  domain: string;
  timestamp?: string;
}

/** Build and sign a Verifiable Credential. The signature covers the whole
 *  credential except the `proof.proofValue` field itself. */
export function issueCredential(args: IssueArgs): VerifiableCredential {
  const created = args.timestamp ?? new Date().toISOString();
  const unsigned: Omit<VerifiableCredential, "proof"> & { proof: Omit<Proof, "proofValue"> } = {
    "@context": VC_CONTEXT,
    id: args.id,
    type: ["VerifiableCredential", ...args.type],
    issuer: args.issuerDid,
    validFrom: created,
    credentialSubject: {
      id: args.subjectDid,
      taskId: args.taskId,
      rating: args.rating,
      domain: args.domain,
    },
    proof: {
      type: "Ed25519Signature2020",
      created,
      verificationMethod: `${args.issuerDid}#${args.issuerDid.split(":").pop()}`,
      proofPurpose: "assertionMethod",
    },
  };

  const proofValue = signPayload(unsigned, args.issuerPrivateKeyHex);
  return { ...unsigned, proof: { ...unsigned.proof, proofValue } };
}

/** Cryptographically verify a credential: recompute the signed payload and
 *  check it against the public key embedded in the issuer's did:key. */
export function verifyCredential(vc: VerifiableCredential): boolean {
  try {
    const { proof, ...rest } = vc;
    const { proofValue, ...proofRest } = proof;
    const payload = { ...rest, proof: proofRest };
    const issuerPubKey = publicKeyFromDid(vc.issuer);
    return verifyPayload(payload, proofValue, issuerPubKey);
  } catch {
    return false;
  }
}
