// 量表筛查弹窗：选量表 -> 说明页（知情同意）-> 单页作答 -> 分数结果；历史记录 tab。

import { api, openModal, displayTime } from "/app.js";

const els = {};
const view = { scaleType: null, scaleInfo: null };

// 后端以英文代码返回严重程度，界面统一显示中文（与计分规则文案一致）。
const SEVERITY_LABELS = {
  minimal: "极少",
  mild: "轻度",
  moderate: "中度",
  moderately_severe: "中重度",
  severe: "重度"
};

function severityLabel(value) {
  return SEVERITY_LABELS[value] || value || "未知";
}

export function initScreening() {
  els.open = document.querySelector("#openScreening");
  els.screenTabBtn = document.querySelector("#screenTabBtn");
  els.historyTabBtn = document.querySelector("#historyTabBtn");
  els.screenTab = document.querySelector("#screenTab");
  els.historyTab = document.querySelector("#historyTab");
  els.scaleSelect = document.querySelector("#scaleSelect");
  els.scaleIntro = document.querySelector("#scaleIntro");
  els.scaleIntroBody = document.querySelector("#scaleIntroBody");
  els.scaleStart = document.querySelector("#scaleStart");
  els.scaleCancel = document.querySelector("#scaleCancel");
  els.questions = document.querySelector("#scaleQuestions");
  els.result = document.querySelector("#scaleResult");
  els.history = document.querySelector("#screeningHistory");

  els.open.addEventListener("click", () => {
    openModal("screeningModal");
    showTab("screen");
    resetFlow();
  });
  els.screenTabBtn.addEventListener("click", () => showTab("screen"));
  els.historyTabBtn.addEventListener("click", () => {
    showTab("history");
    loadHistory();
  });
  document.querySelectorAll(".scale-choice").forEach((button) => {
    button.addEventListener("click", () => loadScale(button.dataset.scale));
  });
  els.scaleStart.addEventListener("click", showQuestions);
  els.scaleCancel.addEventListener("click", resetFlow);
  els.questions.addEventListener("submit", submitAnswers);
}

function showTab(which) {
  const screen = which === "screen";
  els.screenTab.hidden = !screen;
  els.historyTab.hidden = screen;
  els.screenTabBtn.classList.toggle("active", screen);
  els.historyTabBtn.classList.toggle("active", !screen);
}

function resetFlow() {
  view.scaleType = null;
  view.scaleInfo = null;
  els.scaleSelect.hidden = false;
  els.scaleIntro.hidden = true;
  els.questions.hidden = true;
  els.result.hidden = true;
}

async function loadScale(scaleType) {
  try {
    const response = await api(`/api/screening/${encodeURIComponent(scaleType)}`);
    view.scaleType = scaleType;
    view.scaleInfo = await response.json();
    renderIntro();
  } catch (error) {
    els.scaleSelect.insertAdjacentHTML("beforeend", `<p class="hint">量表读取失败：${error.message}</p>`);
  }
}

function renderIntro() {
  const info = view.scaleInfo;
  els.scaleSelect.hidden = true;
  els.scaleIntro.hidden = false;
  els.scaleIntroBody.innerHTML = "";
  const block = document.createElement("div");
  block.className = "scale-intro-block";

  const purpose = document.createElement("h3");
  purpose.textContent = `${info.scaleType} · 共 ${info.questions.length} 题`;
  const purposeDetail = document.createElement("p");
  purposeDetail.textContent = `用途：${info.purpose}`;
  const scoring = document.createElement("p");
  scoring.textContent = `计分规则：${info.scoringRules}`;
  const options = document.createElement("p");
  options.textContent = `每题四个选项：${info.answerOptions.map((o) => o.label).join(" / ")}`;
  const disclaimer = document.createElement("p");
  disclaimer.textContent = info.disclaimer;
  disclaimer.className = "danger-text";

  block.append(purpose, purposeDetail, scoring, options, disclaimer);
  els.scaleIntroBody.append(block);
}

function showQuestions() {
  els.scaleIntro.hidden = true;
  els.questions.hidden = false;
  els.questions.innerHTML = "";
  view.scaleInfo.questions.forEach((question, index) => {
    const fieldset = document.createElement("div");
    fieldset.className = "scale-question";
    const title = document.createElement("p");
    title.textContent = `${index + 1}. ${question}`;
    const options = document.createElement("div");
    options.className = "scale-options";
    for (const option of view.scaleInfo.answerOptions) {
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `q${index}`;
      radio.value = option.value;
      label.append(radio, document.createTextNode(option.label));
      options.append(label);
    }
    fieldset.append(title, options);
    els.questions.append(fieldset);
  });
  const submit = document.createElement("button");
  submit.className = "primary full";
  submit.type = "submit";
  submit.textContent = "提交";
  els.questions.append(submit);
}

async function submitAnswers(event) {
  event.preventDefault();
  const answers = view.scaleInfo.questions.map((_, index) => {
    const checked = els.questions.querySelector(`input[name="q${index}"]:checked`);
    return checked ? Number(checked.value) : null;
  });
  if (answers.some((answer) => answer === null)) {
    alert("还有题目未作答，请完成所有题目后再提交。");
    return;
  }
  try {
    const response = await api(`/api/screening/${encodeURIComponent(view.scaleType)}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers, triggerSource: "voluntary" })
    });
    renderResult(await response.json());
  } catch (error) {
    alert(`提交失败：${error.message}`);
  }
}

function renderResult(result) {
  els.questions.hidden = true;
  els.result.hidden = false;
  els.result.innerHTML = "";
  const box = document.createElement("div");
  box.className = "scale-result-box";
  const score = document.createElement("strong");
  score.textContent = `${result.scaleType} · ${result.totalScore} 分 · ${severityLabel(result.severity)}`;
  const disclaimer = document.createElement("p");
  disclaimer.textContent = result.disclaimer;
  if (result.highRiskFlagged) {
    const safety = document.createElement("p");
    safety.className = "danger-text";
    safety.textContent = "你的回答提示你可能需要立即获得更多支持。请现在联系身边可信任的人、学校心理中心，或拨打 24 小时心理援助热线 400-161-9995；如有紧急危险，请立即联系当地急救或报警服务。";
    box.append(score, safety, disclaimer);
  } else {
    box.append(score, disclaimer);
  }
  const time = document.createElement("p");
  time.className = "hint";
  time.textContent = displayTime(result.createdAt);
  const again = document.createElement("button");
  again.className = "ghost";
  again.type = "button";
  again.textContent = "返回量表选择";
  again.addEventListener("click", resetFlow);
  box.append(time, again);
  els.result.append(box);
}

async function loadHistory() {
  els.history.innerHTML = `<p class="hint">读取中...</p>`;
  try {
    const response = await api("/api/screening/results");
    const results = await response.json();
    els.history.innerHTML = "";
    if (!results.length) {
      els.history.innerHTML = `<p class="hint">还没有筛查记录。</p>`;
      return;
    }
    for (const item of results) {
      const row = document.createElement("div");
      row.className = "memory-card history-item";
      const label = document.createElement("span");
      label.textContent = `${item.scaleType} · ${item.totalScore} 分 · ${severityLabel(item.severity)}`;
      const time = document.createElement("span");
      time.className = "card-meta";
      time.textContent = displayTime(item.createdAt);
      row.append(label, time);
      els.history.append(row);
    }
  } catch (error) {
    els.history.innerHTML = `<p class="hint">读取失败：${error.message}</p>`;
  }
}
