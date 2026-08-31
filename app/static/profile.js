// 支持背景面板（当前关注问题 / 支持目标 / 偏好方式）+ 数据删除入口。

import { state, api, openModal, closeModal } from "/app.js";

const els = {};

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

  els.edit.addEventListener("click", () => showForm());
  els.fill.addEventListener("click", () => showForm());
  els.skip.addEventListener("click", () => {
    els.onboarding.hidden = true;
    els.state.textContent = "已跳过，可随时点击“编辑”补充。";
    els.summary.hidden = false;
    els.summaryText.textContent = "未设置支持背景";
  });
  els.cancel.addEventListener("click", () => render());
  els.form.addEventListener("submit", saveProfile);

  els.openDataDelete.addEventListener("click", () => {
    els.dataDeleteState.textContent = "";
    els.confirmDataDelete.disabled = false;
    openModal("dataDeleteModal");
  });
  els.confirmDataDelete.addEventListener("click", deleteAccount);
}

export async function loadSupportProfile() {
  try {
    const response = await api("/api/profile/support");
    state.supportProfile = await response.json();
  } catch {
    state.supportProfile = null;
  }
  render();
}

function hasProfile() {
  const p = state.supportProfile;
  return Boolean(p && (p.currentConcern || p.supportGoal || p.preferredSupportStyle));
}

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

function showForm() {
  els.onboarding.hidden = true;
  els.summary.hidden = true;
  els.form.hidden = false;
  els.currentConcern.value = state.supportProfile?.currentConcern || "";
  els.supportGoal.value = state.supportProfile?.supportGoal || "";
  els.supportStyle.value = state.supportProfile?.preferredSupportStyle || "";
  els.state.textContent = "";
}

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

function styleLabel(value) {
  return {
    listening: "先倾听和梳理",
    small_steps: "小步行动建议",
    structured: "结构化整理"
  }[value] || value;
}

async function deleteAccount() {
  els.confirmDataDelete.disabled = true;
  els.dataDeleteState.textContent = "正在删除...";
  try {
    await api("/api/account", { method: "DELETE" });
    els.dataDeleteState.textContent = "已删除，正在退出...";
    state.auth.token = null;
    state.profile = null;
    setTimeout(() => location.reload(), 800);
  } catch (error) {
    els.confirmDataDelete.disabled = false;
    els.dataDeleteState.textContent = `删除失败：${error.message}`;
  }
}
