// 登录 / 退出登录，以及登录后各功能区的加载编排。

import { state, api, isAdmin, loadAgentStatus } from "/app.js";
import { loadSupportProfile } from "/profile.js";
import { loadActionPlans, resetPlanPanel } from "/action-plan.js";
import { loadSessionList } from "/chat.js";
import { loadAdminDashboard, loadReviews } from "/admin.js";

const els = {};

export function initAuth() {
  els.loginForm = document.querySelector("#loginForm");
  els.username = document.querySelector("#username");
  els.password = document.querySelector("#password");
  els.loginState = document.querySelector("#loginState");
  els.accountBadge = document.querySelector("#accountBadge");
  els.activeAccount = document.querySelector("#activeAccount");
  els.activeRole = document.querySelector("#activeRole");
  els.switchAccount = document.querySelector("#switchAccount");
  els.studentView = document.querySelector("#studentView");
  els.adminView = document.querySelector("#adminView");
  els.sessionsPanel = document.querySelector("#sessionsPanel");

  els.loginForm.addEventListener("submit", login);
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
    state.auth.token = data.accessToken;
    await afterLogin();
  } catch (error) {
    els.loginState.textContent = `登录失败：${error.message}`;
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
    await Promise.all([loadSupportProfile(), loadActionPlans(), loadSessionList()]);
  }
}

function showApp() {
  const admin = isAdmin(state.profile);
  els.accountBadge.hidden = false;
  els.switchAccount.hidden = false;
  els.activeAccount.textContent = state.profile.displayName || state.profile.username;
  els.activeRole.textContent = admin ? "授权审核账号" : "用户账号";
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
