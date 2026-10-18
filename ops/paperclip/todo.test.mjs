import test from 'node:test';
import assert from 'node:assert/strict';
import { digest } from './round-contracts.mjs';
import { validateTodoManifest, todoAdmission, parseTodoGate, readyTodoTasks } from './todo-contracts.mjs';
import { drainTodo } from './todo.mjs';

const ids = ['10000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000002'];
const limits = { liveChanges: false, paidSources: false, newAgentCalls: false, serviceRepositoryWrites: false, publicSourceAcquisition: false };
function fixture() {
  const config = { configHash: 'config', spec: { limits: { maxDailyRuns: null, maxConcurrentRuns: 2, timeoutSec: 600 } } };
  const installation = { companyId: 'signal', projectId: 'research', agents: { engineering: 'engineer', qa: 'reviewer' },
    workspaces: { engineering: 'we', qa: 'wq' }, roleWorkspaces: { engineering: '/owned/engineering', qa: '/owned/qa' } };
  const tasks = ids.map((issueId, i) => ({ issueId, role: i ? 'qa' : 'engineering',
    mode: i ? 'research_qa' : 'research_implementation', descriptionSha256: digest(`approved exact task ${i}`),
    scope: 'One bounded source supplement implementation or independent review only', evidence: ['fixed-source-sha256:0123456789'],
    limits: { ...limits }, dependsOn: i ? [ids[0]] : [] }));
  const manifest = { schemaVersion: 1, companyId: 'signal', projectId: 'research', configHash: 'config', baseSha: 'a'.repeat(40),
    authority: 'Owner explicitly requests this finite implementation and independent review.', approvedAt: '2026-10-18T00:00:00Z', tasks };
  const state = { companyId: 'signal', configHash: 'config', manifestSha256: digest(manifest), status: 'active', tasks: {} };
  const issues = ids.map((id, i) => ({ id, companyId: 'signal', projectId: 'research',
    assigneeAgentId: i ? 'reviewer' : 'engineer', status: 'todo', description: `approved exact task ${i}` }));
  const calls = [], runs = [], reports = new Map();
  const settings = { blocked: false, failWake: false, foreignActive: false, running: false, wrongAuthor: false };
  const api = { async call(url, method = 'GET', body) {
    calls.push({ url, method, body });
    if (url.includes('/companies/') && url.includes('/heartbeat-runs')) return settings.foreignActive
      ? [...runs, { id: 'foreign-run', status: 'running' }] : runs;
    if (url.startsWith('/agents/') && method === 'POST') {
      if (settings.failWake) throw new Error('Lost wake response');
      const issue = issues.find((item) => item.id === body.payload.issueId);
      const role = tasks.find((task) => task.issueId === issue.id).role;
      const run = { id: `run-${runs.length}`, companyId: 'signal', agentId: issue.assigneeAgentId,
        issueId: issue.id, status: settings.running ? 'running' : 'succeeded', exitCode: 0,
        contextSnapshot: { issueId: issue.id, paperclipWorkspace: { cwd: installation.roleWorkspaces[role] } } };
      runs.push(run); issue.status = 'done';
      reports.set(issue.id, { id: `doc-${issue.id}`, key: 'report', issueId: issue.id, companyId: 'signal',
        latestRevisionId: `rev-${issue.id}`, latestRevisionNumber: 1,
        body: JSON.stringify({ continuation: settings.blocked && issue.id === ids[0] ? 'blocked' : 'ready', reason: 'Exact approved bounded task completed with evidence.' }) });
      return run;
    }
    if (url.startsWith('/heartbeat-runs/')) {
      const run = runs.find((item) => url.includes(item.id));
      if (method === 'POST') return { cancelled: true };
      return run;
    }
    const id = url.split('/')[2], issue = issues.find((item) => item.id === id), report = reports.get(id);
    if (url.endsWith('/workspace')) return { bindingRevision: 1 };
    if (url.endsWith('/documents')) return report ? [report] : [];
    if (url.endsWith('/documents/report')) return report;
    if (url.endsWith('/documents/report/revisions')) return [{ id: report.latestRevisionId, documentId: report.id,
      issueId: id, companyId: 'signal', createdByAgentId: settings.wrongAuthor ? 'other' : issue.assigneeAgentId,
      revisionNumber: 1, body: report.body }];
    if (url.endsWith('/activity')) return [{ id: `activity-${id}`, companyId: 'signal', entityId: id,
      action: 'issue.document_created', actorType: 'agent', actorId: issue.assigneeAgentId,
      runId: runs.find((run) => run.issueId === id).id,
      details: { key: 'report', documentId: report.id, revisionNumber: 1 } }];
    if (issue) return issue;
    throw new Error(`Unexpected test route ${url}`);
  } };
  const options = { api, config, installation, manifest, state, save: async () => {}, validate: async () => {},
    currentHead: () => manifest.baseSha, wait: async () => {} };
  return { ...options, options, issues, runs, reports, calls, settings };
}
const wakes = (f) => f.calls.filter((call) => call.url.endsWith('/heartbeat/invoke'));

