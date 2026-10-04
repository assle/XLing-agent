import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/action-plan-ui.test.mjs
// Load the real browser module without bringing in app bootstrap/network dependencies.
class Element {
  constructor() {
    this.children = [];
    this.style = {};
    this.listeners = {};
  }

  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  set innerHTML(value) { this.children = []; }
}

const elements = new Map();
globalThis.document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  createElement() { return new Element(); }
};

const state = { sessionId: "first-thread" };
let plans = [];
let pending = [];
let planResponse = null;
const requests = [];
const api = async (path) => {
  requests.push(path);
  return { json: async () => path.startsWith("/api/action-plans?") ? (planResponse ? planResponse(path) : plans) : pending };
};
const app = new SyntheticModule(["state", "api"], function () {
  this.setExport("state", state);
  this.setExport("api", api);
});
const module = new SourceTextModule(await readFile(new URL("../app/static/action-plan.js", import.meta.url), "utf8"));
await module.link(() => app);
await module.evaluate();
const { initActionPlan, loadActionPlans, handlePlanEvent } = module.namespace;
initActionPlan();

// Legacy duplicates must not return after the latest plan receives improved feedback.
plans = [
  { id: 2, status: "completed", items: [] },
  { id: 1, status: "active", items: [{ id: 10, content: "旧计划", order: 0 }] }
];
pending = [plans[1]];
await loadActionPlans();
assert.equal(state.activePlan, null);
assert.equal(state.pendingCheckInPlanId, null);
assert.equal(elements.get("#planPanel").hidden, true);

// Reload preserves the current plan's replacement and completed item.
plans = [{
  id: 3, status: "active", feedbackAvailable: true,
  items: [
    { id: 30, content: "保留紧急通知，十分钟整理安排", order: 0, completed: false },
    { id: 31, content: "完成的行动", order: 1, completed: true }
  ]
}];
pending = [{ id: 1 }];
await loadActionPlans();
assert.equal(state.activePlan.id, 3);
assert.equal(state.pendingCheckInPlanId, null);
assert.equal(elements.get("#checkinStart").hidden, true);
assert.equal(elements.get("#planProgress").textContent, "1/2 已完成");
assert.equal(elements.get("#planItems").children[0].children[1].textContent, plans[0].items[0].content);
assert.equal(elements.get("#planItems").children[1].children[0].disabled, true);

pending = [{ id: 3 }];
await loadActionPlans();
assert.equal(state.pendingCheckInPlanId, 3);
assert.equal(elements.get("#checkinStart").hidden, false);

// A newly created plan still appears immediately from its SSE event.
handlePlanEvent({ planId: 4, items: [{ id: 40, content: "新会话行动", order: 0, completed: false }] });
assert.equal(state.activePlan.id, 4);
assert.equal(state.pendingCheckInPlanId, 4);
assert.equal(elements.get("#planPanel").hidden, false);

// Switching two support threads must fetch/display their own plan. A newer
// completed second-thread plan must not hide the first thread's active plan.
const firstThreadPlan = { id: 5, status: "active", items: [{ id: 50, content: "第一会话的行动", order: 0 }] };
const secondThreadPlan = { id: 6, status: "completed", items: [{ id: 60, content: "第二会话的行动", order: 0 }] };
pending = [firstThreadPlan];
planResponse = (path) => new URL(path, "http://test.local").searchParams.get("sessionId") === "first-thread"
  ? [firstThreadPlan] : [secondThreadPlan];
state.sessionId = "second-thread";
await loadActionPlans();
assert.equal(state.activePlan, null);
state.sessionId = "first-thread";
await loadActionPlans();
assert.equal(state.activePlan.id, 5);
assert.equal(elements.get("#planItems").children[0].children[1].textContent, "第一会话的行动");
assert.equal(state.pendingCheckInPlanId, 5);
assert.equal(requests.at(-2), "/api/action-plans?sessionId=first-thread");

// Home/new conversation has no selected thread and must not fetch a stale plan.
state.sessionId = null;
const requestCount = requests.length;
await loadActionPlans();
assert.equal(requests.length, requestCount);
assert.equal(state.activePlan, null);
assert.equal(elements.get("#planPanel").hidden, true);

// A delayed response for an abandoned thread cannot overwrite the new view.
let resolveOldPlan;
planResponse = () => new Promise((resolve) => { resolveOldPlan = resolve; });
state.sessionId = "first-thread";
const oldLoad = loadActionPlans();
await Promise.resolve();
await Promise.resolve();
state.sessionId = null;
await loadActionPlans();
resolveOldPlan([firstThreadPlan]);
await oldLoad;
assert.equal(state.activePlan, null);
assert.equal(elements.get("#planPanel").hidden, true);

console.log("action-plan UI regressions passed");
