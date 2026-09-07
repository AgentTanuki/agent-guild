# Historical credential quarantine — 7 September 2026

PR #180 prevented public rendering of historical bearer credentials. The two
public key identifiers recorded in the incident are now denied unconditionally
at member authentication and presented billing-account resolution. This includes
legacy and hashed storage, state restores, and a malformed legacy account keyed
by the public identifier. No disclosed credential is stored in this change or
used in a live verification request. Regression tests use newly generated local
credentials and add their public identifiers to the policy temporarily.

The deny rule leaves identities, balances and evidence intact. The existing
administrator-authorised rotation endpoint can reissue an unaffected credential.
A denied credential cannot authorise its own rotation. Free guest access and
anonymous trials retain their existing behaviour.

`GET /health` publishes only the policy version and number of blocked key IDs.
Verify the exact deployed source revision and this aggregate before treating
runtime containment as live. Every rollback must retain this quarantine patch.

This denies use of these credentials in the patched Guild service. It does not
determine whether they were valid, establish ownership or misuse, delete stored
verifiers, invalidate independently held signing keys, or revoke credentials in
another system. Authorised production record retirement remains a separate
follow-up when hosting access is available; removal of this patch would remove
its protection. Do not claim full incident closure from a health readback.
