import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/admin-tasks-ui.test.mjs
// Exercise the real admin renderer and real role-based login orchestration.
process.env.TZ = "Asia/Shanghai";

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this._text = "";
    this.value = "";
    this.hidden = false;
    this.parentNode = null;
    this.classList = { toggle() {} };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) {
    this.children.push(...children);
    for (const child of children) child.parentNode = this;
  }
  remove() {
    const index = this.parentNode?.children.indexOf(this) ?? -1;
    if (index >= 0) this.parentNode.children.splice(index, 1);
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(" "); }
  set innerHTML(value) { this._text = String(value); this.children = []; }
  querySelectorAll() { return []; }
  querySelector(selector) {
    const className = selector.startsWith(".") ? selector.slice(1) : null;
    return this.children.find((child) => child.className === className) || null;
  }
  focus() {}
}

const elements = new Map();
globalThis.document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  createElement() { return new Element(); }
};

const state = { auth: { token: "test-only" } };
const requests = [];
const requestBodies = [];
const profile = { username: "qa_user", roles: [{ authority: "ROLE_USER" }] };
const fixture = new Map([
  ["/api/profile", profile],
  ["/api/admin/reports", [{ id: 9, sessionId: "example-thread", displayName: "虚构成年人", riskLevel: "HIGH", createdAt: "2026-10-01T17:00:00+00:00", summary: "测试", content: "虚构测试" }]],
  ["/api/admin/excel-records", []],
  ["/api/admin/alerts", []],
  ["/api/admin/reviews", []],
  ["/api/admin/conversations/example-thread", { title: "虚构会话", messages: [{ role: "USER", content: "虚构用户消息", createdAt: "2026-10-01T17:00:00+00:00" }] }]
]);
const time = "2026-10-01T17:00:00+00:00";
fixture.set("/api/admin/tool-jobs", [
  { id: 1, reportId: 9, kind: "EXCEL_REPORT", status: "SUCCESS", attempts: 1, maxAttempts: 3, createdAt: time, updatedAt: time },
  { id: 2, reportId: 9, kind: "RISK_ALERT", status: "PENDING", attempts: 1, maxAttempts: 3, lastError: "SMTPDataError: secret recipient/body", runAfter: time, createdAt: time, updatedAt: time, dependsOnJobId: 1 },
  { id: 3, reportId: 10, kind: "RISK_ALERT", status: "RUNNING", attempts: 1, maxAttempts: 3, createdAt: time, updatedAt: time },
  { id: 4, reportId: 10, kind: "RISK_ALERT", status: "PENDING", attempts: 0, maxAttempts: 3, runAfter: time, createdAt: time, updatedAt: time },
  { id: 5, reportId: 10, kind: "EXCEL_REPORT", status: "DEAD", attempts: 3, maxAttempts: 3, lastError: "OperationalError: INSERT INTO sensitive_table VALUES ('secret')\nSQL payload", createdAt: time, updatedAt: time }
]);
fixture.set("/api/admin/dead-letters", [{ id: 6, jobId: 5, reportId: 10, kind: "EXCEL_REPORT", reason: "<img src=x onerror=alert(1)>: secret", payload: "raw sensitive payload", createdAt: time }]);
const deferred = new Map();
const failures = new Set();
const api = async (path, options = {}) => {
  requests.push(path);
  if (options.body) requestBodies.push({ path, body: JSON.parse(options.body) });
  if (failures.has(path)) throw new Error("OperationalError: raw SQL/password must not render");
  if (deferred.has(path)) await deferred.get(path).promise;
  return { json: async () => fixture.get(path) ?? [] };
};
const app = new SyntheticModule(["state", "api", "displayTime", "isAdmin", "loadAgentStatus"], function () {
  this.setExport("state", state);
  this.setExport("api", api);
  this.setExport("displayTime", (value) => value ? new Date(value).toLocaleString() : "");
  this.setExport("isAdmin", (value) => value.roles?.some((role) => role.authority === "ROLE_ADMIN"));
  this.setExport("loadAgentStatus", async () => {});
});
const admin = new SourceTextModule(await readFile(new URL("../app/static/admin.js", import.meta.url), "utf8"));
await admin.link(() => app);
await admin.evaluate();
admin.namespace.initAdmin();
await admin.namespace.loadAdminDashboard();

