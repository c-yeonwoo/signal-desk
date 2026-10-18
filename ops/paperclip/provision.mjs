// Only this company's local resources are created; no server/After changes.
import assert from "node:assert/strict";
import { access, mkdir, realpath, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { loadConfig, Paperclip, adapterConfig, instruction, roleWorkspace, atomicJson, readInstallation,
  validateCompany, assertAdapter, activateInstallation, SOURCE } from "./lib.mjs";

const args = process.argv.slice(2);
assert(args.every((a) => ["--check", "--apply", "--enable-manual"].includes(a)));
const apply = args.includes("--apply"), enable = args.includes("--enable-manual");
assert(!enable || apply, "--enable-manual requires --apply");
const config = await loadConfig(), api = new Paperclip();
async function linkSources() {
  for (const name of ["company.json", "POLICY.md", "provision.mjs", "round.mjs", "verify.mjs"]) {
    const local = join(config.runtimeRoot, name), source = join(SOURCE, name);
    try { await symlink(source, local); }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      assert.equal(await realpath(local), await realpath(source), "Refusing unrelated runtime file");
    }
  }
}
const health = await api.call("/health");
assert.equal(health.version, "0.3.1"); assert.equal(health.deploymentMode, "local_trusted");
const companies = await api.call("/companies");
const otherCompanies = companies.filter((c) => c.name !== config.spec.name).map((c) => ({ id: c.id, name: c.name, status: c.status }));
if (!apply) {
  console.log(JSON.stringify({ mode: "check-only", roles: config.spec.agents.length,
    runtimeRoot: config.runtimeRoot, localServer: health.version, configHash: config.configHash }));
  process.exit(0);
}
await mkdir(config.runtimeRoot, { recursive: true });
// Existing initialized installation: verification is idempotent, no reset/re-pause.
try {
  await access(join(config.runtimeRoot, "installation.json"));
  const state = await readInstallation(config);
  const { company } = await validateCompany(api, config, state);
  if (enable && ["prepared", "activating", "activation_failed"].includes(state.phase)) {
    await activateInstallation(api, config, state);
  } else assert(!enable || company.status === "active", "Paused ready installation requires a reviewed activation, not reprovision");
  await linkSources();
  console.log(JSON.stringify({ mode: "verified-existing", companyId: company.id,
    url: `http://127.0.0.1:3100/${company.issuePrefix}/dashboard` }));
  process.exit(0);
} catch (error) { if (error.code !== "ENOENT") throw error; }

const matches = companies.filter((c) => c.name === config.spec.name);
assert(matches.length <= 1, "Ambiguous company name");
let company = matches[0];
if (company) {
  assert(company.description?.includes(`[${config.spec.marker}]`), "Refusing unrelated existing company");
  assert.equal(company.status, "paused", "Refusing to overwrite an active company");
  assert.equal((await api.call(`/companies/${company.id}/heartbeat-runs`)).length, 0);
} else company = await api.call("/companies", "POST", { name: config.spec.name,
  description: `[${config.spec.marker}] 비용 후 수익 개선을 위한 제한된 전문가 토론·개발 미션. 실주문 권한 없음.`,
  budgetMonthlyCents: 0 });
