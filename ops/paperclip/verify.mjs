// Read-only verification, also valid after actual council tasks have run.
import assert from "node:assert/strict";
import { loadConfig, otherCompanyDrift, Paperclip, readInstallation, validateCompany } from "./lib.mjs";

assert.equal(process.argv.length, 2, "No mutating or reset flags supported");
const config = await loadConfig(), api = new Paperclip(), state = await readInstallation(config);
const { company, agents } = await validateCompany(api, config, state);
assert.equal(company.status, "active");
for (const a of agents) {
  assert(["idle", "running"].includes(a.status));
  assert.equal(a.lifecycleState, "ready"); assert.equal(a.lifecycleError, null);
}
const runs = await api.call(`/companies/${company.id}/heartbeat-runs`);
const current = await api.call("/companies");
// Other operators may rename/archive/delete a different company. Report the
// observation without repairing it or misclassifying our company as unhealthy.
const externalCompanyDrift = otherCompanyDrift(state.otherCompanies, current);
console.log(JSON.stringify({ company: company.name, roles: agents.length, taskRuns: runs.length,
  ready: true, automaticWakeups: false, maxParallelPerRound: config.spec.limits.maxConcurrentRuns,
  paidApiBudgetApproved: false, liveTradingAuthority: false, externalCompanyDrift, url: state.url }));
