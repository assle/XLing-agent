// 24 小时行动计划面板：条目勾选完成、替换条目、次日反馈。

import { state, api } from "/app.js";

const els = {};

/**
 * 取得计划与反馈表单元素并绑定展开、取消及提交事件。
 * 初始化不读取计划，读取由登录后的加载流程触发。
 */
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

  // 点击反馈入口时展开表单并隐藏重复入口。
  els.checkinStart.addEventListener("click", () => {
    els.checkinForm.hidden = false;
    els.checkinStart.hidden = true;
  });
  // 取消时收起表单并恢复反馈入口，不发送请求。
  els.checkinCancel.addEventListener("click", () => {
    els.checkinForm.hidden = true;
    els.checkinStart.hidden = false;
  });
  els.checkinForm.addEventListener("submit", submitCheckin);
}

/**
 * 清除当前计划和待反馈编号，再隐藏相应展示。
 * 仅重置本地状态，不删除服务器中的计划。
 */
export function resetPlanPanel() {
  state.activePlan = null;
  state.pendingCheckInPlanId = null;
  render();
}

// action_plan 流式事件：认知行为四维追问完成后实时展示新计划。
/**
 * 把对话流中的新计划事件转换成本地面板状态。
 * 逐项保留编号、内容和顺序并规范完成标志，随后立即提供反馈入口。
 */
export function handlePlanEvent(data) {
  if (!data.planId) return;
  state.activePlan = {
    id: data.planId,
    status: "active",
    feedbackDueAt: data.feedbackDueAt || null,
    feedbackAvailable: data.feedbackAvailable !== false,
    // 将每个事件条目转换为面板所需字段，完成状态统一为布尔值。
    items: (data.items || []).map((item) => ({
      id: item.id,
      content: item.content,
      order: item.order,
      completed: Boolean(item.completed)
    }))
  };
  // 反馈入口立即可用；“次日”只是建议时间。
  state.pendingCheckInPlanId = data.planId;
  render();
}

/**
 * 同时读取当前会话计划与待反馈计划，选择当前可用项展示。
 * 仅展示该会话最新的进行中计划；最新计划完成后不退回较旧的 active 计划。
 */
export async function loadActionPlans() {
  const sessionId = state.sessionId;
  resetPlanPanel();
  if (!sessionId) return;
  try {
    const [plansRes, pendingRes] = await Promise.all([
      api(`/api/action-plans?sessionId=${encodeURIComponent(sessionId)}`),
      api("/api/check-ins/pending")
    ]);
    const plans = await plansRes.json();
    const pending = await pendingRes.json();
    if (state.sessionId !== sessionId) return;
    const latestPlan = plans[0];
    state.activePlan = latestPlan?.status === "active" ? latestPlan : null;
    state.pendingCheckInPlanId = pending.some((plan) => plan.id === state.activePlan?.id)
      ? state.activePlan.id
      : null;
  } catch {
    if (state.sessionId !== sessionId) return;
    state.activePlan = null;
    state.pendingCheckInPlanId = null;
  }
  render();
}

/**
 * 按行动顺序绘制条目、完成进度和反馈按钮。
 * 先复制再排序，避免改变共享数组；已完成项禁用勾选和替换，反馈时间只影响提示文字。
 */
function render() {
  const plan = state.activePlan;
  els.panel.hidden = !plan;
  els.checkinForm.hidden = true;
  if (!plan) return;

  // 比较条目的 order 值，让展示顺序与生成计划一致。
  const items = [...(plan.items || [])].sort((a, b) => a.order - b.order);
  // 筛选已经完成的条目，用于计算数量和进度条。
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
    // 勾选时提交当前条目编号，不在绑定事件时立即执行。
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
      // 点击替换时在当前列表项中展开输入框。
      replaceBtn.addEventListener("click", () => showReplaceRow(li, item.id));
      li.append(replaceBtn);
    }
    els.items.append(li);
  }

  // 次日反馈入口：当前行动计划可立即反馈，到期前后只改变提示文案
  const due = !plan.feedbackDueAt || new Date(plan.feedbackDueAt).getTime() <= Date.now();
  els.checkinStart.hidden = state.pendingCheckInPlanId !== plan.id || plan.feedbackAvailable === false;
  els.checkinStart.textContent = due
    ? "次日反馈：计划执行得怎么样？"
    : "提前反馈：计划执行得怎么样？（次日再反馈也可以）";
  els.checkinState.textContent = "";
}

/**
 * 为指定行动项创建替换输入框，已有输入框时跳过。
 * 确认回调读取非空文本再调用保存入口，输入框创建后立即获得焦点。
 */
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
  // 读取并去掉首尾空白，内容非空才提交替换请求。
  ok.addEventListener("click", async () => {
    const content = input.value.trim();
    if (!content) return;
    await replaceItem(itemId, content);
  });
  row.append(input, ok);
  li.append(row);
  input.focus();
}

/**
 * 请求标记行动项完成，成功后同步本地标志并重绘。
 * 请求失败则重新加载服务端计划，避免停留在未经确认的页面状态。
 */
async function completeItem(itemId) {
  try {
    await api(`/api/action-plans/items/${itemId}/complete`, { method: "POST" });
    // 按编号找到本地同一条目，只更新它的完成状态。
    const item = state.activePlan?.items.find((entry) => entry.id === itemId);
    if (item) item.completed = true;
    render();
  } catch {
    await loadActionPlans();
  }
}

/**
 * 提交新的行动内容，并使用服务端返回的文本更新本地条目。
 * 失败时重新读取计划，使显示内容回到当前服务器状态。
 */
async function replaceItem(itemId, content) {
  try {
    const response = await api(`/api/action-plans/items/${itemId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content })
    });
    const updated = await response.json();
    // 按编号匹配服务端刚更新的条目，使用返回内容重绘。
    const item = state.activePlan?.items.find((entry) => entry.id === itemId);
    if (item) item.content = updated.content;
    render();
  } catch {
    await loadActionPlans();
  }
}

/**
 * 校验改善选项并提交当前待反馈计划的备注和状态。
 * 成功后清空表单并重新加载计划，失败在反馈区域显示原因。
 */
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
    // 响应正文无法解析时使用空对象继续后续表单复位。
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
