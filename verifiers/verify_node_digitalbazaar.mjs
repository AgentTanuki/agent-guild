// INDEPENDENT third-party verification: Digital Bazaar's Data Integrity stack.
import {DataIntegrityProof} from '@digitalbazaar/data-integrity';
import {createVerifyCryptosuite} from '@digitalbazaar/eddsa-jcs-2022-cryptosuite';
const eddsaJcs2022CryptoSuite = createVerifyCryptosuite();
import * as Ed25519Multikey from '@digitalbazaar/ed25519-multikey';
import jsigs from 'jsonld-signatures';
import {readFileSync} from 'node:fs';

const {purposes: {AssertionProofPurpose}} = jsigs;
const vec = JSON.parse(readFileSync(process.argv[2] || 'vector.json', 'utf8'));

function didKeyDoc(did) {
  const mb = did.slice('did:key:'.length);
  const id = `${did}#${mb}`;
  return {
    keyDoc: {'@context': 'https://w3id.org/security/multikey/v1',
             type: 'Multikey', id, controller: did, publicKeyMultibase: mb},
    didDoc: {'@context': ['https://www.w3.org/ns/did/v1'], id: did,
             assertionMethod: [id],
             verificationMethod: [{type: 'Multikey', id, controller: did, publicKeyMultibase: mb}]},
  };
}

const documentLoader = async (url) => {
  if (url.startsWith('did:key:')) {
    const {keyDoc, didDoc} = didKeyDoc(url.split('#')[0]);
    return {contextUrl: null, documentUrl: url, document: url.includes('#') ? keyDoc : didDoc};
  }
  throw new Error(`unexpected documentLoader url: ${url}`);
};

async function verifyOne(name, credential) {
  const suite = new DataIntegrityProof({cryptosuite: eddsaJcs2022CryptoSuite});
  const result = await jsigs.verify(credential, {
    suite, purpose: new AssertionProofPurpose(), documentLoader,
  });
  console.log(name, 'verified:', result.verified,
              result.verified ? '' : JSON.stringify(result.error?.errors?.map(e=>e.message)));
  // tamper check
  const tampered = structuredClone(credential);
  tampered.credentialSubject = {...tampered.credentialSubject, rating: 0.1, trust: 99};
  const bad = await jsigs.verify(tampered, {suite: new DataIntegrityProof({cryptosuite: eddsaJcs2022CryptoSuite}), purpose: new AssertionProofPurpose(), documentLoader});
  console.log(name, 'tampered rejected:', !bad.verified);
  return result.verified && !bad.verified;
}

const ok1 = await verifyOne('credential', vec.credential);
const ok2 = await verifyOne('passport', vec.passport);
process.exit(ok1 && ok2 ? 0 : 1);
