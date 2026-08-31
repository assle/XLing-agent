// Xling frontend — shared state + API helper + app bootstrap.
// ES Modules: feature modules import `state` / `api()` from here;
// this file imports each feature's init() and wires everything up.

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
  modelName: "mock",
  noMemory: false,
  activePlan: null,
  pendingCheckInPlanId: null
};

export function authHeader() {
  return `Bearer ${state.auth.token}`;
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

export function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

export function isAdmin(profile) {
  return profile?.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

export function displayTime(value) {
  return value ? new Date(value).toLocaleString() : "";
}

export function displayModel(model) {
  return (model || "").includes("xling-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

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
export function openModal(id) {
  document.querySelector(`#${id}`).hidden = false;
}

export function closeModal(id) {
  document.querySelector(`#${id}`).hidden = true;
}

function initModalCloseButtons() {
  document.querySelectorAll("[data-close]").forEach((button) => {
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
