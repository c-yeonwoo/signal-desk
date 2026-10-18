import assert from "node:assert/strict";
import { access, mkdir, readFile, realpath, rename, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";

export const SOURCE = dirname(fileURLToPath(import.meta.url));
export const ORIGIN = "http://127.0.0.1:3100";
export const ROLE_KEYS = ["chief", "quant", "regime", "events", "execution", "data", "engineering", "qa"];

export function validateSpec(spec) {
  assert.equal(spec.version, 1);
  assert.equal(spec.name, "Signal Desk");
  assert.equal(spec.marker, "signal-desk-harness-v1");
  assert.equal(spec.runtimeDirectory, ".local/share/paperclip-companies/signal-desk");
  assert.deepEqual(spec.agents.map((r) => r.key).sort(), [...ROLE_KEYS].sort());
  assert.equal(spec.agents[0].key, "chief", "Create the reporting root first");
  assert(spec.model && spec.reasoning && spec.command);
  assert.equal(spec.limits.maxConcurrentRuns, 2);
  assert.equal(spec.limits.maxRebuttalRounds, 1);
  assert(spec.limits.maxRunsPerRound >= 6 && spec.limits.maxRunsPerRound <= 7);
  assert(spec.limits.maxDailyRuns > 0 && spec.limits.maxDailyRuns <= 4);
  assert(spec.limits.timeoutSec > 0 && spec.limits.timeoutSec <= 600);
  for (const r of spec.agents) assert(r.name && r.role && r.title && r.mission && r.deliverable);
  return spec;
}

export async function canonicalRuntimeRoot(runtimeRoot) {
  try { return await realpath(runtimeRoot); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    // Provisioning can create a new root, but existing aliases must resolve to
    // the physical directory accepted by Codex workspace-write sandboxes.
    return runtimeRoot;
  }
}

export async function loadConfig() {
  const raw = await readFile(join(SOURCE, "company.json"), "utf8");
  const spec = validateSpec(JSON.parse(raw));
  const policy = await readFile(join(SOURCE, "POLICY.md"), "utf8");
  const repo = resolve(SOURCE, "../..");
  const runtimeRoot = await canonicalRuntimeRoot(resolve(homedir(), spec.runtimeDirectory));
  const command = resolve(homedir(), spec.command);
  await access(command, constants.X_OK);
  const configHash = createHash("sha256").update(raw).update("\0").update(policy).digest("hex");
  return { spec, policy, repo, runtimeRoot, planning: join(runtimeRoot, "planning"), command, configHash };
}

export class Paperclip {
  constructor(origin = ORIGIN) {
    assert.equal(origin, ORIGIN, "Only the existing local Paperclip instance is authorized");
  }
  async call(path, method = "GET", body) {
    assert(path.startsWith("/") && !path.startsWith("//") && !path.includes(".."), "Invalid local API path");
    const response = await fetch(new URL(`/api${path}`, ORIGIN), {
      method, headers: { "Content-Type": "application/json", Origin: ORIGIN },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000), redirect: "error",
    });
    // Never echo response bodies: server/adapter errors can contain credentials.
    assert(response.ok, `${method} ${path.split("?")[0]}: HTTP ${response.status}`);
    return response.json();
  }
}

