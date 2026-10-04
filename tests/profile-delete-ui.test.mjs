import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/profile-delete-ui.test.mjs
// Actual deletion click callback, with transport outcomes and browser timers controlled.
class Element {
  constructor() { this.listeners = {}; this.textContent = ""; this.disabled = false; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

async function fixture(result, failure = false) {
  const elements = new Map();
  globalThis.document = { querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  } };
  const state = { auth: { token: "disposable-token" }, profile: { username: "qa_delete" } };
  const requests = [];
  const timers = [];
  let reloads = 0;
  globalThis.location = { reload() { reloads += 1; } };
  globalThis.setTimeout = (callback, ms) => { timers.push({ callback, ms }); };
  const app = new SyntheticModule(["state", "api", "openModal", "closeModal"], function () {
    this.setExport("state", state);
    this.setExport("api", async (path, options) => {
      requests.push({ path, options });
      if (failure) throw new Error("controlled request failure");
      return { json: async () => result };
    });
    this.setExport("openModal", (id) => { document.querySelector(`#${id}`).hidden = false; });
    this.setExport("closeModal", (id) => { document.querySelector(`#${id}`).hidden = true; });
  });
  const module = new SourceTextModule(await readFile(new URL("../app/static/profile.js", import.meta.url), "utf8"));
  await module.link(() => app);
  await module.evaluate();
  module.namespace.initProfile();
  return { state, elements, requests, timers, reloads: () => reloads };
}

const originalSetTimeout = globalThis.setTimeout;
try {
  const complete = await fixture({ deleted: true, businessDataDeleted: true, checkpointCleanupPending: false });
  await complete.elements.get("#confirmDataDelete").listeners.click();
  assert.equal(complete.state.auth.token, null);
  assert.equal(complete.state.profile, null);
  assert.match(complete.elements.get("#dataDeleteState").textContent, /已删除，正在退出/);
  assert.equal(complete.elements.get("#confirmDataDelete").disabled, true);
  assert.equal(complete.timers.length, 1);
  assert.equal(complete.timers[0].ms, 800);
  complete.timers[0].callback();
  assert.equal(complete.reloads(), 1);
  assert.deepEqual(complete.requests.map((request) => request.path), ["/api/account"]);

  const pending = await fixture({ deleted: false, businessDataDeleted: true, checkpointCleanupPending: true });
  await pending.elements.get("#confirmDataDelete").listeners.click();
  assert.equal(pending.state.auth.token, null);
  assert.equal(pending.state.profile, null);
  const explanation = pending.elements.get("#dataDeleteState").textContent;
  assert.match(explanation, /账号及业务数据已删除/);
  assert.match(explanation, /尚待清理/);
  assert.match(explanation, /下次启动时自动重试/);
  assert.match(explanation, /无需重新登录或重复删除/);
  assert.equal(pending.timers.length, 0, "Pending cleanup must not automatically hide the explanation");
  assert.equal(pending.reloads(), 0);
  assert.equal(pending.elements.get("#confirmDataDelete").textContent, "返回登录");
  assert.equal(pending.elements.get("#confirmDataDelete").disabled, false);
  pending.elements.get("#openDataDelete").listeners.click();
  assert.equal(pending.elements.get("#dataDeleteState").textContent, explanation, "Reopening the dialog must preserve pending cleanup information");
  await pending.elements.get("#confirmDataDelete").listeners.click();
  assert.equal(pending.reloads(), 1);
  assert.equal(pending.requests.length, 1, "Acknowledgement must not issue another DELETE with an invalidated account");

  const failed = await fixture(null, true);
  await failed.elements.get("#confirmDataDelete").listeners.click();
  assert.equal(failed.state.auth.token, "disposable-token");
  assert.equal(failed.state.profile.username, "qa_delete");
  assert.equal(failed.elements.get("#confirmDataDelete").disabled, false);
  assert.match(failed.elements.get("#dataDeleteState").textContent, /删除失败/);
  assert.equal(failed.timers.length, 0);
} finally {
  globalThis.setTimeout = originalSetTimeout;
}
console.log("profile deletion complete/pending/failure UI regressions passed");
