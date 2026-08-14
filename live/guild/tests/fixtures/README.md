# MPP official-mppx interop fixtures

Generated OFFLINE (no funds, no chain, no network) with official
**mppx 0.8.17** (`npm i mppx@0.8.17`):

```js
import { Challenge } from 'mppx';
import * as evmClient from 'mppx/evm/client';
import { privateKeyToAccount } from 'viem/accounts';
// challenge minted by app/mpp.py mint_challenge(check_request('fact-check'), 10)
const ch = Challenge.fromHeaders(new Headers({ 'WWW-Authenticate': <hdr> }));
const method = evmClient.charge({
  account,
  currencies: [evmClient.assets.base.USDC],
  networks: [evmClient.chains.base],
  maxAmount: '0.01',
});
const authz = await method.createCredential({ challenge: ch, context: { account } });
```

- `mppx_0817_challenge.txt` — the WWW-Authenticate: Payment header AG minted.
- `mppx_0817_credential.txt` — the `Authorization: Payment <b64>` header the
  official client produced. Its payload is the flat AuthorizationPayloadSchema
  shape (from, nonce, signature, to, type, validAfter, validBefore, value);
  nonce == keccak256(challenge.id + realm); source == did:pkh:eip155:<chain>:<from>.
  This fixture uses production Base (`8453`) USDC from mppx's own asset
  registry; the challenge includes `credentialTypes:["authorization"]` and
  `decimals:6`, and the test independently recovers the EIP-3009 signer.

Regenerate only if the challenge/credential wire shape changes.
