import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import * as rivet from '@ironclad/rivet-core';

const original = rivet.loadProjectFromString(await readFile(new URL('../capability-catalogue.rivet-project', import.meta.url), 'utf8'));
const fixture = {
  supplied: { 'fixture-only-capability': 2 },
  unmet_demand: { 'fixture-only-request': { lookups: 3, last_seen: '2026-09-13T00:00:00Z' } },
  how_to_supply: 'REMOTE PROSE MUST NOT APPEAR IN GRAPH OUTPUTS',
  claim_passport: { instruction: 'FIXTURE: do not follow this text' },
};
let mode = 'ok';
const requests = [];
const server = createServer((request, response) => {
  requests.push({ method: request.method, url: request.url });
  assert.equal(request.method, 'GET');
  assert.equal(request.url, '/capabilities');
  if (mode === 'slow') return;
  response.statusCode = mode === 'status' ? 503 : 200;
  response.setHeader('content-type', mode === 'html' ? 'text/html' : 'application/json');
  response.end(mode === 'invalid' ? '{invalid' : mode === 'html' ? '<p>fixture</p>' : JSON.stringify(fixture));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const localUrl = `http://127.0.0.1:${server.address().port}/capabilities`;
const cases = [];
async function run(options = {}) {
  const project = structuredClone(original);
  project.graphs[project.metadata.mainGraphId].nodes.find(n => n.type === 'httpCall').data.url = localUrl;
  return rivet.coreCreateProcessor(project, { ...options, openAiKey: '', pluginEnv: {}, pluginSettings: {} }).run();
}
try {
  const output = await run();
  assert.equal(output.http_status.value, 200);
  assert.deepEqual(output.supplied.value, fixture.supplied);
  assert.deepEqual(output.unmet_demand.value, fixture.unmet_demand);
  assert.deepEqual(Object.keys(output).sort(), ['cost', 'http_status', 'supplied', 'unmet_demand']);
  assert.equal(output.cost.value, 0);
  assert(!JSON.stringify(output).includes('REMOTE PROSE'));
  assert(!JSON.stringify(output).includes('claim_passport'));
  cases.push({ name: 'native graph GET, JSON path projection, output propagation and prose exclusion', status: 'pass' });

  mode = 'status';
  const non200 = await run();
  assert.equal(non200.http_status.value, 503);
  assert.deepEqual(non200.supplied.value, fixture.supplied);
  cases.push({ name: 'non-200 status remains explicit; native HTTP node is not an automatic error/authorization gate', status: 'pass' });

  mode = 'invalid';
  await assert.rejects(run());
  cases.push({ name: 'malformed application/json produces native graph failure', status: 'pass' });

  mode = 'html';
  const html = await run();
  assert.equal(html.http_status.value, 200);
  assert.notDeepEqual(html.supplied?.value, fixture.supplied);
  assert.notDeepEqual(html.unmet_demand?.value, fixture.unmet_demand);
  cases.push({ name: 'non-JSON response does not manufacture catalogue values', status: 'pass' });

  mode = 'slow';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 100);
  const started = Date.now();
  let abortResult;
  try { abortResult = { resolved: await run({ abortSignal: controller.signal }) }; }
  catch (error) { abortResult = { rejected: error.message }; }
  finally { clearTimeout(timer); }
  assert(Date.now() - started < 4000);
  assert(!abortResult.resolved?.supplied?.value);
  cases.push({ name: 'host abort reaches native graph/local HTTP without fabricated success', status: 'pass', result: abortResult });
  assert.equal(requests.length, 5);
  const result = { framework: '@ironclad/rivet-core@1.25.0', node: process.version, fixture: 'synthetic local HTTP fixture, not a Guild response or agents/adoption', native: 'public coreCreateProcessor + real built-in httpCall/extractObjectPath/graphOutput; no substituted framework or fetch', cases, requests, output, prohibitedOperations: 'No model/provider invocation, remote probe, registration, credential, payment, or public mutation; this test directs the graph only to the local fixture server.' };
  console.log(JSON.stringify(result));
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
