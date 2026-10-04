import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/chat-history-ui.test.mjs
process.env.TZ = "Asia/Shanghai";

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.classList = { toggle() {} };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(child) { this.children.push(child); }
  set innerHTML(value) {
    this.html = value;
    this.children = [];
    this.bubble = value.includes('class="bubble"') ? new Element() : null;
  }
  querySelector(selector) { return selector === ".bubble" ? this.bubble : null; }
  querySelectorAll() { return []; }
}

const elements = new Map();
globalThis.document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  createElement() { return new Element(); },
  addEventListener() {}
};

const state = { auth: { token: "test-only" }, sending: false };
const conversation = {
  pendingReview: false, noMemory: false,
  messages: [
    { role: "USER", content: "虚构用户消息" },
    { role: "ASSISTANT", content: "虚构回复" }
  ]
};
const sessions = [{
  sessionId: "history", title: "虚构会话", updatedAt: "2026-10-01T17:05:00+00:00", messageCount: 2
}];
const api = async (path) => ({ json: async () => path === "/api/sessions" ? sessions : conversation });
const app = new SyntheticModule(["state", "api", "setPill", "isAdmin"], function () {
  this.setExport("state", state);
  this.setExport("api", api);
  this.setExport("setPill", (el, value) => { el.textContent = value; });
  this.setExport("isAdmin", () => false);
});
let historyLoaded;
const plans = new SyntheticModule(["handlePlanEvent", "resetPlanPanel", "loadActionPlans"], function () {
  this.setExport("handlePlanEvent", () => {});
  this.setExport("resetPlanPanel", () => {});
  this.setExport("loadActionPlans", async () => { historyLoaded(); });
});
const module = new SourceTextModule(await readFile(new URL("../app/static/chat.js", import.meta.url), "utf8"));
await module.link((path) => path === "/app.js" ? app : plans);
await module.evaluate();
module.namespace.initChat();

// Select history through the same listener used by a browser click.
const loaded = new Promise((resolve) => { historyLoaded = resolve; });
elements.get("#sessionList").listeners.click({
  target: { closest: () => ({ dataset: { sessionId: "history" } }) }
});
await loaded;
const rows = elements.get("#messages").children;
assert.equal(rows[0].className, "message user");
assert.match(rows[0].html, /class="message-role">我<\/div>/);
assert.equal(rows[0].bubble.textContent, "虚构用户消息");
assert.equal(rows[1].className, "message assistant");
assert.match(rows[1].html, /class="message-role">Xling<\/div>/);

// The UTC instant crosses midnight in Shanghai, rather than displaying the stored UTC clock locally.
await module.namespace.loadSessionList();
assert.match(elements.get("#sessionList").html, /10\/2.*01:05/);
console.log("chat history role and Shanghai time regressions passed");
