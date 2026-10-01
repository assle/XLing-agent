// 网页共享状态、接口请求辅助方法和功能初始化。
// 各功能模块从这里取得状态和请求入口，再由本文件统一绑定页面事件。

import { initAuth } from "/auth.js";
import { initProfile } from "/profile.js";
import { initChat } from "/chat.js";
import { initActionPlan } from "/action-plan.js";
import { initMemoryCards } from "/memory-cards.js";
import { initScreening } from "/screening.js";
import { initAdmin } from "/admin.js";

export const state = {
  auth: { token: null },
  profile: null,
  supportProfile: null,
  sessionId: null,
  sending: false,
  pendingReview: false,
  modelName: "mock",
  noMemory: false,
  activePlan: null,
  pendingCheckInPlanId: null
};

/**
 * 把内存中的登录凭证包装成请求头文本。
 * 只构造 Bearer 格式（表示携带登录凭证），不验证凭证是否存在或过期。
 */
export function authHeader() {
  return `Bearer ${state.auth.token}`;
}

/**
 * 为网页请求统一加入当前凭证并检查响应状态。
 * path 是接口路径，options 保留方法、正文及自定义请求头；非成功响应读取错误正文并抛错。
 * 成功返回原始响应对象，由调用方决定如何读取内容。
 */
export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

/**
 * 更新状态标签的文字和视觉类别。
 * el 是页面元素，tone 默认为正常色；设置 className 会替换该元素原有的全部类别。
 */
export function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

/**
 * 检查账户信息中是否包含管理员角色。
 * 回调逐个比较 authority 字段；资料缺失时可得到 undefined，调用处按真假使用。
 */
export function isAdmin(profile) {
  // 逐个检查角色对象，命中管理员角色即可停止查找。
  return profile?.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

/**
 * 将时间值转换为浏览器本地格式的文字。
 * 空值返回空字符串，显示时区由用户浏览器环境决定。
 */
export function displayTime(value) {
  return value ? new Date(value).toLocaleString() : "";
}

/**
 * 为项目指定的微调模型显示更易读的名称。
 * 其他模型原样返回，此转换仅影响展示，不改变请求使用的模型。
 */
export function displayModel(model) {
  return (model || "").includes("xling-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

/**
 * 读取公开健康接口并更新页面服务状态。
 * 请求或解析失败时显示不可用；这里不检查模型是否能成功生成回复。
 */
export async function checkHealth() {
  const el = document.querySelector("#serviceState");
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setPill(el, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(el, "服务 DOWN", "danger");
  }
}

/**
 * 读取已登录用户可见的模型状态，保存模型名称并更新标签。
 * 区分真实模型配置和模拟模式，接口失败交由调用方处理。
 */
export async function loadAgentStatus() {
  const el = document.querySelector("#modelState");
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(el, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(el, "mock 演示", "warn");
  }
}

// Shared modal helpers (used by memory-cards / screening / data deletion).
/**
 * 按元素编号显示一个已有弹窗。
 * 只修改 hidden 属性，不创建内容或自动加载数据。
 */
export function openModal(id) {
  document.querySelector(`#${id}`).hidden = false;
}

/**
 * 按元素编号隐藏一个已有弹窗。
 * 不删除弹窗中的表单内容或取消已发出的请求。
 */
export function closeModal(id) {
  document.querySelector(`#${id}`).hidden = true;
}

/**
 * 为所有带关闭目标标记的按钮绑定点击处理。
 * 遍历时读取每个按钮的目标编号，点击后关闭对应弹窗。
 */
function initModalCloseButtons() {
  // 为每个关闭按钮各绑定一个回调，目标由该按钮的 data-close 属性提供。
  document.querySelectorAll("[data-close]").forEach((button) => {
    // 点击发生时才读取当前按钮对应的弹窗编号并关闭。
    button.addEventListener("click", () => closeModal(button.dataset.close));
  });
}

initAuth();
initProfile();
initChat();
initActionPlan();
initMemoryCards();
initScreening();
initAdmin();
initModalCloseButtons();
checkHealth();
