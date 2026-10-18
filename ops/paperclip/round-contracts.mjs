import { createHash } from 'node:crypto';

export const STAGES = Object.freeze([
  { id: 'quant-proposal', role: 'quant', depends: [] },
  { id: 'execution-proposal', role: 'execution', depends: [] },
  { id: 'regime-proposal', role: 'regime', depends: [] },
  { id: 'qa-critique', role: 'qa', depends: ['quant-proposal', 'execution-proposal', 'regime-proposal'] },
  { id: 'quant-reply', role: 'quant', depends: ['quant-proposal', 'execution-proposal', 'regime-proposal', 'qa-critique'] },
  { id: 'chief-decision', role: 'chief', depends: ['quant-proposal', 'execution-proposal', 'regime-proposal', 'qa-critique', 'quant-reply'] },
].map((stage) => Object.freeze({ ...stage, depends: Object.freeze(stage.depends) })));

export const TERMINAL_RUNS = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
export function digest(value) {
  return createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
}
export function assert(condition, message) {
  if (!condition) throw new Error(message);
}
export function roundKey(goal, configHash, baseSha) {
  return digest({ goal: goal.trim(), configHash, baseSha });
}
export function nextStages(state, maxConcurrent = 2) {
  assert(Number.isInteger(maxConcurrent) && maxConcurrent >= 1 && maxConcurrent <= 2, 'Concurrency must be 1 or 2');
  const active = STAGES.filter((stage) => ['invoking', 'running'].includes(state.stages[stage.id]?.status));
  const ready = STAGES.filter((stage) => !state.stages[stage.id] || state.stages[stage.id].status === 'pending')
    .filter((stage) => stage.depends.every((id) => state.stages[id]?.status === 'done'));
  return ready.slice(0, Math.max(0, maxConcurrent - active.length));
}
export function reportEvidence({ issue, run, document, revisions, activity, expected }) {
  assert(issue.id === expected.issueId && issue.companyId === expected.companyId && issue.projectId === expected.projectId, 'Issue scope mismatch');
  assert(issue.assigneeAgentId === expected.agentId && issue.status === 'done', 'Assigned agent must finish its own task');
  assert(run.id === expected.runId && run.companyId === expected.companyId && run.agentId === expected.agentId, 'Run scope mismatch');
  assert(run.issueId === issue.id || run.contextSnapshot?.issueId === issue.id, 'Run must reference the exact task');
  assert(run.status === 'succeeded' && run.exitCode === 0 && !run.errorCode && !run.signal, 'Agent process did not succeed cleanly');
  assert(document.companyId === expected.companyId && document.issueId === issue.id && document.key === 'report', 'Report scope mismatch');
  const revision = revisions.find((item) => item.id === document.latestRevisionId);
  assert(revision && revision.documentId === document.id && revision.issueId === issue.id && revision.companyId === expected.companyId, 'Report revision missing or foreign');
  assert(revision.createdByAgentId === expected.agentId && !revision.createdByUserId, 'Report revision was not authored by the assigned agent');
  assert(revision.revisionNumber === document.latestRevisionNumber && revision.body === document.body && typeof revision.body === 'string' && revision.body.trim(), 'Report body/revision mismatch');
  const attribution = activity.find((entry) => entry.companyId === expected.companyId && entry.entityId === issue.id
    && ['issue.document_created', 'issue.document_updated'].includes(entry.action)
    && entry.actorType === 'agent' && entry.actorId === expected.agentId && entry.runId === expected.runId
    && entry.details?.key === 'report' && entry.details.documentId === document.id
    && entry.details.revisionNumber === revision.revisionNumber);
  assert(attribution, 'No server activity attributes this report revision to the exact run');
  return { issueId: issue.id, runId: run.id, agentId: expected.agentId, documentId: document.id,
    revisionId: revision.id, revisionNumber: revision.revisionNumber, body: revision.body,
    bodySha256: digest(revision.body), activityId: attribution.id };
}

