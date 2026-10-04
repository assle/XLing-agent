import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule, SyntheticModule } from "node:vm";

// Run with: node --experimental-vm-modules tests/screening-intro-ui.test.mjs
// Real async scale-selection callback and intro rendering, before any consent/submission.
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this._text = ""; this.classList = { toggle() {} }; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  set innerHTML(value) { this._text = String(value); this.children = []; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(" "); }
}
const elements = new Map();
const choices = ["GAD-7", "PHQ-9"].map((scale) => {
  const choice = new Element(); choice.dataset.scale = scale; return choice;
});
globalThis.document = {
  querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
  querySelectorAll(selector) { return selector === ".scale-choice" ? choices : []; },
  createElement() { return new Element(); }
};
const requests = [];
let resolve;
let delayed = new Promise((done) => { resolve = done; });
const options = [
  { value: 0, label: "完全没有" }, { value: 1, label: "有几天" },
  { value: 2, label: "超过一半的天数" }, { value: 3, label: "几乎每天" }
];
const scale = (name) => ({
  scaleType: name,
  purpose: `帮助了解最近两周的${name === "GAD-7" ? "焦虑" : "抑郁"}相关感受和变化趋势。`,
  questions: Array(name === "GAD-7" ? 7 : 9).fill("虚构标准题目占位"),
  answerOptions: options,
  scoringRules: `各题所选分数相加得到总分；总分分档：${name === "GAD-7" ? "0-4=极少, 15-21=重度" : "0-4=极少, 20-27=重度"}`,
  disclaimer: "本量表完全自愿；结果仅供筛查与趋势，不作诊断。"
});
const app = new SyntheticModule(["api", "openModal", "displayTime"], function () {
  this.setExport("api", async (path, options) => {
    requests.push({ path, options });
    await delayed;
    return { json: async () => scale(decodeURIComponent(path.split("/").at(-1))) };
  });
  this.setExport("openModal", () => {});
  this.setExport("displayTime", (value) => String(value));
});
const module = new SourceTextModule(await readFile(new URL("../app/static/screening.js", import.meta.url), "utf8"));
await module.link(() => app);
await module.evaluate();
module.namespace.initScreening();
elements.get("#openScreening").listeners.click();
const loading = choices[0].listeners.click();
assert.equal(elements.get("#scaleIntro").hidden, true, "Selection is asynchronous: intro is not yet rendered");
assert.equal(elements.get("#scaleIntroBody").textContent, "");
resolve();
await loading;
assert.equal(elements.get("#scaleIntro").hidden, false);
let intro = elements.get("#scaleIntroBody").textContent;
assert.match(intro, /GAD-7 · 共 7 题/);
assert.match(intro, /用途：.*焦虑/);
assert.match(intro, /相加得到总分/);
assert.match(intro, /15-21=重度/);
assert.match(intro, /完全自愿.*不作诊断/s);
for (const option of options) assert.ok(intro.includes(`${option.value} 分：${option.label}`));
assert.equal(requests.length, 1);
assert.equal(requests[0].options, undefined);

elements.get("#scaleCancel").listeners.click();
assert.equal(elements.get("#scaleIntro").hidden, true);
assert.equal(elements.get("#scaleSelect").hidden, false);
assert.equal(elements.get("#scaleQuestions").hidden, true);
assert.equal(requests.length, 1, "Cancelling the intro must not submit a screening result");
delayed = Promise.resolve();
await choices[1].listeners.click();
intro = elements.get("#scaleIntroBody").textContent;
assert.match(intro, /PHQ-9 · 共 9 题/);
assert.match(intro, /用途：.*抑郁/);
assert.match(intro, /20-27=重度/);
assert.doesNotMatch(intro, /GAD-7/);
for (const option of options) assert.ok(intro.includes(`${option.value} 分：${option.label}`));
console.log("screening asynchronous intro, score explanation and cancel regressions passed");
