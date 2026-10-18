#!/usr/bin/env node
// Drain an OWNER-APPROVED finite task graph, reusing Paperclip's native queue.
// No timer/assignment wakeups, OS scheduler, arbitrary todo discovery, or merge.
import { mkdir, readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadConfig, Paperclip, readInstallation, validateCompany, atomicJson } from './lib.mjs';
import { acquireLock } from './round.mjs';
import { assert, digest, reportEvidence, TERMINAL_RUNS } from './round-contracts.mjs';
import { validateTodoManifest, assertTodoIssue, todoAdmission, parseTodoGate, readyTodoTasks } from './todo-contracts.mjs';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const rows = (value) => { assert(Array.isArray(value), 'Expected an unambiguous API array'); return value; };
const head = (repo) => execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim();

export async function drainTodo({ api, config, installation, manifest, state, save,
  validate = () => validateCompany(api, config, installation), currentHead = () => head(config.repo),
  now = () => Date.now(), wait = sleep }) {
  validateTodoManifest(manifest, config, installation);
  assert(config.spec.limits.maxDailyRuns === null && config.spec.limits.maxConcurrentRuns === 2
    && config.spec.limits.timeoutSec > 0 && config.spec.limits.timeoutSec <= 600, 'Execution limits changed');
  assert(state.manifestSha256 === digest(manifest) && state.companyId === installation.companyId
    && state.configHash === config.configHash, 'Saved queue identity changed');
  assert(state.tasks && !Array.isArray(state.tasks) && Object.keys(state.tasks).every((id) => manifest.tasks.some((task) =>
    task.issueId === id && task.role === state.tasks[id]?.role)), 'Saved queue contains a foreign task/role');
  const savedRunIds = Object.values(state.tasks).flatMap((item) => item.runId ? [item.runId] : []);
  assert(new Set(savedRunIds).size === savedRunIds.length, 'A run ID cannot satisfy more than one approved task');
  for (const saved of Object.values(state.tasks).filter((item) => item.status === 'running'))
    assert(typeof saved.runId === 'string' && Number.isFinite(Date.parse(saved.invokedAt)), 'Known run identity/start clock required');
  const readRuns = async () => {
    const list = rows(await api.call(`/companies/${installation.companyId}/heartbeat-runs?limit=1000`));
    assert(list.length < 1000, 'Run history may be truncated; inspect before dispatch');
    return list;
  };
  const ensureIdle = async () => {
    const runs = await readRuns();
    const owned = new Set(Object.values(state.tasks).flatMap((item) => item.runId ? [item.runId] : []));
    assert(runs.every((run) => TERMINAL_RUNS.has(run.status) || owned.has(run.id)),
      'Another company controller/run is active; no competing dispatch');
    return runs;
  };
  const verify = async (task, saved) => {
    const root = `/issues/${task.issueId}`;
    const [issue, run, document, revisions, activity] = await Promise.all([
      api.call(root), api.call(`/heartbeat-runs/${saved.runId}`), api.call(`${root}/documents/report`),
      api.call(`${root}/documents/report/revisions`), api.call(`${root}/activity`),
    ]);
    assertTodoIssue(issue, task, manifest, installation);
    assert(run.contextSnapshot?.paperclipWorkspace?.cwd === installation.roleWorkspaces[task.role],
      'Actual run workspace does not match the approved role');
    const proof = reportEvidence({ issue, run, document, revisions: rows(revisions), activity: rows(activity),
      expected: { issueId: task.issueId, companyId: installation.companyId, projectId: installation.projectId,
        agentId: installation.agents[task.role], runId: saved.runId } });
    if (saved.report) assert(digest(proof) === digest(saved.report), 'Completed report revision changed; stop rather than substitute');
    return proof;
  };
  const finish = async (task, saved) => {
    const deadline = Date.parse(saved.invokedAt) + config.spec.limits.timeoutSec * 1000;
    while (true) {
      const run = await api.call(`/heartbeat-runs/${saved.runId}`);
      assert(run.companyId === installation.companyId && run.agentId === installation.agents[task.role], 'Run owner changed');
      if (TERMINAL_RUNS.has(run.status)) break;
      if (now() >= deadline) {
        saved.status = 'cancelling'; await save();
        await api.call(`/heartbeat-runs/${saved.runId}/cancel`, 'POST', {});
        saved.status = 'failed'; saved.reason = '600-second ceiling; cancellation requested, never retried'; await save();
        throw new Error(saved.reason);
      }
      await wait(2500);
    }
    saved.report = await verify(task, saved);
    saved.gate = parseTodoGate(saved.report.body);
    saved.status = 'done'; await save();
  };
  const invoke = async (task) => {
    const saved = state.tasks[task.issueId] = { role: task.role, status: 'checking' };
    await save();
    const issue = await api.call(`/issues/${task.issueId}`);
    assertTodoIssue(issue, task, manifest, installation);
    const documents = rows(await api.call(`/issues/${task.issueId}/documents`));
    const admission = todoAdmission({ issue, documents, runs: await readRuns(), task });
    if (admission !== 'ready') { saved.status = admission; await save(); return; }
    const binding = await api.call(`/issues/${task.issueId}/workspace`);
    saved.status = 'binding'; await save();
    await api.call(`/issues/${task.issueId}/workspace`, 'PUT', {
      requestKey: `approved-todo:${state.manifestSha256}:${task.issueId}`, expectedBindingRevision: binding.bindingRevision,
      selection: { kind: 'configured_source', projectWorkspaceId: installation.workspaces[task.role], mode: 'shared' },
    });
    // Recheck after binding and immediately before the sole side-effecting wake.
    await validate(); assert(currentHead() === manifest.baseSha, 'Repository HEAD changed; new explicit approval required');
    const latest = await api.call(`/issues/${task.issueId}`); assertTodoIssue(latest, task, manifest, installation);
    const freshRuns = await ensureIdle();
    assert(todoAdmission({ issue: latest, documents: rows(await api.call(`/issues/${task.issueId}/documents`)),
      runs: freshRuns, task }) === 'ready', 'Task was executed/changed while binding; do not invoke');
    saved.status = 'invoking'; saved.invokedAt = new Date(now()).toISOString(); await save();
    const run = await api.call(`/agents/${installation.agents[task.role]}/heartbeat/invoke`, 'POST', {
      reason: `Signal Desk approved todo ${task.mode}`, triggerDetail: 'manual',
      payload: { issueId: task.issueId }, idempotencyKey: `approved-todo:${state.manifestSha256}:${task.issueId}`, forceFreshSession: true,
    });
    assert(run.id && run.companyId === installation.companyId && run.agentId === installation.agents[task.role],
      'Wake skipped/foreign/ambiguous: reconcile manually, no retry');
    saved.runId = run.id; saved.status = 'running'; await save();
    await finish(task, saved);
  };
  await validate(); assert(currentHead() === manifest.baseSha, 'Repository HEAD changed before dispatch');
  for (const task of manifest.tasks) {
    const saved = state.tasks[task.issueId];
    assert(!saved || ['running', 'done'].includes(saved.status), 'Held/uncertain/failed task is preserved; automatic retry is forbidden');
    if (saved?.status === 'done') await verify(task, saved);
  }
  await ensureIdle();
  // Known run IDs are monitored on resume; invoking/unknown outcomes never wake again.
  const existing = manifest.tasks.filter((task) => state.tasks[task.issueId]?.status === 'running');
  for (const task of existing) await finish(task, state.tasks[task.issueId]);
  while (true) {
    const ready = readyTodoTasks(manifest, state, config.spec.limits.maxConcurrentRuns);
    if (!ready.length) break;
    await ensureIdle();
    for (const task of manifest.tasks.filter((item) => state.tasks[item.issueId]?.status === 'done'))
      await verify(task, state.tasks[task.issueId]);
    // Serial dispatch avoids a race between two pre-invoke active-run snapshots.
    // The existing backend queue still enforces agent=1; this uses company<=1<=2.
    for (const task of ready) await invoke(task);
  }
  state.status = manifest.tasks.every((task) => state.tasks[task.issueId]?.status === 'done'
    && state.tasks[task.issueId]?.gate?.continuation === 'ready') ? 'completed' : 'held';
  state.finishedAt = new Date(now()).toISOString(); await save();
  return state;
}

