// 备考画像面板（阶段 / 目标考试 / 考试日期）+ 数据删除入口。

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
  els.stage = document.querySelector("#profileStage");
  els.targetExam = document.querySelector("#profileTargetExam");
  els.examDate = document.querySelector("#profileExamDate");
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
    els.summaryText.textContent = "未设置画像";
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

export async function loadExamProfile() {
  try {
    const response = await api("/api/profile/exam");
    state.examProfile = await response.json();
  } catch {
    state.examProfile = null;
  }
  render();
}

function hasProfile() {
  const p = state.examProfile;
  return Boolean(p && (p.examStage || p.targetExam || p.examDate));
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
  if (state.examProfile.examStage) parts.push(`${state.examProfile.examStage}阶段`);
  if (state.examProfile.targetExam) parts.push(state.examProfile.targetExam);
  if (state.examProfile.examDate) parts.push(state.examProfile.examDate);
  els.summaryText.textContent = parts.join(" · ") || "未设置画像";
}

function showForm() {
  els.onboarding.hidden = true;
  els.summary.hidden = true;
  els.form.hidden = false;
  els.stage.value = state.examProfile?.examStage || "";
  els.targetExam.value = state.examProfile?.targetExam || "";
  els.examDate.value = state.examProfile?.examDate || "";
  els.state.textContent = "";
}

async function saveProfile(event) {
  event.preventDefault();
  els.state.textContent = "保存中...";
  try {
    const response = await api("/api/profile/exam", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        examStage: els.stage.value,
        targetExam: els.targetExam.value.trim(),
        examDate: els.examDate.value || ""
      })
    });
    state.examProfile = await response.json();
    render();
  } catch (error) {
    els.state.textContent = `保存失败：${error.message}`;
  }
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
