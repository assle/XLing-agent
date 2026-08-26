// 记忆卡片弹窗：查看 / 新建 / 编辑 / 删除 / 确认系统建议卡片。

import { api, openModal } from "/app.js";

const els = {};

export function initMemoryCards() {
  els.open = document.querySelector("#openMemoryCards");
  els.list = document.querySelector("#memoryCardList");
  els.create = document.querySelector("#memoryCardCreate");
  els.input = document.querySelector("#memoryCardNew");
  els.state = document.querySelector("#memoryCardState");

  els.open.addEventListener("click", () => {
    openModal("memoryCardsModal");
    loadCards();
  });
  els.create.addEventListener("submit", createCard);
}

async function loadCards() {
  els.state.textContent = "";
  els.list.innerHTML = `<p class="hint">读取中...</p>`;
  try {
    const response = await api("/api/memory-cards");
    renderCards(await response.json());
  } catch (error) {
    els.list.innerHTML = "";
    els.state.textContent = `读取失败：${error.message}`;
  }
}

function renderCards(cards) {
  els.list.innerHTML = "";
  if (!cards.length) {
    els.list.innerHTML = `<p class="hint">还没有记忆卡片。你可以在下方新建，或在聊天后确认系统建议的卡片。</p>`;
    return;
  }
  const pending = cards.filter((card) => !card.confirmed);
  const confirmed = cards.filter((card) => card.confirmed);
  for (const card of [...pending, ...confirmed]) {
    els.list.append(renderCard(card));
  }
}

function renderCard(card) {
  const box = document.createElement("div");
  box.className = `memory-card${card.confirmed ? "" : " pending"}`;

  const content = document.createElement("p");
  content.textContent = card.content;
  const meta = document.createElement("span");
  meta.className = "card-meta";
  meta.textContent = card.confirmed ? "已确认" : "待确认 · 确认后才会写入长期记忆";
  box.append(content, meta);

  const actions = document.createElement("div");
  actions.className = "card-actions";
  if (!card.confirmed) {
    actions.append(actionButton("确认", () => confirmCard(card.id)));
  }
  actions.append(
    actionButton("编辑", () => showEditRow(box, card)),
    actionButton("删除", () => deleteCard(card.id), "danger-link")
  );
  box.append(actions);
  return box;
}

function actionButton(label, onClick, extraClass = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (extraClass) button.className = extraClass;
  button.addEventListener("click", onClick);
  return button;
}

function showEditRow(box, card) {
  if (box.querySelector(".card-edit-row")) return;
  const row = document.createElement("div");
  row.className = "card-edit-row";
  const input = document.createElement("input");
  input.value = card.content;
  input.maxLength = 2000;
  const ok = actionButton("保存", async () => {
    const content = input.value.trim();
    if (!content) return;
    try {
      await api(`/api/memory-cards/${card.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content })
      });
      await loadCards();
    } catch (error) {
      els.state.textContent = `保存失败：${error.message}`;
    }
  });
  row.append(input, ok);
  box.append(row);
  input.focus();
}

async function createCard(event) {
  event.preventDefault();
  const content = els.input.value.trim();
  if (!content) return;
  try {
    await api("/api/memory-cards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content })
    });
    els.input.value = "";
    await loadCards();
  } catch (error) {
    els.state.textContent = `创建失败：${error.message}`;
  }
}

async function confirmCard(id) {
  try {
    await api(`/api/memory-cards/${id}/confirm`, { method: "POST" });
    await loadCards();
  } catch (error) {
    els.state.textContent = `确认失败：${error.message}`;
  }
}

async function deleteCard(id) {
  try {
    await api(`/api/memory-cards/${id}`, { method: "DELETE" });
    await loadCards();
  } catch (error) {
    els.state.textContent = `删除失败：${error.message}`;
  }
}
