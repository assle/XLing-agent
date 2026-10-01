// 登录 / 退出登录，以及登录后各功能区的加载编排。

import { state, api, isAdmin, loadAgentStatus } from "/app.js";
import { loadSupportProfile } from "/profile.js";
import { loadActionPlans, resetPlanPanel } from "/action-plan.js";
import { loadSessionList } from "/chat.js";
import { loadAdminDashboard, loadReviews } from "/admin.js";

const els = {};

/**
 * 取得登录和账户展示元素，绑定登录提交与切换账号事件。
 * 仅建立事件关系，不自动登录或读取账户数据。
 */
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

/**
 * 拦截表单默认提交，校验非空账号密码后请求登录。
 * 成功取得凭证后只保存在页面内存，并继续加载用户资料；失败在表单附近显示原因。
 */
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

/**
 * 取得用户信息和模型状态，再按角色加载相应功能区。
 * 用户功能与管理功能分别并行读取自己的数据，任一请求错误继续交给登录入口显示。
 */
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

/**
 * 根据当前账户角色切换用户区和审核后台的可见状态。
 * 页面隐藏只是展示控制，真正的访问权限仍由服务端验证。
 */
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

/**
 * 清空当前页面的登录状态和计划，再重新加载页面。
 * 凭证没有写入长期浏览器存储，刷新可清理当前页面内存；这里不调用服务端撤销凭证。
 */
function logout() {
  // token 只保存在内存中，刷新页面即回到未登录状态，各面板也随之复位。
  state.auth.token = null;
  state.profile = null;
  state.sessionId = null;
  state.pendingReview = false;
  resetPlanPanel();
  location.reload();
}
