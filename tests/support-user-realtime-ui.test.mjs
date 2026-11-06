import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.classList = { toggle() {} };
    this.dataset = {};
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

const queuedFrames = [];
let waitingRead = null;
const encoder = new TextEncoder();
const streamResponse = {
  body: {
    getReader: () => ({
      async read() {
        if (queuedFrames.length) return { done: false, value: queuedFrames.shift() };
        if (waitingRead) return waitingRead;
        return new Promise((resolve) => { waitingRead = resolve; });
      },
      async cancel() {
        if (waitingRead) {
          const resolve = waitingRead;
          waitingRead = null;
          resolve({ done: true });
        }
      },
      releaseLock() {}
    })
  }
};
function pushEvent(event) {
  const frame = encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  if (waitingRead) {
    const resolve = waitingRead;
    waitingRead = null;
    resolve({ done: false, value: frame });
  } else queuedFrames.push(frame);
}

const conversation = {
  sessionId: "history", pendingReview: true, supportStatus: "in_progress", noMemory: false,
  messages: [{ messageId: 1, role: "USER", content: "用户留言" }]
};
let chatEvents = [];
const api = async (path) => {
  if (path === "/api/chat/stream") {
    let read = false;
    return {
      body: {
        getReader: () => ({
          async read() {
            if (read) return { done: true };
            read = true;
            return {
              done: false,
              value: encoder.encode(chatEvents.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""))
            };
          },
          async cancel() {},
          releaseLock() {}
        })
      }
    };
  }
  if (path === "/api/sessions/history/events") return streamResponse;
  if (path === "/api/sessions/history") return { json: async () => conversation };
  return { json: async () => [] };
};
const state = { auth: { token: "test-only" }, sending: false };
const app = new SyntheticModule(["state", "api", "setPill", "isAdmin"], function () {
  this.setExport("state", state);
  this.setExport("api", api);
  this.setExport("setPill", () => {});
  this.setExport("isAdmin", () => false);
});
const plans = new SyntheticModule(["handlePlanEvent", "resetPlanPanel", "loadActionPlans", "bindActionPlanObjectSender"], function () {
  this.setExport("handlePlanEvent", () => {});
  this.setExport("resetPlanPanel", () => {});
  this.setExport("loadActionPlans", async () => {});
  this.setExport("bindActionPlanObjectSender", () => {});
});
const module = new SourceTextModule(await readFile(new URL("../app/static/chat.js", import.meta.url), "utf8"));
await module.link(async (path) => {
  if (path === "/app.js") return app;
  if (path === "/sse.js") return new SourceTextModule(await readFile(new URL("../app/static/sse.js", import.meta.url), "utf8"));
  return plans;
});
await module.evaluate();
module.namespace.initChat();

elements.get("#sessionList").listeners.click({
  target: { closest: () => ({ dataset: { sessionId: "history" } }) }
});
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(elements.get("#messages").children.length, 1);

const reply = {
  type: "support_message", sessionId: "history", reviewId: 7, supportStatus: "in_progress",
  messageId: 99, role: "SUPPORT", content: "支持人员的实时回复"
};
pushEvent(reply);
pushEvent(reply);
await new Promise((resolve) => setTimeout(resolve, 10));
const rows = elements.get("#messages").children;
assert.equal(rows.length, 2);
assert.equal(rows[1].className, "message support");
assert.equal(rows[1].bubble.textContent, "支持人员的实时回复");

module.namespace.stopSessionRealtime();
state.auth.token = null;
conversation.messages.push({ messageId: 100, role: "USER", content: "接手后补充的细节" });
chatEvents = [
  { type: "meta", sessionId: "history", noMemory: false },
  { type: "user_message", sessionId: "history", messageId: 100, supportStatus: "in_progress" },
  { type: "done", sessionId: "history" }
];
elements.get("#messageInput").value = "接手后补充的细节";
await elements.get("#chatForm").listeners.submit({ preventDefault() {} });
await new Promise((resolve) => setTimeout(resolve, 10));
const afterFollowUp = elements.get("#messages").children;
assert.equal(afterFollowUp.some((row) => row.className === "message assistant"), false);
assert.equal(afterFollowUp.at(-1).bubble.textContent, "接手后补充的细节");
module.namespace.stopSessionRealtime();
console.log("user realtime support message regressions passed");