async function main() {
  const [mode, input, ...extra] = process.argv.slice(2);
  assert(['--run', '--resume'].includes(mode) && input && extra.length === 0,
    'Use --run APPROVED_MANIFEST.json or --resume APPROVED_MANIFEST.json');
  const config = await loadConfig(), api = new Paperclip(), installation = await readInstallation(config);
  const directory = path.join(config.runtimeRoot, 'todo'); await mkdir(directory, { recursive: true, mode: 0o700 });
  const manifestPath = await realpath(input);
  assert(path.dirname(manifestPath) === await realpath(directory), 'Approval file must be in the owner-only runtime todo directory');
  const manifest = validateTodoManifest(JSON.parse(await readFile(manifestPath, 'utf8')), config, installation);
  const key = digest(manifest), statePath = path.join(directory, `${key}.state.json`);
  let state;
  try {
    state = JSON.parse(await readFile(statePath, 'utf8'));
    assert(mode === '--resume' && state.status === 'active', 'Existing/terminal queue is evidence, not a retry job');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    assert(mode === '--run', 'No saved queue to resume');
    state = { schemaVersion: 1, roundId: `approved-todo:${key}`, manifestSha256: key,
      companyId: installation.companyId, configHash: config.configHash, status: 'active', tasks: {}, createdAt: new Date().toISOString() };
  }
  const release = await acquireLock(path.join(config.runtimeRoot, 'rounds/company.lock'), statePath, state, mode === '--resume');
  const save = () => atomicJson(statePath, state);
  try {
    await save();
    await drainTodo({ api, config, installation, manifest, state, save });
    console.log(JSON.stringify({ status: state.status, statePath,
      tasks: Object.entries(state.tasks).map(([issueId, task]) => ({ issueId, status: task.status, runId: task.runId })) }, null, 2));
  } catch (error) {
    state.status = 'failed'; state.failedAt = new Date().toISOString(); await save();
    throw new Error(`Approved todo stopped; inspect ${statePath}. No automatic retry.`, { cause: error });
  } finally { await release(); }
}
if (process.argv[1] && await realpath(process.argv[1]).catch(() => null) === fileURLToPath(import.meta.url))
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
