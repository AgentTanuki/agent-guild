// Apache-2.0. Original AnythingLLM adaptation by AgentTanuki.
module.exports.runtime = {
  handler: async function (args) {
    try {
      const policy = require('./passport-policy.js');
      return JSON.stringify(policy.check(args, this && this.runtimeArgs));
    } catch (_) {
      // No raw exception, credential, introspection or credential-bearing log.
      return JSON.stringify({
        status: 'unavailable',
        verified: false,
        reason: 'internal_error',
        note: 'Verification unavailable. No authorization or trust decision was made.',
      });
    }
  },
};
