import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, mkdir, realpath, symlink, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { adapterConfig, assertAdapter, activateInstallation, canonicalRuntimeRoot, otherCompanyDrift, Paperclip, ROLE_KEYS, validateSpec } from "./lib.mjs";

const spec = JSON.parse(await readFile(new URL("./company.json", import.meta.url), "utf8"));
test("an existing runtime alias resolves to the physical sandbox root", async () => {
  const temp = await mkdtemp(join(tmpdir(), "signal-runtime-"));
  try {
    const target = join(temp, "actual"), alias = join(temp, "alias");
    await mkdir(target); await symlink(target, alias);
    assert.equal(await canonicalRuntimeRoot(alias), await realpath(target));
    const absent = join(temp, "not-provisioned");
    assert.equal(await canonicalRuntimeRoot(absent), absent);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
test("external company drift is reported without mutation or false ownership claims", () => {
  const previous = [{ id: "a", name: "After" }, { id: "b", name: "Archive" }];
  const snapshot = structuredClone(previous);
  assert.deepEqual(otherCompanyDrift(previous, [{ id: "a", name: "After", status: "paused" }]),
    [{ id: "b", kind: "missing_from_listing" }]);
  assert.deepEqual(otherCompanyDrift(previous, [{ id: "a", name: "Renamed" }, previous[1]]),
    [{ id: "a", kind: "renamed" }]);
  assert.deepEqual(previous, snapshot);
});
test("finite role roster and execution ceilings are explicit", () => {
  assert.equal(validateSpec(spec), spec);
  for (const change of [
    { agents: spec.agents.slice(1) }, { limits: { ...spec.limits, maxRunsPerRound: 20 } },
    { limits: { ...spec.limits, maxConcurrentRuns: 8 } },
    { runtimeDirectory: ".local/share/paperclip-companies/after" },
  ]) assert.throws(() => validateSpec({ ...spec, ...change }));
});
test("role write areas do not include service or another company", () => {
  const config = { spec, planning: "/tmp/signal-company/planning", repo: "/tmp/service", command: "/tmp/codex" };
  const paths = new Set();
  for (const role of ROLE_KEYS) {
    const a = adapterConfig(config, role); paths.add(a.cwd);
    assert(!a.extraArgs.includes("--add-dir"));
    assert(!a.extraArgs.includes(config.repo));
    assert.equal(a.dangerouslyBypassApprovalsAndSandbox, false);
    assert(a.extraArgs.includes("workspace-write"));
    assert(a.extraArgs.some((v) => v.includes("shell_environment_policy.exclude")));
  }
  assert.equal(paths.size, 8);
});
test("managed instructions do not hide unapproved execution settings", () => {
  const expected = { cwd: "/tmp/company", extraArgs: ["--sandbox", "workspace-write"] };
  assertAdapter({ ...expected, instructionsFilePath: "/tmp/managed/AGENTS.md" }, expected);
  assert.throws(() => assertAdapter({ ...expected, extraArgs: ["--dangerously-bypass-approvals-and-sandbox"] }, expected));
  assert.throws(() => assertAdapter({ ...expected, env: { API_KEY: "SYNTHETIC" } }, expected));
});
test("client rejects remote and cross-origin URLs before network", async () => {
  assert.throws(() => new Paperclip("https://example.com"));
  const api = new Paperclip();
  for (const path of ["//example.com", "/../../other", "https://example.com"]) {
    await assert.rejects(api.call(path), /Invalid local API path/);
  }
});
test("partial activation saves identity, pauses only owned resources and resumes explicitly", async () => {
  const state = { companyId: "own-company", agents: { chief: "own-chief", quant: "own-quant" }, phase: "prepared" };
  const calls = [], phases = []; let fail = true;
  const api = { async call(path, method = "GET", body) {
    calls.push({ path, method, body });
    if (path.endsWith("heartbeat-runs")) return [];
    if (method === "GET" && path === "/agents/own-quant" && fail) throw new Error("readiness unavailable");
    if (method === "GET" && path.startsWith("/agents/")) return { status: "idle", lifecycleState: "ready", lifecycleError: null, lifecycleHolds: [] };
    return {};
  } };
  const persist = async () => phases.push(state.phase);
  await assert.rejects(activateInstallation(api, {}, state, persist), /readiness unavailable/);
  assert.deepEqual(phases, ["activating", "activation_failed"]);
  assert.equal(state.activationRollbackComplete, true);
  for (const id of ["own-chief", "own-quant"]) assert(calls.some((c) => c.path === `/agents/${id}` && c.body?.status === "paused"));
  assert(calls.every((c) => c.path.includes("own-")));
  fail = false;
  await activateInstallation(api, {}, state, persist);
  assert.equal(state.phase, "ready"); assert.equal(state.activationAttempts, 2);
  state.phase = "activation_failed";
  await assert.rejects(activateInstallation(api, {}, state, persist), /retry limit/);
});
