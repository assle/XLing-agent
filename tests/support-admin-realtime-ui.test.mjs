import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.classList = { toggle() {} };
    this.value = "";
    this.checked = false;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(child) {
    child.parent = this;
    this.children.push(child);
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
  }
  focus() {}
  set innerHTML(value) { this.children = []; this.html = value; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const all = [];
    const visit = (node) => {
      for (const child of node.children) {
        all.push(child);
        visit(child);
      }
    };
    visit(this);
    if (selector === ".review-note-row") return all.filter((node) => node.className === "review-note-row");
    if (selector === "input" || selector === ".review-note-row input") return all.filter((node) => node.tag === "input");
    if (selector === ".report") return all.filter((node) => node.className === "report");
    const reviewMatch = selector.match(/^\.review-item\[data-review-id="(\d+)"\]$/);
    if (reviewMatch) return all.filter((node) => node.dataset.reviewId === reviewMatch[1]);
    if (selector === ".review-decisions") return all.filter((node) => node.className === "review-decisions");
    if (selector === ".review-summary") return all.filter((node) => node.className === "review-summary");
    if ([".primary", ".review-note-row .primary"].includes(selector)) {
      return all.filter((node) => node.className === "primary");
    }
    return [];
  }
}

const elements = new Map();
globalThis.document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  createElement(tag) { return new Element(tag); },
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
      async cancel() {},
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

let snapshotLoads = 0;
const replies = [];
const api = async (path, options = {}) => {
  if (path === "/api/admin/reviews/events") return streamResponse;
  if (path === "/api/admin/reviews") {
    snapshotLoads += 1;
    return { json: async () => [] };
  }
  if (path === "/api/admin/reviews/7/reply") {
    const payload = JSON.parse(options.body);
    replies.push(payload);
    pushEvent({
      type: "support_message", reviewId: 7, sessionId: "session-7", supportStatus: "in_progress",
      messageId: 100 + replies.length, role: "SUPPORT", content: payload.content
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    return { json: async () => ({ messageId: 100 + replies.length }) };
  }
  return { json: async () => [] };
};
const app = new SyntheticModule(["api", "displayTime"], function () {
  this.setExport("api", api);
  this.setExport("displayTime", () => "测试时间");
});
const admin = new SourceTextModule(await readFile(new URL("../app/static/admin.js", import.meta.url), "utf8"));
await admin.link(async (path) => path === "/sse.js"
  ? new SourceTextModule(await readFile(new URL("../app/static/sse.js", import.meta.url), "utf8"))
  : app);
await admin.evaluate();
admin.namespace.initAdmin();
admin.namespace.startAdminRealtime();
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(snapshotLoads, 1);

const review = {
  reviewId: 7, sessionId: "session-7", threadId: "session-7", status: "in_progress",
  supportStatus: "in_progress", riskLevel: "HIGH", waitSeconds: 5, handoffReason: "HIGH_RISK_KEYWORD",
  claimedBy: "support-admin", recentContext: [{ messageId: 1, role: "USER", content: "原始留言" }]
};
pushEvent({ type: "status_changed", reviewId: 7, sessionId: "session-7", supportStatus: "in_progress", review });
await new Promise((resolve) => setTimeout(resolve, 10));
const reviews = elements.get("#reviews");
assert.equal(reviews.children.length, 1);
const decisions = reviews.querySelector(".review-decisions");
const replyButton = decisions.children.find((node) => node.className === "decide-reply");
replyButton.listeners.click();
const draft = reviews.querySelector(".review-note-row input");
draft.value = "正在输入的人工回复";
const requestId = reviews.querySelector(".review-note-row").dataset.clientRequestId;

pushEvent({
  type: "user_message", reviewId: 7, sessionId: "session-7", supportStatus: "in_progress",
  messageId: 2, role: "USER", content: "用户新的补充", createdAt: "2026-11-05T00:00:00Z"
});
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(snapshotLoads, 1);
assert.equal(reviews.querySelector(".review-note-row input").value, "正在输入的人工回复");
assert.equal(reviews.querySelector(".review-note-row").dataset.clientRequestId, requestId);
assert.equal(reviews.querySelector(".review-summary").textContent.includes("用户新的补充"), true);

await reviews.querySelector(".review-note-row .primary").listeners.click();
assert.equal(reviews.querySelector(".review-note-row input").value, "");
assert.equal(reviews.querySelector(".review-note-row .primary").disabled, false);
const secondRequestId = reviews.querySelector(".review-note-row").dataset.clientRequestId;
assert.notEqual(secondRequestId, requestId);
reviews.querySelector(".review-note-row input").value = "第二条人工回复";
await reviews.querySelector(".review-note-row .primary").listeners.click();
assert.equal(reviews.querySelector(".review-note-row input").value, "");
assert.equal(replies.length, 2);
assert.deepEqual(replies.map((reply) => reply.content), ["正在输入的人工回复", "第二条人工回复"]);
assert.notEqual(replies[0].clientRequestId, replies[1].clientRequestId);
admin.namespace.stopAdminRealtime();
console.log("admin realtime merge and draft preservation regressions passed");