const cid = company.id;
await api.call(`/companies/${cid}`, "PATCH", { status: "paused", requireBoardApprovalForNewAgents: false });
let completed = false;
try {
  const roster = await api.call(`/companies/${cid}/agents`), ids = {}, workspaces = {}, roleWorkspaces = {};
  for (const role of config.spec.agents) {
    const cwd = roleWorkspace(config, role.key);
    await mkdir(join(cwd, "context"), { recursive: true });
    const localInstructions = `${config.policy}\n\n# 본인 작업 폴더\n${role.key} 전용 조율 폴더다. 서비스 코드/비밀/장부는 수정하지 않는다. 배정된 작업의 context/만 검토하고 Paperclip report에 결과를 저장한다.\n`;
    await writeFile(join(cwd, "AGENTS.md"), localInstructions, { mode: 0o600 });
    let agent = roster.find((a) => a.metadata?.signalDeskRole === role.key);
    if (agent) {
      assert.equal(agent.metadata?.harness, config.spec.marker);
      assert.equal(agent.status, "paused");
      assertAdapter(agent.adapterConfig, adapterConfig(config, role.key));
      const file = await api.call(`/agents/${agent.id}/instructions-bundle/file?path=AGENTS.md`);
      assert.equal(file.content, instruction(config, role));
    } else agent = await api.call(`/companies/${cid}/agents`, "POST", {
      name: role.name, role: role.role, title: role.title, reportsTo: role.key === "chief" ? null : ids.chief,
      capabilities: role.mission, adapterType: "codex_local", runner: "legacy", adapterConfig: adapterConfig(config, role.key),
      runtimeConfig: { heartbeat: { enabled: false, wakeOnDemand: false, allowExplicitManualWake: true,
        intervalSec: 0, maxConcurrentRuns: 1, maxDailyRuns: config.spec.limits.maxDailyRuns } },
      permissions: { canCreateAgents: false, canCreateSkills: false },
      metadata: { signalDeskRole: role.key, harness: config.spec.marker, executionMode: "goal-scoped-council" },
      instructionsBundle: { entryFile: "AGENTS.md", files: { "AGENTS.md": instruction(config, role) } },
    });
    await api.call(`/agents/${agent.id}`, "PATCH", { status: "paused" });
    ids[role.key] = agent.id; roleWorkspaces[role.key] = cwd;
  }
  assert.equal((await api.call(`/companies/${cid}/agents`)).length, config.spec.agents.length);
  const projects = await api.call(`/companies/${cid}/projects`);
  let project = projects.find((p) => p.name === "Signal Desk · 수익 개선 연구실");
  if (!project) project = await api.call(`/companies/${cid}/projects`, "POST", {
    name: "Signal Desk · 수익 개선 연구실", description: "유한 토론→반증→구현 미션. 개발 진단과 잠긴 연구 채택·실전 승격은 분리.",
    status: "planned", leadAgentId: ids.chief,
    workspace: { name: "chief", sourceType: "local_path", cwd: roleWorkspaces.chief, isPrimary: true },
  });
  const sources = await api.call(`/projects/${project.id}/workspaces`);
  for (const role of config.spec.agents) {
    const ws = sources.find((w) => w.cwd === roleWorkspaces[role.key]) ?? await api.call(`/projects/${project.id}/workspaces`, "POST", {
      name: role.key, sourceType: "local_path", cwd: roleWorkspaces[role.key], isPrimary: false });
    workspaces[role.key] = ws.id;
  }
  // Curated committed material, not .env, account DB, or untracked source archives.
  const contextFiles = ["docs/SIGNAL-DESK-UNIFIED-ROADMAP-2026-10-10.md", "docs/KR-NET-ALPHA-PLAN-2026-10-17.md",
    "docs/KR-RIDGE-SELECTION-RESULT-2026-10-17.md", "docs/KR-NET-ALPHA-EXECUTION-2026-10-18.md",
    "docs/KR-NET-ALPHA-ACCOUNT-REPLAY-2026-10-18.md", "docs/KR-NET-ALPHA-VENDOR-SOURCES-2026-10-18.md"];
  const baseSha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: config.repo, encoding: "utf8" }).trim();
  for (const cwd of Object.values(roleWorkspaces)) for (const file of contextFiles) {
    const raw = execFileSync("git", ["show", `${baseSha}:${file}`], { cwd: config.repo, maxBuffer: 4 * 1024 * 1024 });
    await writeFile(join(cwd, "context", file.split("/").at(-1)), raw, { mode: 0o400 });
  }
  await api.call(`/companies/${cid}`, "PATCH", { requireBoardApprovalForNewAgents: true });
  const state = { version: 1, marker: config.spec.marker, companyId: cid, projectId: project.id,
    agents: ids, workspaces, roleWorkspaces, repository: config.repo, configHash: config.configHash,
    baseSha, contextFiles, createdAt: new Date().toISOString(), otherCompanies, phase: "prepared",
    url: `http://127.0.0.1:3100/${company.issuePrefix}/dashboard` };
  await validateCompany(api, config, state);
  // Save identity before any idle/active transitions, including crash recovery.
  await atomicJson(join(config.runtimeRoot, "installation.json"), state);
  if (enable) {
    await activateInstallation(api, config, state);
  }
  // A local entry in the same folder as After, without copying drifting source files.
  const launcher = `# Signal Desk Paperclip 회사\n\n원본 설정: ${SOURCE}\n서비스: ${config.repo}\n대시보드: ${state.url}\n\n실행: node ${join(SOURCE, "verify.mjs")}\n토론: node ${join(SOURCE, "round.mjs")} --run --goal '구체적인 개발 미션'\n\n자동 예약·실주문·운영 전략 변경 없음. 구독 사용량 소모, OS 보안 격리 아님.\n`;
  await writeFile(join(config.runtimeRoot, "README.md"), launcher, { mode: 0o600 });
  await linkSources();
  await atomicJson(join(config.runtimeRoot, "installation.json"), state);
  completed = true;
  console.log(JSON.stringify({ companyId: cid, roles: Object.keys(ids).length, mode: enable ? "manual-ready" : "paused", url: state.url }));
} finally {
  await api.call(`/companies/${cid}`, "PATCH", { status: completed && enable ? "active" : "paused", requireBoardApprovalForNewAgents: true });
}
