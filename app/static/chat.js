// 学生聊天：消息收发、流式事件解析、认知行为四维追问进度、无记忆会话。

import { state, api, setPill, isAdmin } from "/app.js";
import { handlePlanEvent, resetPlanPanel, loadActionPlans } from "/action-plan.js";

const els = {};

export function initChat() {
  els.messages = document.querySelector("#messages");
  els.chatForm = document.querySelector("#chatForm");
  els.messageInput = document.querySelector("#messageInput");
  els.sendButton = document.querySelector("#sendButton");
  els.newSession = document.querySelector("#newSession");
  els.sessionBadge = document.querySelector("#sessionBadge");
  els.noMemoryCheck = document.querySelector("#noMemoryCheck");
  els.noMemoryBadge = document.querySelector("#noMemoryBadge");
  els.cbtTag = document.querySelector("#cbtTag");
  els.sessionList = document.querySelector("#sessionList");

  els.chatForm.addEventListener("submit", sendMessage);
  els.newSession.addEventListener("click", startNewSession);
  // 示例语填入输入框：用事件委托，动态渲染的开场白卡片也能响应
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-quick]");
    if (!trigger) return;
    els.messageInput.value = trigger.dataset.quick;
    els.messageInput.focus();
  });
  // 会话历史列表：点击切换回老对话
  els.sessionList?.addEventListener("click", (event) => {
    const item = event.target.closest(".session-item");
    if (!item) return;
    switchSession(item.dataset.sessionId);
  });
}

function welcomeHtml() {
  return `
    <div class="empty">
      <strong>你好，这里是 Xling</strong>
      <p>一个可以安心说话的地方。备考的压力、睡不着的夜晚、说不清的烦躁，都可以慢慢写下来。不知道从何说起？点下面任意一句：</p>
      <div class="starter-row">
        <button class="starter" type="button" data-quick="我最近压力很大，晚上总是睡不着。">压力大，睡不着</button>
        <button class="starter" type="button" data-quick="快考试了，我总觉得复习不完，心里很慌。">担心复习不完</button>
        <button class="starter" type="button" data-quick="我最近学习效率很低，越学越焦虑，还容易拖延。">越学越焦虑</button>
      </div>
    </div>`;
}

function startNewSession() {
  state.sessionId = null;
  state.noMemory = els.noMemoryCheck.checked;
  hideCbtTag();
  els.messages.innerHTML = welcomeHtml();
  els.noMemoryBadge.hidden = !state.noMemory;
  setPill(els.sessionBadge, "READY");
  // 切换新会话时清空上一轮的行动计划面板，避免残留
  resetPlanPanel();
  // 刷新历史列表，让刚结束的会话出现在列表中
  loadSessionList();
}

// 切换到历史会话：加载消息、行动计划，并高亮当前会话
async function switchSession(publicId) {
  state.sessionId = publicId;
  hideCbtTag();
  setPill(els.sessionBadge, "READY");
  els.messages.innerHTML = `<div class="empty"><p>加载中...</p></div>`;
  try {
    const response = await api(`/api/sessions/${publicId}`);
    const data = await response.json();
    els.messages.innerHTML = "";
    if (!data.messages || data.messages.length === 0) {
      els.messages.innerHTML = welcomeHtml();
    } else {
      for (const msg of data.messages) {
        const role = msg.role === "user" ? "user" : "assistant";
        addMessage(role, msg.content);
      }
    }
    // 加载该会话关联的行动计划
    await loadActionPlans();
    // 同步无记忆模式标记
    state.noMemory = false;
    els.noMemoryCheck.checked = false;
    els.noMemoryBadge.hidden = true;
  } catch (error) {
    els.messages.innerHTML = `<div class="empty"><p>加载会话失败：${error.message}</p></div>`;
  }
  highlightActiveSession();
}