export async function atomicJson(path, value) {
  await mkdir(dirname(path), { recursive: true });
  const temp = `${path}.${randomUUID()}.tmp`;
  await writeFile(temp, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  await rename(temp, path);
}

export function roleWorkspace(config, key) {
  assert(ROLE_KEYS.includes(key));
  return join(config.planning, key);
}

export function adapterConfig(config, key) {
  const cwd = roleWorkspace(config, key);
  return {
    cwd, command: config.command, engine: "cli", model: config.spec.model,
    modelReasoningEffort: config.spec.reasoning, timeoutSec: config.spec.limits.timeoutSec,
    graceSec: 10, fastMode: false, dangerouslyBypassApprovalsAndSandbox: false,
    extraArgs: ["--cd", cwd, "--skip-git-repo-check", "--sandbox", "workspace-write",
      "-c", "sandbox_workspace_write.network_access=true", "-c", 'approval_policy="never"',
      "-c", 'shell_environment_policy.exclude=["KIS_*","TOSS_*","DART_*","DATA_GO_KR_*","APCA_*","GH_TOKEN","GITHUB_TOKEN"]'],
  };
}

export function assertAdapter(actual, expected) {
  for (const [key, value] of Object.entries(expected)) assert.deepEqual(actual[key], value, `Adapter drift: ${key}`);
  const managed = ["instructionsFilePath", "instructionsRootPath", "instructionsEntryFile",
    "instructionsBundleMode", "paperclipSkillSync"];
  assert(Object.keys(actual).every((key) => key in expected || managed.includes(key)), "Unexpected adapter setting");
}

export function instruction(config, role) {
  return `${config.policy}\n\n# 담당: ${role.title}\n\n${role.mission}\n\n## 필수 결과물\n${role.deliverable}\n\n## 작업 경로\n본인 조율 폴더: ${roleWorkspace(config, role.key)}\n서비스 저장소(읽기만): ${config.repo}\n이 폴더의 context/는 controller가 고정 커밋에서 복사한 검토 자료다. 원본 repo의 .env/비밀/계좌/개인정보는 읽지 않는다. 구현 승인 작업이 따로 오기 전 서비스 파일·브랜치·원천 장부·실주문·매매 규칙을 변경하지 않는다.\n`;
}

export async function readInstallation(config) {
  const state = JSON.parse(await readFile(join(config.runtimeRoot, "installation.json"), "utf8"));
  assert.equal(state.configHash, config.configHash, "Configuration changed; do not silently overwrite a running company");
  assert.equal(state.repository, config.repo);
  assert.equal(state.marker, config.spec.marker);
  return state;
}

export function otherCompanyDrift(previous, current) {
  // Observational, not attribution: other operators own these companies.
  return previous.flatMap((old) => {
    const found = current.find((company) => company.id === old.id);
    if (!found) return [{ id: old.id, kind: "missing_from_listing" }];
    return found.name === old.name ? [] : [{ id: old.id, kind: "renamed" }];
  });
}

export async function validateCompany(api, config, state) {
  const company = await api.call(`/companies/${state.companyId}`);
  assert.equal(company.name, config.spec.name);
  assert(company.description?.includes(`[${config.spec.marker}]`), "Unrelated company");
  assert.equal(company.requireBoardApprovalForNewAgents, true);
  const agents = await api.call(`/companies/${company.id}/agents`);
  assert.equal(agents.length, config.spec.agents.length);
  for (const role of config.spec.agents) {
    const agent = agents.find((a) => a.id === state.agents[role.key]);
    assert(agent && agent.companyId === company.id && agent.metadata?.signalDeskRole === role.key);
    assert.equal(agent.metadata?.harness, config.spec.marker);
    assert.equal(agent.reportsTo, role.key === "chief" ? null : state.agents.chief);
    assert.equal(agent.adapterType, "codex_local");
    assertAdapter(agent.adapterConfig, adapterConfig(config, role.key));
    const h = agent.runtimeConfig?.heartbeat;
    assert.equal(h?.enabled, false); assert.equal(h?.wakeOnDemand, false);
    assert.equal(h?.allowExplicitManualWake, true); assert.equal(h?.maxConcurrentRuns, 1);
    assert.equal(h?.maxDailyRuns, config.spec.limits.maxDailyRuns);
    assert.equal(agent.permissions?.canCreateAgents, false); assert.equal(agent.permissions?.canCreateSkills, false);
    const file = await api.call(`/agents/${agent.id}/instructions-bundle/file?path=AGENTS.md`);
    assert.equal(file.content, instruction(config, role), `Role instruction drift: ${role.key}`);
  }
  const projects = await api.call(`/companies/${company.id}/projects`);
  assert(projects.some((p) => p.id === state.projectId), "Missing company project");
  const workspaces = await api.call(`/projects/${state.projectId}/workspaces`);
  for (const role of config.spec.agents) assert(workspaces.some((w) => w.id === state.workspaces[role.key]
    && w.cwd === roleWorkspace(config, role.key)), `Workspace drift: ${role.key}`);
  return { company, agents, projects };
}

export async function activateInstallation(api, config, state, persist = async () =>
  atomicJson(join(config.runtimeRoot, "installation.json"), state)) {
  assert(["prepared", "activating", "activation_failed"].includes(state.phase), "Not an incomplete activation");
  assert.equal((await api.call(`/companies/${state.companyId}/heartbeat-runs`)).length, 0,
    "Refusing activation recovery after business tasks started");
  state.activationAttempts = (state.activationAttempts ?? 0) + 1;
  assert(state.activationAttempts <= 2, "Activation retry limit; inspect the preserved installation");
  state.phase = "activating"; await persist();
  try {
    await api.call(`/companies/${state.companyId}`, "PATCH", { status: "active" });
    for (const id of Object.values(state.agents)) {
      await api.call(`/agents/${id}`, "PATCH", { status: "idle" });
      const deadline = Date.now() + 120000;
      while (true) {
        const a = await api.call(`/agents/${id}`);
        assert.equal(a.lifecycleError, null, "Runtime validation failed; inspect the local agent");
        if (a.status === "idle" && a.lifecycleState === "ready" && a.lifecycleHolds.length === 0) break;
        assert(Date.now() < deadline, "Agent readiness timed out, not necessarily an authentication failure");
        await delay(2000);
      }
    }
    state.phase = "ready"; await persist();
  } catch (error) {
    // Preserve IDs before activation; recover only our own resources, never After.
    const cleanup = await Promise.allSettled([
      api.call(`/companies/${state.companyId}`, "PATCH", { status: "paused", requireBoardApprovalForNewAgents: true }),
      ...Object.values(state.agents).map((id) => api.call(`/agents/${id}`, "PATCH", { status: "paused" })),
    ]);
    state.phase = "activation_failed";
    state.activationRollbackComplete = cleanup.every((item) => item.status === "fulfilled");
    await persist(); throw error;
  }
}
