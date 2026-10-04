import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/chat-stream-ui.test.mjs
class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.classList = { toggle() {} };
    this.value = "";
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
const state = { auth: { token: "test-only" }, sending: false, pendingReview: false };
let suppliedEvents = [];
const api = async (path) => {
  if (path !== "/api/chat/stream") return { json: async () => [] };
  let read = false;
  return { body: { getReader: () => ({
    async read() {
      if (read) return { done: true };
      read = true;
      const text = suppliedEvents.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
      return { done: false, value: new TextEncoder().encode(text) };
    }
  }) } };
};
const app = new SyntheticModule(["state", "api", "setPill", "isAdmin"], function () {
  this.setExport("state", state);
  this.setExport("api", api);
  this.setExport("setPill", (el, value) => { el.textContent = value; });
  this.setExport("isAdmin", () => false);
});
const plans = new SyntheticModule(["handlePlanEvent", "resetPlanPanel", "loadActionPlans"], function () {
  this.setExport("handlePlanEvent", () => {});
  this.setExport("resetPlanPanel", () => {});
  this.setExport("loadActionPlans", async () => {});
});
const module = new SourceTextModule(await readFile(new URL("../app/static/chat.js", import.meta.url), "utf8"));
await module.link((path) => path === "/app.js" ? app : plans);
await module.evaluate();
module.namespace.initChat();

for (const events of [
  [],
  [{ type: "done" }],
  [{ type: "token", content: " \n" }, { type: "done" }],
  [{ type: "token", content: " \n" }, { type: "error", message: "回复生成未返回内容，请重试。" }]
]) {
  suppliedEvents = events;
  state.sessionId = "test-session";
  elements.get("#messageInput").value = "虚构输入";
  await elements.get("#chatForm").listeners.submit({ preventDefault() {} });
  assert.equal(elements.get("#sessionBadge").textContent, "ERROR");
  assert.match(elements.get("#messages").children.at(-1).bubble.textContent, /未返回内容.*重试/);
  assert.equal(elements.get("#sendButton").disabled, false);
  assert.equal(state.sending, false);
}

suppliedEvents = [{ type: "token", content: "有效回复" }, { type: "done" }];
elements.get("#messageInput").value = "再次发送";
await elements.get("#chatForm").listeners.submit({ preventDefault() {} });
assert.equal(elements.get("#sessionBadge").textContent, "DONE");
assert.equal(elements.get("#messages").children.at(-1).bubble.textContent, "有效回复");

// The progress badge follows each new reply, including ordinary replies without a cbt event.
for (const { message, cbt, hidden, label } of [
  { message: "先梳理工作压力", cbt: { active: true, complete: false, completedCount: 1 }, hidden: false, label: "结构化支持 · 1/4 · 可随时退出" },
  { message: "不继续追问，请讲 Python 字典", hidden: true },
  { message: "现在愿意继续梳理", cbt: { active: true, complete: false, completedCount: 2 }, hidden: false, label: "结构化支持 · 2/4 · 可随时退出" }
]) {
  suppliedEvents = [
    { type: "meta", sessionId: "test-session", noMemory: false },
    ...(cbt ? [{ type: "cbt", ...cbt }] : []),
    { type: "token", content: "有效回复" },
    { type: "done" }
  ];
  elements.get("#messageInput").value = message;
  await elements.get("#chatForm").listeners.submit({ preventDefault() {} });
  assert.equal(elements.get("#cbtTag").hidden, hidden, message);
  if (label) assert.equal(elements.get("#cbtTag").textContent, label);
}
console.log("chat empty-stream, retry, and per-reply CBT badge regressions passed");