test('only exact owner-approved company, modes, paths of authority and earlier dependencies are valid', () => {
  const f = fixture(); assert.equal(validateTodoManifest(f.manifest, f.config, f.installation), f.manifest);
  for (const change of [
    (m) => { m.companyId = 'After'; }, (m) => { m.authority = ''; },
    (m) => { m.tasks[0].limits.liveChanges = true; }, (m) => { m.tasks[0].limits.paidSources = true; },
    (m) => { m.tasks[0].limits.serviceRepositoryWrites = true; }, (m) => { m.tasks[0].limits.newAgentCalls = true; },
    (m) => { m.tasks[0].dependsOn = [ids[1]]; }, (m) => { m.tasks[0].role = 'chief'; },
    (m) => { m.tasks[0].limits.publicSourceAcquisition = true; },
  ]) {
    const m = structuredClone(f.manifest); change(m);
    assert.throws(() => validateTodoManifest(m, f.config, f.installation));
  }
});
test('one controller executes approved implementation then independent QA without a second command', async () => {
  const f = fixture(); await drainTodo(f.options);
  assert.equal(f.state.status, 'completed'); assert.equal(wakes(f).length, 2);
  assert.deepEqual(wakes(f).map((call) => call.body.payload.issueId), ids);
  assert.equal(f.state.tasks[ids[0]].report.revisionId, `rev-${ids[0]}`);
  assert.equal(f.state.tasks[ids[1]].report.agentId, 'reviewer');
  assert(f.calls.every((call) => !call.url.includes('After')));
});
test('old reopened todo/report and any previous run are never executed again', async () => {
  const f = fixture(); f.reports.set(ids[0], { key: 'report' });
  await drainTodo(f.options); assert.equal(f.state.status, 'held'); assert.equal(wakes(f).length, 0);
  assert.equal(f.state.tasks[ids[0]].status, 'held_existing_report');
  assert.equal(todoAdmission({ issue: { status: 'todo' }, documents: [], runs: [{ contextSnapshot: { issueId: ids[0] } }],
    task: f.manifest.tasks[0] }), 'held_existing_run');
  assert.equal(todoAdmission({ issue: { status: 'blocked' }, documents: [], runs: [], task: f.manifest.tasks[0] }), 'held_status_blocked');
});
test('blocked evidence holds all dependent work, without retrying the first specialist', async () => {
  const f = fixture(); f.settings.blocked = true;
  await drainTodo(f.options); assert.equal(f.state.status, 'held'); assert.equal(wakes(f).length, 1);
  assert.equal(f.state.tasks[ids[1]], undefined);
  assert.equal(readyTodoTasks(f.manifest, f.state).length, 0);
});
test('ambiguous wake is persisted before POST and is never replayed even on explicit resume', async () => {
  const f = fixture(); f.settings.failWake = true;
  await assert.rejects(drainTodo(f.options), /Lost wake response/);
  assert.equal(f.state.tasks[ids[0]].status, 'invoking'); assert.equal(wakes(f).length, 1);
  f.settings.failWake = false;
  await assert.rejects(drainTodo(f.options), /automatic retry is forbidden/);
  assert.equal(wakes(f).length, 1);
});
test('foreign active runs, altered instructions, reassignment and false report ownership all stop dispatch', async () => {
  for (const mutation of [
    (f) => { f.settings.foreignActive = true; },
    (f) => { f.issues[0].description += ' now trade'; },
    (f) => { f.issues[0].assigneeAgentId = 'foreign'; },
  ]) {
    const f = fixture(); mutation(f); await assert.rejects(drainTodo(f.options)); assert.equal(wakes(f).length, 0);
  }
  const f = fixture(); f.settings.wrongAuthor = true;
  await assert.rejects(drainTodo(f.options), /not authored/); assert.equal(wakes(f).length, 1);
});
test('missing or fabricated successor gates cannot authorize the next task', () => {
  for (const body of ['done', '{}', '{"continuation":"trade","reason":"looks good enough"}',
    '```json\n{"continuation":"ready","reason":"enough detail here"}\n```\n```json\n{}\n```'])
    assert.throws(() => parseTodoGate(body));
  const f = fixture(); f.state.tasks[ids[0]] = { role: 'engineering', status: 'done' };
  assert.deepEqual(readyTodoTasks(f.manifest, f.state), []);
});
test('duplicate JSON keys are rejected at every depth, including Unicode-escaped key aliases', () => {
  const reason = 'Exact source evidence is still missing, so follow-up work must be held.';
  for (const body of [
    `{"continuation":"blocked","reason":"${reason}","continuation":"ready"}`,
    `{"continuation":"blocked","reason":"${reason}","continua\\u0074ion":"ready"}`,
    `{"continuation":"ready","reason":"${reason}","evidence":{"status":"blocked","status":"ready"}}`,
    `{"continuation":"ready","reason":"${reason}","evidence":[{"st\\u0061tus":"blocked","status":"ready"}]}`,
    `{"continuation":"ready","reason":"${reason}","evidence":{"a\\\\b":0,"a\\u005cb":1}}`,
  ]) {
    assert.throws(() => parseTodoGate(body), /Malformed continuation gate/);
    assert.throws(() => parseTodoGate(`A report\n\`\`\`json\n${body}\n\`\`\``), /Malformed continuation gate/);
  }
  assert.equal(parseTodoGate(JSON.stringify({ continuation: 'ready', reason,
    evidence: [{ status: 'ready' }, { status: 'blocked', quote: 'Escaped \\" quotes and \\ backslashes' }],
    nested: { status: 'ready', values: [true, null, -1.2e3, 'a,b}'] } })).continuation, 'ready');
});
test('known interrupted controller run is monitored by ID, never re-invoked', async () => {
  const f = fixture();
  const run = await f.api.call('/agents/engineer/heartbeat/invoke', 'POST', { payload: { issueId: ids[0] } });
  f.state.tasks[ids[0]] = { role: 'engineering', status: 'running', runId: run.id, invokedAt: new Date().toISOString() };
  await drainTodo(f.options);
  assert.equal(f.state.status, 'completed'); assert.equal(wakes(f).length, 2);
  assert.equal(wakes(f).filter((call) => call.body.payload.issueId === ids[0]).length, 1);
});
test('the 600-second ceiling cancels one known run once and cannot auto-retry it', async () => {
  const f = fixture(); f.settings.running = true;
  let clock = 0; f.options.now = () => { clock += 600001; return clock; };
  await assert.rejects(drainTodo(f.options), /cancellation requested/);
  assert.equal(f.calls.filter((call) => call.url.endsWith('/cancel')).length, 1);
  assert.equal(wakes(f).length, 1);
  await assert.rejects(drainTodo(f.options), /automatic retry is forbidden/);
  assert.equal(wakes(f).length, 1);
});