export function parseDecision(body, evidence) {
  const trimmed = body.trim();
  const blocks = [...trimmed.matchAll(/```json\s*\n([\s\S]*?)\n```/g)];
  assert(trimmed.startsWith('{') || blocks.length === 1, 'Chief report must contain one JSON decision');
  let decision;
  try { decision = JSON.parse(trimmed.startsWith('{') ? trimmed : blocks[0][1]); }
  catch { throw new Error('Malformed chief decision JSON'); }
  assert(decision && typeof decision === 'object' && !Array.isArray(decision), 'Decision must be an object');
  assert(['implementation_development_diagnostic', 'reject', 'blocked_needs_user_decision'].includes(decision.decision), 'No live promotion or unknown decision is allowed');
  assert(decision.scope === 'development_diagnostic' && decision.liveChanges === false && decision.adoptionClaim === false, 'Decision may not promote a strategy or change production');
  assert(typeof decision.reason === 'string' && decision.reason.trim(), 'Decision needs an evidence-based reason');
  assert(Array.isArray(decision.evidenceRevisions) && evidence.every((item) => decision.evidenceRevisions.includes(item.revisionId))
    && decision.evidenceRevisions.every((id) => evidence.some((item) => item.revisionId === id)), 'Decision must cite exactly the actual earlier report revisions');
  if (decision.decision !== 'implementation_development_diagnostic') {
    assert(decision.candidate === null, 'Rejected/blocked decision cannot authorize a candidate');
    return decision;
  }
  const candidate = decision.candidate;
  assert(candidate && candidate.owner === 'engineering' && typeof candidate.name === 'string' && candidate.name.trim(), 'Exactly one engineering-owned candidate is required');
  assert(Array.isArray(candidate.files) && candidate.files.length > 0 && candidate.files.length <= 8, 'Candidate needs 1–8 concrete owned files');
  for (const file of candidate.files) {
    assert(typeof file === 'string' && !file.includes('..') && !file.includes('\\') && !file.startsWith('/'), 'Candidate paths must be safe relative paths');
    assert(/^(scripts\/measure\/|tests\/|docs\/|src\/signal_desk\/signals\/research_)/.test(file)
      && !/(?:^|\/)\.env(?:\.|$)|(?:^|\/)\.git(?:\/|$)/.test(file), 'Candidate is restricted to development research/diagnostics');
    assert(!/^docs\/preregistered\.toml$/i.test(file)
      && !/^docs\/kr-net-alpha-(?:plan|tradability|action-clocks)(?:-[^/]*)?\.(?:json|md)$/i.test(file),
    'Registered and frozen plan/tradability/action-clock contracts may not be candidate-owned files');
  }
  assert(typeof candidate.fixedComparison === 'string' && candidate.fixedComparison.trim().length >= 20, 'Fixed comparison protocol is required');
  assert(typeof candidate.stopCondition === 'string' && candidate.stopCondition.trim().length >= 10, 'An explicit stop condition is required');
  assert(candidate.limits && candidate.limits.preserveSelection === true && candidate.limits.noSourceAcquisition === true
    && candidate.limits.noLiveScoreOrOrders === true && candidate.limits.noCertificationChanges === true,
  'Candidate must preserve selection, data budget, production, and certification locks');
  return decision;
}

export function assertStageState(state) {
  assert(state.schemaVersion === 1 && typeof state.roundId === 'string' && state.roundId === state.key, 'Unknown round state');
  assert(Number.isInteger(state.invocationCount) && state.invocationCount >= 0 && state.invocationCount <= 7, 'Invalid round invocation count');
  assert(Object.keys(state.stages).every((id) => STAGES.some((stage) => stage.id === id)), 'Unknown or extra stage');
  const runIds = Object.values(state.stages).flatMap((stage) => stage.runId ? [stage.runId] : []);
  assert(new Set(runIds).size === runIds.length && runIds.length <= STAGES.length, 'Duplicate or excessive stage runs');
  for (const stage of STAGES) {
    const saved = state.stages[stage.id];
    if (!saved) continue;
    assert(saved.role === stage.role, 'Stage role changed');
    assert(['pending', 'creating', 'issue_created', 'bound', 'invoking', 'running', 'done', 'failed'].includes(saved.status), 'Unknown stage status');
    if (saved.status !== 'pending') assert(stage.depends.every((id) => state.stages[id]?.status === 'done'), 'Stage ran before its required evidence');
    if (saved.status === 'done') assert(saved.runId && saved.report?.revisionId, 'Completed stage lacks run/report evidence');
  }
}
