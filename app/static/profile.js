// 支持背景面板（当前关注问题 / 支持目标 / 偏好方式）+ 数据删除入口。

import { state, api, openModal, closeModal } from "/app.js";

const els = {};
let deletionNeedsAcknowledgement = false;

/**
 * 绑定支持背景表单、跳过和编辑按钮及数据删除弹窗。
 * 所有操作只在事件触发后执行，初始化不提交背景或删除数据。
 */
export function initProfile() {
  els.summary = document.querySelector("#profileSummary");
  els.summaryText = document.querySelector("#profileSummaryText");
  els.edit = document.querySelector("#profileEdit");
  els.onboarding = document.querySelector("#profileOnboarding");
  els.fill = document.querySelector("#profileFill");
  els.skip = document.querySelector("#profileSkip");
  els.form = document.querySelector("#profileForm");
  els.currentConcern = document.querySelector("#profileCurrentConcern");
  els.supportGoal = document.querySelector("#profileSupportGoal");
  els.supportStyle = document.querySelector("#profileSupportStyle");
  els.cancel = document.querySelector("#profileCancel");
  els.state = document.querySelector("#profileState");
  els.openDataDelete = document.querySelector("#openDataDelete");
  els.confirmDataDelete = document.querySelector("#confirmDataDelete");
  els.dataDeleteState = document.querySelector("#dataDeleteState");

  // 点击编辑时展开同一个背景表单。
  els.edit.addEventListener("click", () => showForm());
  // 首次选择填写时复用编辑表单。
  els.fill.addEventListener("click", () => showForm());
  // 跳过只切换本地面板，不向服务端保存任何背景。
  els.skip.addEventListener("click", () => {
    els.onboarding.hidden = true;
    els.state.textContent = "已跳过，可随时点击“编辑”补充。";
    els.summary.hidden = false;
    els.summaryText.textContent = "未设置支持背景";
  });
  // 取消编辑时按已保存的本地资料重绘摘要。
  els.cancel.addEventListener("click", () => render());
  els.form.addEventListener("submit", saveProfile);

  // 每次打开删除弹窗先清除旧错误并恢复确认按钮。
  els.openDataDelete.addEventListener("click", () => {
    if (deletionNeedsAcknowledgement) {
      openModal("dataDeleteModal");
      return;
    }
    els.dataDeleteState.textContent = "";
    els.confirmDataDelete.textContent = "确认删除";
    els.confirmDataDelete.disabled = false;
    openModal("dataDeleteModal");
  });
  els.confirmDataDelete.addEventListener("click", deleteAccount);
}

/**
 * 读取当前账户的支持背景并刷新面板。
 * 请求失败时把本地背景置为空，再显示填写入口；不自动创建服务端记录。
 */
export async function loadSupportProfile() {
  try {
    const response = await api("/api/profile/support");
    state.supportProfile = await response.json();
  } catch {
    state.supportProfile = null;
  }
  render();
}

/**
 * 判断本地背景是否至少有一个非空字段。
 * 只检查关注问题、支持目标和偏好方式，用于选择显示摘要还是填写引导。
 */
function hasProfile() {
  const p = state.supportProfile;
  return Boolean(p && (p.currentConcern || p.supportGoal || p.preferredSupportStyle));
}

/**
 * 依据已有背景重新选择面板状态并生成简短摘要。
 * 先隐藏各子区域，避免编辑、引导和摘要同时显示；正文按文本写入。
 */
function render() {
  els.form.hidden = true;
  els.onboarding.hidden = true;
  els.summary.hidden = true;
  if (!hasProfile()) {
    els.onboarding.hidden = false;
    return;
  }
  els.summary.hidden = false;
  const parts = [];
  if (state.supportProfile.currentConcern) parts.push(state.supportProfile.currentConcern);
  if (state.supportProfile.supportGoal) parts.push(`目标：${state.supportProfile.supportGoal}`);
  if (state.supportProfile.preferredSupportStyle) parts.push(`方式：${styleLabel(state.supportProfile.preferredSupportStyle)}`);
  els.summaryText.textContent = parts.join(" · ") || "未设置支持背景";
}

/**
 * 展开编辑表单并填入当前已保存的背景值。
 * 缺失字段用空文本显示，清除上一次操作提示；此时尚未向服务端保存。
 */
function showForm() {
  els.onboarding.hidden = true;
  els.summary.hidden = true;
  els.form.hidden = false;
  els.currentConcern.value = state.supportProfile?.currentConcern || "";
  els.supportGoal.value = state.supportProfile?.supportGoal || "";
  els.supportStyle.value = state.supportProfile?.preferredSupportStyle || "";
  els.state.textContent = "";
}

/**
 * 阻止页面跳转并提交用户填写的支持背景。
 * 以服务端返回值更新本地状态后渲染，失败则在表单中保留错误提示。
 */
async function saveProfile(event) {
  event.preventDefault();
  els.state.textContent = "保存中...";
  try {
    const response = await api("/api/profile/support", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        currentConcern: els.currentConcern.value.trim(),
        supportGoal: els.supportGoal.value.trim(),
        preferredSupportStyle: els.supportStyle.value
      })
    });
    state.supportProfile = await response.json();
    render();
  } catch (error) {
    els.state.textContent = `保存失败：${error.message}`;
  }
}

/**
 * 把支持方式代码转换为对应中文名称。
 * 未识别代码原样返回，避免静默替换成其他偏好。
 */
function styleLabel(value) {
  return {
    listening: "先倾听和梳理",
    small_steps: "小步行动建议",
    structured: "结构化整理"
  }[value] || value;
}

/**
 * 删除业务数据后立即失效本页凭证；完整清理成功才自动退出。
 * 会话执行状态尚待补偿清理时保留说明，由用户明确返回登录页。
 */
async function deleteAccount() {
  if (deletionNeedsAcknowledgement) {
    location.reload();
    return;
  }
  els.confirmDataDelete.disabled = true;
  els.dataDeleteState.textContent = "正在删除...";
  try {
    const response = await api("/api/account", { method: "DELETE" });
    const result = await response.json();
    state.auth.token = null;
    state.profile = null;
    if (result.checkpointCleanupPending) {
      deletionNeedsAcknowledgement = true;
      els.dataDeleteState.textContent = "账号及业务数据已删除，账号已失效。部分会话执行记录尚待清理，系统将在服务下次启动时自动重试；无需重新登录或重复删除。";
      els.confirmDataDelete.textContent = "返回登录";
      els.confirmDataDelete.disabled = false;
      return;
    }
    els.dataDeleteState.textContent = "已删除，正在退出...";
    // 留出短暂提示时间后刷新页面，使内存中的账户状态清空。
    setTimeout(() => location.reload(), 800);
  } catch (error) {
    els.confirmDataDelete.disabled = false;
    els.dataDeleteState.textContent = `删除失败：${error.message}`;
  }
}
