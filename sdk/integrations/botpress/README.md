# Agent Guild Endpoint Observations for Botpress

Standalone source for an optional Botpress integration with one action: `observeEndpoint`. The Hub-facing usage and disclosure text is in [hub.md](hub.md).

The implementation uses a native `IntegrationDefinition`, generated `.botpress` types and Integration class, and the SDK action dispatcher. There are no channels, events, resources to provision, configuration secrets, or automatic connection hooks. Registration checks the empty configuration; registration and unregistration make no external requests.

## Local build and checks

Use an isolated working directory and Node 24 for the validated development setup. The dependency lock pins SDK 7.2.3, CLI 7.1.3, client 2.4.0, and TypeScript 6.0.2. Package engine ranges describe supported dependency declarations; only Node 24 was exercised for this candidate.

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run build
npm run check:type
npm test
npm run check:format
```

The build creates `.botpress/implementation` and the native `.botpress/dist/index.cjs` bundle. Generated files are local build outputs, not hand-maintained source. Tests call that bundle's real SDK dispatcher with fixture requests and substituted Fetch responses. They also load the actual definition schemas. No model, live Guild endpoint, target service, or Botpress API is used by these tests. Tests do not establish hosted deployment, bot installation, Hub rendering, verification, account eligibility, adoption, or production compatibility.

## Publication scope

This source is prepared for Botpress's documented standalone Hub integration route. Publishing, choosing a workspace-qualified identifier if required, checking for duplicate Hub listings, accepting applicable terms, installing a demonstration bot, and requesting verification remain separate account actions. A local build does not perform them. No Botpress-maintained monorepo contribution or endorsement is claimed.

The neutral icon is original SVG artwork. The integration source is Apache-2.0; the starter layout and configuration derive from the MIT-licensed Botpress template, preserved in `LICENSE-BOTPRESS-TEMPLATE` and `NOTICE`.
