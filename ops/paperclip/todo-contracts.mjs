import { assert, digest } from './round-contracts.mjs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH = /^[0-9a-f]{64}$/;
const ROLES = new Set(['chief', 'quant', 'regime', 'events', 'execution', 'data', 'engineering', 'qa']);
const MODES = new Set(['source_review', 'research_implementation', 'research_qa', 'research_decision']);

// A local owner/controller approval, NOT authority inferred from an agent's
// report, issue status, role name, board comment, or company-wide todo search.
export function validateTodoManifest(value, config, installation) {
  assert(value?.schemaVersion === 1, 'Unknown todo manifest');
  assert(value.companyId === installation.companyId && value.projectId === installation.projectId,
    'Only the installed Signal Desk company/project may continue');
  assert(value.configHash === config.configHash && /^[0-9a-f]{40}$/.test(value.baseSha), 'Todo configuration/base mismatch');
  assert(typeof value.authority === 'string' && value.authority.trim().length >= 20,
    'Record the explicit user authorization before enrolling work');
  assert(typeof value.approvedAt === 'string' && Number.isFinite(Date.parse(value.approvedAt)), 'Approval time required');
  assert(Array.isArray(value.tasks) && value.tasks.length > 0 && value.tasks.length <= 32, 'Enroll 1–32 finite tasks');
  const seen = new Set();
  for (const task of value.tasks) {
    assert(UUID.test(task.issueId) && !seen.has(task.issueId), 'Invalid/duplicate approved issue');
    assert(ROLES.has(task.role) && installation.agents[task.role] && MODES.has(task.mode), 'Unknown role/task mode');
    assert((task.mode !== 'source_review' || task.role === 'data')
      && (task.mode !== 'research_implementation' || task.role === 'engineering')
      && (task.mode !== 'research_qa' || task.role === 'qa')
      && (task.mode !== 'research_decision' || ['chief', 'quant', 'regime', 'events', 'execution'].includes(task.role)),
    'Task mode does not match the authorized specialist');
    assert(HASH.test(task.descriptionSha256), 'Freeze each exact task description');
    assert(typeof task.scope === 'string' && task.scope.trim().length >= 20, 'A concrete approved scope is required');
    assert(Array.isArray(task.evidence) && task.evidence.length > 0
      && task.evidence.every((item) => typeof item === 'string' && item.trim().length >= 10), 'Fixed evidence references required');
    assert(task.limits?.liveChanges === false && task.limits?.paidSources === false
      && task.limits?.newAgentCalls === false && task.limits?.serviceRepositoryWrites === false,
    'Queue agents may not trade, buy data, delegate, or apply service-repository changes');
    assert(typeof task.limits.publicSourceAcquisition === 'boolean'
      && (task.mode === 'source_review' || task.limits.publicSourceAcquisition === false),
    'Public collection must be explicitly scoped to an approved source task');
    assert(Array.isArray(task.dependsOn) && new Set(task.dependsOn).size === task.dependsOn.length
      && task.dependsOn.every((id) => seen.has(id)), 'Dependencies must reference earlier approved tasks, never cycles or new work');
    seen.add(task.issueId);
  }
  return value;
}

export function assertTodoIssue(issue, task, manifest, installation) {
  assert(issue.id === task.issueId && issue.companyId === manifest.companyId && issue.projectId === manifest.projectId
    && issue.assigneeAgentId === installation.agents[task.role], 'Approved issue scope/assignment changed');
  assert(typeof issue.description === 'string' && digest(issue.description) === task.descriptionSha256,
    'Task instructions changed after approval; do not execute the new text');
}

export function todoAdmission({ issue, documents, runs, task }) {
  assert(Array.isArray(documents) && Array.isArray(runs), 'Unambiguous document/run lists required');
  // A done task can be reopened by a board comment. Neither the new todo status
  // nor a missing current report permits repeating ANY previously invoked task.
  if (documents.some((item) => item.key === 'report')) return 'held_existing_report';
  if (runs.some((run) => run.issueId === task.issueId || run.contextSnapshot?.issueId === task.issueId))
    return 'held_existing_run';
  if (issue.status !== 'todo') return `held_status_${issue.status}`;
  return 'ready';
}

// JSON.parse silently keeps the last duplicate key. A report is an execution
// gate, so reject ambiguous objects at every depth, including escaped spellings
// of the same key. Parse the grammar first, then walk its already-valid tokens.
export function parseUniqueJson(text) {
  const value = JSON.parse(text);
  let offset = 0;
  const whitespace = () => { while (/\s/.test(text[offset] ?? '') && offset < text.length) offset += 1; };
  const string = () => {
    const start = offset++;
    while (offset < text.length) {
      if (text[offset] === '\\') { offset += 2; continue; }
      if (text[offset++] === '"') return JSON.parse(text.slice(start, offset));
    }
    throw new Error('Unterminated JSON string');
  };
  const walk = () => {
    whitespace();
    if (text[offset] === '{') {
      offset += 1; whitespace();
      const keys = new Set();
      if (text[offset] === '}') { offset += 1; return; }
      while (true) {
        whitespace(); const key = string();
        assert(!keys.has(key), 'Duplicate JSON key is not an admissible continuation gate'); keys.add(key);
        whitespace(); offset += 1; // colon (the grammar was validated above)
        walk(); whitespace();
        if (text[offset++] === '}') return;
      }
    }
    if (text[offset] === '[') {
      offset += 1; whitespace();
      if (text[offset] === ']') { offset += 1; return; }
      while (true) {
        walk(); whitespace();
        if (text[offset++] === ']') return;
      }
    }
    if (text[offset] === '"') { string(); return; }
    while (offset < text.length && !/[\s,}\]]/.test(text[offset])) offset += 1;
  };
  walk();
  return value;
}

export function parseTodoGate(body) {
  const trimmed = body.trim();
  const blocks = [...trimmed.matchAll(/```json\s*\n([\s\S]*?)\n```/g)];
  assert(trimmed.startsWith('{') || blocks.length === 1, 'Report needs exactly one JSON continuation gate');
  let value;
  try { value = parseUniqueJson(trimmed.startsWith('{') ? trimmed : blocks[0][1]); }
  catch { throw new Error('Malformed continuation gate'); }
  assert(value && ['ready', 'blocked'].includes(value.continuation)
    && typeof value.reason === 'string' && value.reason.trim().length >= 10, 'Explicit ready/blocked gate and reason required');
  return { continuation: value.continuation, reason: value.reason };
}

export function readyTodoTasks(manifest, state, maxConcurrent = 2) {
  assert(Number.isInteger(maxConcurrent) && maxConcurrent >= 1 && maxConcurrent <= 2, 'Company concurrency must be 1 or 2');
  const active = Object.values(state.tasks).filter((item) => ['binding', 'invoking', 'running', 'cancelling'].includes(item.status));
  const busyRoles = new Set(active.map((item) => item.role));
  const ready = [];
  for (const task of manifest.tasks) {
    if (state.tasks[task.issueId] || busyRoles.has(task.role)) continue;
    if (!task.dependsOn.every((id) => state.tasks[id]?.status === 'done'
      && state.tasks[id].gate?.continuation === 'ready')) continue;
    ready.push(task); busyRoles.add(task.role);
    if (ready.length >= Math.max(0, maxConcurrent - active.length)) break;
  }
  return active.length >= maxConcurrent ? [] : ready;
}
