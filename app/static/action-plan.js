// 24 小时行动计划面板：条目勾选完成、替换条目、次日反馈。

import { state, api } from "/app.js";

const els = {};

export function initActionPlan() {
  els.panel = document.querySelector("#planPanel");
  els.progress = document.querySelector("#planProgress");
  els.items = document.querySelector("#planItems");
  els.checkinStart = document.querySelector("#checkinStart");
  els.checkinForm = document.querySelector("#checkinForm");
  els.checkinNotes = document.querySelector("#checkinNotes");
  els.checkinCancel = document.querySelector("#checkinCancel");
  els.checkinState = document.querySelector("#checkinState");
  els.progressBar = document.querySelector("#planProgressBar");

  els.checkinStart.addEventListener("click", () => {
    els.checkinForm.hidden = false;
    els.checkinStart.hidden = true;
  });
  els.checkinCancel.addEventListener("click", () => {
    els.checkinForm.hidden = true;
    els.checkinStart.hidden = false;
  });
  els.checkinForm.addEventListener("submit", submitCheckin);
}

export function resetPlanPanel() {
  state.activePlan = null;
  state.pendingCheckInPlanId = null;
  render();
}

// action_plan 流式事件：认知行为四维追问完成后实时展示新计划。
export function handlePlanEvent(data) {
  if (!data.planId) return;
  state.activePlan = {
    id: data.planId,
    status: "active",
    items: (data.items || []).map((item) => ({
      id: item.id,
      content: item.content,
      order: item.order,
      completed: Boolean(item.completed)
    }))
  };
  // 计划的 24 小时目标窗口结束后，GET /api/check-ins/pending 才会开放反馈入口。
  state.pendingCheckInPlanId = null;
  render();
}

export async function loadActionPlans() {
  try {
    const [plansRes, pendingRes] = await Promise.all([
      api("/api/action-plans"),
      api("/api/check-ins/pending")
    ]);
    const plans = await plansRes.json();
    const pending = await pendingRes.json();
    state.activePlan = plans.find((plan) => plan.status === "active") || null;
    state.pendingCheckInPlanId = pending.length ? pending[0].id : null;
  } catch {
    state.activePlan = null;
    state.pendingCheckInPlanId = null;
  }
  render();
}

function render() {
  const plan = state.activePlan;
  els.panel.hidden = !plan;
  els.checkinForm.hidden = true;
  if (!plan) return;

  const items = [...(plan.items || [])].sort((a, b) => a.order - b.order);
  const done = items.filter((item) => item.completed).length;
  els.progress.textContent = `${done}/${items.length} 已完成`;
  // 可视进度条：完成比例直接驱动宽度，宽度变化带过渡动画
  const ratio = items.length ? done / items.length : 0;
  els.progressBar.style.width = `${Math.round(ratio * 100)}%`;

  els.items.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    li.className = `plan-item${item.completed ? " done" : ""}`;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = item.completed;
    checkbox.disabled = item.completed;
    checkbox.addEventListener("change", () => completeItem(item.id));

    const text = document.createElement("span");
    text.className = "plan-item-text";
    text.textContent = item.content;

    li.append(checkbox, text);
    if (!item.completed) {
      const replaceBtn = document.createElement("button");
      replaceBtn.type = "button";
      replaceBtn.className = "replace-btn";
      replaceBtn.textContent = "替换";
      replaceBtn.addEventListener("click", () => showReplaceRow(li, item.id));
      li.append(replaceBtn);
    }
    els.items.append(li);
  }

  // 次日反馈入口：有计划待反馈时出现
  els.checkinStart.hidden = state.pendingCheckInPlanId !== plan.id;
  els.checkinState.textContent = "";
}

function showReplaceRow(li, itemId) {
  if (li.querySelector(".plan-replace-row")) return;
  const row = document.createElement("div");
  row.className = "plan-replace-row";
  const input = document.createElement("input");
  input.placeholder = "输入更适合你的行动";
  input.maxLength = 2000;
  const ok = document.createElement("button");
  ok.type = "button";
  ok.className = "replace-btn";
  ok.textContent = "确定";
  ok.addEventListener("click", async () => {
    const content = input.value.trim();
    if (!content) return;
    await replaceItem(itemId, content);
  });
  row.append(input, ok);
  li.append(row);
  input.focus();
}

async function completeItem(itemId) {
  try {
    await api(`/api/action-plans/items/${itemId}/complete`, { method: "POST" });
    const item = state.activePlan?.items.find((entry) => entry.id === itemId);
    if (item) item.completed = true;
    render();
  } catch {
    await loadActionPlans();
  }
}

async function replaceItem(itemId, content) {
  try {
    const response = await api(`/api/action-plans/items/${itemId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content })
    });
    const updated = await response.json();
    const item = state.activePlan?.items.find((entry) => entry.id === itemId);
    if (item) item.content = updated.content;
    render();
  } catch {
    await loadActionPlans();
  }
}

async function submitCheckin(event) {
  event.preventDefault();
  const status = els.checkinForm.querySelector('input[name="improvement"]:checked')?.value;
  if (!status) {
    els.checkinState.textContent = "请选择一个状态";
    return;
  }
  els.checkinState.textContent = "提交中...";
  try {
    const res = await api("/api/check-ins", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        planId: state.pendingCheckInPlanId,
        improvementStatus: status,
        notes: els.checkinNotes.value.trim()
      })
    });
    const data = await res.json().catch(() => ({}));
    els.checkinNotes.value = "";
    els.checkinForm.reset();
    els.checkinState.textContent =
      data.escalated && data.safetyMessage ? data.safetyMessage : "已提交，谢谢反馈。";
    state.pendingCheckInPlanId = null;
    await loadActionPlans();
  } catch (error) {
    els.checkinState.textContent = `提交失败：${error.message}`;
  }
}
