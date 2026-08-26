// 登录 / 密码重置 / 退出登录，以及登录后各功能区的加载编排。

import { state, api, isAdmin, loadAgentStatus } from "/app.js";
import { loadExamProfile } from "/profile.js";
import { loadActionPlans, resetPlanPanel } from "/action-plan.js";
import { loadSessionList } from "/chat.js";
import { loadAdminDashboard, loadReviews } from "/admin.js";

const els = {};

export function initAuth() {
  els.loginForm = document.querySelector("#loginForm");
  els.username = document.querySelector("#username");
  els.password = document.querySelector("#password");
  els.loginState = document.querySelector("#loginState");
  els.resetForm = document.querySelector("#resetForm");
  els.resetUsername = document.querySelector("#resetUsername");
  els.resetOldPassword = document.querySelector("#resetOldPassword");
  els.resetNewPassword = document.querySelector("#resetNewPassword");
  els.resetState = document.querySelector("#resetState");
  els.accountBadge = document.querySelector("#accountBadge");
  els.activeAccount = document.querySelector("#activeAccount");
  els.activeRole = document.querySelector("#activeRole");
  els.switchAccount = document.querySelector("#switchAccount");
  els.studentView = document.querySelector("#studentView");
  els.adminView = document.querySelector("#adminView");
  els.sessionsPanel = document.querySelector("#sessionsPanel");

  els.loginForm.addEventListener("submit", login);
  els.resetForm.addEventListener("submit", resetPassword);
  els.switchAccount.addEventListener("click", logout);
}

async function login(event) {
  event?.preventDefault();
  const username = els.username.value.trim();
  const password = els.password.value;
  if (!username || !password) {
    els.loginState.textContent = "请输入用户名和密码";
    return;
  }
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    if (!response.ok) {
      const text = await response.text();
      els.loginState.textContent = `登录失败：${text || response.statusText}`;
      return;
    }
    const data = await response.json();
    if (data.resetRequired) {
      els.loginForm.hidden = true;
      els.resetForm.hidden = false;
      els.resetUsername.value = username;
      els.resetOldPassword.value = password;
      els.resetState.textContent = "请设置新密码以完成迁移";
      return;
    }
    state.auth.token = data.accessToken;
    await afterLogin();
  } catch (error) {
    els.loginState.textContent = `登录失败：${error.message}`;
  }
}

async function resetPassword(event) {
  event?.preventDefault();
  const username = els.resetUsername.value.trim();
  const oldPassword = els.resetOldPassword.value;
  const newPassword = els.resetNewPassword.value;
  if (!username || !oldPassword || !newPassword) {
    els.resetState.textContent = "请填写所有字段";
    return;
  }
  if (newPassword.length < 6) {
    els.resetState.textContent = "新密码至少 6 位";
    return;
  }
  try {
    const response = await fetch("/api/auth/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, oldPassword, newPassword })
    });
    if (!response.ok) {
      const text = await response.text();
      els.resetState.textContent = `重置失败：${text || response.statusText}`;
      return;
    }
    const data = await response.json();
    state.auth.token = data.accessToken;
    els.resetForm.hidden = true;
    await afterLogin();
  } catch (error) {
    els.resetState.textContent = `重置失败：${error.message}`;
  }
}

async function afterLogin() {
  const profileResponse = await api("/api/profile");
  state.profile = await profileResponse.json();
  await loadAgentStatus();
  showApp();
  if (isAdmin(state.profile)) {
    await Promise.all([loadAdminDashboard(), loadReviews()]);
  } else {
    await Promise.all([loadExamProfile(), loadActionPlans(), loadSessionList()]);
  }
}

function showApp() {
  const admin = isAdmin(state.profile);
  els.accountBadge.hidden = false;
  els.switchAccount.hidden = false;
  els.activeAccount.textContent = state.profile.displayName || state.profile.username;
  els.activeRole.textContent = admin ? "管理员账号" : "学生账号";
  els.loginForm.hidden = true;
  els.studentView.hidden = admin;
  els.adminView.hidden = !admin;
  document.querySelector("#profilePanel").hidden = admin;
  document.querySelector("#toolsPanel").hidden = admin;
  document.querySelector("#settingsPanel").hidden = admin;
  els.sessionsPanel.hidden = admin;
}

function logout() {
  // token 只保存在内存中，刷新页面即回到未登录状态，各面板也随之复位。
  state.auth.token = null;
  state.profile = null;
  state.sessionId = null;
  resetPlanPanel();
  location.reload();
}