// 加载会话历史列表
export async function loadSessionList() {
  if (!state.auth.token || isAdmin(state.profile)) return;
  try {
    const response = await api("/api/sessions");
    const sessions = await response.json();
    if (!sessions.length) {
      els.sessionList.innerHTML = `<p class="hint">还没有历史会话</p>`;
      return;
    }
    els.sessionList.innerHTML = sessions.map((s) => {
      const active = s.sessionId === state.sessionId;
      const time = new Date(s.updatedAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const badge = s.noMemory ? `<span class="session-badge warn">无记忆</span>` : "";
      return `<button class="session-item${active ? " active" : ""}" type="button" data-session-id="${s.sessionId}">
        <span class="session-title">${s.title}</span>
        <span class="session-meta">${time} · ${s.messageCount} 条${badge}</span>
      </button>`;
    }).join("");
  } catch {
    els.sessionList.innerHTML = `<p class="hint">加载失败</p>`;
  }
}

function highlightActiveSession() {
  els.sessionList?.querySelectorAll(".session-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.sessionId === state.sessionId);
  });
}

function renderCbtTag(data) {
  if (data.complete || !data.active) {
    hideCbtTag();
    return;
  }
  els.cbtTag.textContent = `结构化支持 · ${data.completedCount}/4 · 可随时退出`;
  els.cbtTag.hidden = false;
}

function hideCbtTag() {
  els.cbtTag.hidden = true;
}

function clearWelcome() {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, content) {
  clearWelcome();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  row.innerHTML = `
    <div class="message-role">${role === "user" ? "我" : "Xling"}</div>
    <div class="bubble"></div>
  `;
  row.querySelector(".bubble").textContent = content;
  els.messages.append(row);
  els.messages.scrollTop = els.messages.scrollHeight;
  return row.querySelector(".bubble");
}

function parseSse(buffer, onEvent) {
  const parts = buffer.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const dataLine = part.split("\n").find((line) => line.startsWith("data: "));
    if (!dataLine) continue;
    onEvent(JSON.parse(dataLine.slice(6)));
  }
  return rest;
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending || isAdmin(state.profile)) return;
  const message = els.messageInput.value.trim();
  if (!message) return;
  state.sending = true;
  const wasNewSession = !state.sessionId;
  els.sendButton.disabled = true;
  setPill(els.sessionBadge, "THINKING", "warn");
  els.messageInput.value = "";
  addMessage("user", message);
  const assistant = addMessage("assistant", "");
  let raw = "";

  try {
    const response = await api("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.sessionId, message, noMemory: state.noMemory })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamFailed = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, (eventData) => {
        if (eventData.type === "meta") {
          state.sessionId = eventData.sessionId;
          state.noMemory = Boolean(eventData.noMemory);
          els.noMemoryBadge.hidden = !state.noMemory;
        }
        if (eventData.type === "cbt") {
          renderCbtTag(eventData);
        }
        if (eventData.type === "action_plan") {
          handlePlanEvent(eventData);
        }
        if (eventData.type === "pending_review") {
          raw = eventData.content || "";
          assistant.textContent = raw;
          setPill(els.sessionBadge, "REVIEW", "warn");
        }
        if (eventData.type === "token") {
          raw += eventData.content || "";
          assistant.textContent = raw;
          els.messages.scrollTop = els.messages.scrollHeight;
        }
        if (eventData.type === "error") {
          streamFailed = true;
          if (!raw) assistant.textContent = eventData.message || "MCP 工具调用失败";
          setPill(els.sessionBadge, "ERROR", "danger");
        }
      });
    }
    if (!streamFailed) setPill(els.sessionBadge, "DONE", "ok");
  } catch (error) {
    assistant.textContent = `发送失败：${error.message}`;
    setPill(els.sessionBadge, "ERROR", "danger");
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
    // 新会话的第一条消息发送后，刷新历史列表让该会话出现
    if (wasNewSession && state.sessionId) {
      loadSessionList();
    } else {
      highlightActiveSession();
    }
  }
}