const jobs = elements.get("#toolJobs");
const letters = elements.get("#deadLetters");
assert.equal(jobs.children.length, 5);
assert.match(jobs.textContent, /台账导出 #1.*成功/s);
assert.match(jobs.textContent, /预警通知 #2.*等待重试/s);
assert.match(jobs.textContent, /执行中/);
assert.match(jobs.textContent, /待执行/);
assert.match(jobs.textContent, /失败留存/);
assert.match(jobs.textContent, /尝试 1\/3 次/);
assert.match(jobs.textContent, /前置任务 #1/);
assert.match(jobs.textContent, /下次重试：.*10\/2.*(?:01:00|1:00:00 AM)/s);
assert.match(jobs.textContent, /邮件服务拒绝接收通知（SMTPDataError）/);
assert.match(jobs.textContent, /业务数据暂时不可写（OperationalError）/);
assert.doesNotMatch(jobs.textContent, /INSERT|sensitive_table|secret|SQL payload/);
assert.equal(letters.children.length, 1);
assert.match(letters.textContent, /报告 #10.*留存记录 #6/s);
assert.doesNotMatch(letters.textContent, /img|onerror|secret|raw sensitive payload/);

// Link to the real conversation view using a report already displayed on the page.
const link = jobs.children[0].children.find((child) => child.textContent === "查看关联对话");
assert.ok(link);
await link.listeners.click();
assert.ok(requests.includes("/api/admin/conversations/example-thread"));
assert.match(elements.get("#conversationDetail").textContent, /虚构用户消息/);

// Loading is visible; the two lists complete independently.
for (const path of ["/api/admin/tool-jobs", "/api/admin/dead-letters"]) {
  let resolve;
  deferred.set(path, { promise: new Promise((done) => { resolve = done; }), resolve });
}
fixture.set("/api/admin/tool-jobs", []);
fixture.set("/api/admin/dead-letters", []);
const loading = admin.namespace.loadTasks();
assert.equal(jobs.textContent, "读取中...");
assert.equal(letters.textContent, "读取中...");
deferred.get("/api/admin/tool-jobs").resolve();
await new Promise((resolve) => setImmediate(resolve));
assert.equal(jobs.textContent, "暂无后台任务。");
assert.equal(letters.textContent, "读取中...");
deferred.get("/api/admin/dead-letters").resolve();
await loading;
deferred.clear();
assert.equal(letters.textContent, "没有失败留存记录。");

// Failed reads never dump a raw transport/database error, and refresh can recover.
failures.add("/api/admin/tool-jobs");
await admin.namespace.loadTasks();
assert.equal(jobs.textContent, "任务状态读取失败，请刷新重试。");
assert.equal(letters.textContent, "没有失败留存记录。");
assert.doesNotMatch(jobs.textContent, /SQL|password/);
failures.clear();
await elements.get("#refreshTasks").listeners.click();
assert.equal(jobs.textContent, "暂无后台任务。");

// Human support workbench: claim, reply, offsite contact and all three outcomes.
const findButton = (root, text) => root.children.flatMap((child) => {
  if (child.textContent === text && child.listeners.click) return [child];
  return child.children?.length ? [findButton(child, text)] : [];
}).flat().find(Boolean);
fixture.set("/api/admin/reviews", [
  { reviewId: 41, status: "pending", riskLevel: "HIGH", handoffReason: "HIGH_RISK_KEYWORD", createdAt: time, recentContext: [] },
  { reviewId: 42, status: "in_progress", riskLevel: "HIGH", handoffReason: "HIGH_RISK_KEYWORD", createdAt: time, claimedBy: "qa_user", recentContext: [] }
]);
await admin.namespace.loadReviews();
const reviewCards = elements.get("#reviews").children;
assert.ok(findButton(reviewCards[0], "接手"));
assert.doesNotMatch(reviewCards[1].textContent, /放行|拒绝/);
await findButton(reviewCards[0], "接手").listeners.click();
assert.ok(requests.includes("/api/admin/reviews/41/claim"));
await findButton(reviewCards[1], "发送人工回复").listeners.click();
await findButton(reviewCards[1], "确认").listeners.click();
await findButton(reviewCards[1], "记录站外联系").listeners.click();
await findButton(reviewCards[1], "确认").listeners.click();
for (const label of ["持续关注", "转介", "完成处置"]) {
  await findButton(reviewCards[1], label).listeners.click();
  assert.ok(reviewCards[1].textContent.includes("确认"));
  await findButton(reviewCards[1], "确认").listeners.click();
}
assert.ok(requestBodies.some((request) => request.path === "/api/admin/reviews/42/reply"));
assert.ok(requestBodies.some((request) => request.path === "/api/admin/reviews/42/contact"));
assert.ok(requestBodies.some((request) => request.path === "/api/admin/reviews/42/decision" && request.body.decision === "complete"));

// Actual auth orchestration must not load tasks or reviews for a normal account.
const support = new SyntheticModule(["loadSupportProfile"], function () { this.setExport("loadSupportProfile", async () => {}); });
const plans = new SyntheticModule(["loadActionPlans", "resetPlanPanel"], function () {
  this.setExport("loadActionPlans", async () => {}); this.setExport("resetPlanPanel", () => {});
});
const chat = new SyntheticModule(["loadSessionList"], function () { this.setExport("loadSessionList", async () => {}); });
const auth = new SourceTextModule(await readFile(new URL("../app/static/auth.js", import.meta.url), "utf8"));
await auth.link((path) => ({ "/app.js": app, "/admin.js": admin, "/profile.js": support, "/action-plan.js": plans, "/chat.js": chat })[path]);
await auth.evaluate();
auth.namespace.initAuth();
globalThis.fetch = async () => ({ ok: true, json: async () => ({ accessToken: "disposable-test-token" }) });
elements.get("#username").value = "qa_user";
elements.get("#password").value = "disposable-test-password";
requests.length = 0;
await elements.get("#loginForm").listeners.submit({ preventDefault() {} });
assert.equal(elements.get("#adminView").hidden, true);
assert.equal(elements.get("#studentView").hidden, false);
assert.equal(requests.some((path) => path.startsWith("/api/admin/")), false);

profile.roles = [{ authority: "ROLE_ADMIN" }];
requests.length = 0;
await elements.get("#loginForm").listeners.submit({ preventDefault() {} });
assert.equal(elements.get("#adminView").hidden, false);
assert.equal(elements.get("#studentView").hidden, true);
assert.ok(requests.includes("/api/admin/tool-jobs"));
assert.ok(requests.includes("/api/admin/dead-letters"));
console.log("admin task/deadletter rendering and role access regressions passed");
