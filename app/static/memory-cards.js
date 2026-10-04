// 记忆卡片弹窗：查看 / 新建 / 编辑 / 删除 / 确认系统建议卡片。

import { api, openModal } from "/app.js";

const els = {};

/**
 * 绑定记忆卡片弹窗入口和新建表单。
 * 打开时读取最新列表，初始化本身不请求卡片。
 */
export function initMemoryCards() {
  els.open = document.querySelector("#openMemoryCards");
  els.list = document.querySelector("#memoryCardList");
  els.create = document.querySelector("#memoryCardCreate");
  els.input = document.querySelector("#memoryCardNew");
  els.state = document.querySelector("#memoryCardState");

  // 打开弹窗后读取卡片列表，使管理页面显示最新保存状态。
  els.open.addEventListener("click", () => {
    openModal("memoryCardsModal");
    loadCards();
  });
  els.create.addEventListener("submit", createCard);
}

/**
 * 显示加载状态，读取当前用户卡片并绘制。
 * 失败清空旧列表并展示错误，避免把旧数据误作本次查询结果。
 */
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

/**
 * 把待确认卡片放在已确认卡片之前展示。
 * 分别筛选后连接列表，不改变每组内部原有顺序；空列表展示引导说明。
 */
function renderCards(cards) {
  els.list.innerHTML = "";
  if (!cards.length) {
    els.list.innerHTML = `<p class="hint">还没有记忆卡片。你可以在下方新建，或在聊天后确认系统建议的卡片。</p>`;
    return;
  }
  // 取出尚未确认的建议，优先展示待用户决定的内容。
  const pending = cards.filter((card) => !card.confirmed);
  // 取出已确认内容，保持服务端给定的组内顺序。
  const confirmed = cards.filter((card) => card.confirmed);
  for (const card of [...pending, ...confirmed]) {
    els.list.append(renderCard(card));
  }
}

/**
 * 为一条卡片创建正文、确认状态和操作按钮。
 * 待确认项额外提供确认按钮；内容按文本写入，按钮回调持有对应卡片编号。
 */
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
    // 用户点击后才提交这张卡片的确认请求。
    actions.append(actionButton("确认", () => confirmCard(card.id)));
  }
  actions.append(
    // 点击编辑时把当前卡片对象传入编辑区域。
    actionButton("编辑", () => showEditRow(box, card)),
    // 删除按钮绑定当前卡片编号，避免误删其他行。
    actionButton("删除", () => deleteCard(card.id), "danger-link")
  );
  box.append(actions);
  return box;
}

/**
 * 创建统一的卡片操作按钮并绑定传入回调。
 * label 是显示文字，extraClass 可指定样式；type 为 button，避免触发表单提交。
 */
function actionButton(label, onClick, extraClass = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (extraClass) button.className = extraClass;
  button.addEventListener("click", onClick);
  return button;
}

/**
 * 在卡片内打开一次编辑区域并预填原内容。
 * 保存时检查非空文本，请求成功后重载列表；失败在弹窗内显示错误。
 */
function showEditRow(box, card) {
  if (box.querySelector(".card-edit-row")) return;
  const row = document.createElement("div");
  row.className = "card-edit-row";
  const input = document.createElement("input");
  input.value = card.content;
  input.maxLength = 2000;
  // 保存回调提交修改后的非空正文，成功后重新获取权威列表。
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

/**
 * 提交用户填写的非空卡片内容。
 * 成功才清空输入并刷新列表，失败保留内容供再次编辑。
 */
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

/**
 * 请求确认指定卡片，再重新读取列表。
 * id 指定卡片，确认失败显示错误，不在本地提前修改确认状态。
 */
async function confirmCard(id) {
  try {
    await api(`/api/memory-cards/${id}/confirm`, { method: "POST" });
    await loadCards();
  } catch (error) {
    els.state.textContent = `确认失败：${error.message}`;
  }
}

/**
 * 请求删除指定卡片并刷新列表。
 * 服务端验证归属，失败时显示原因；本地不会仅凭点击立即移除记录。
 */
async function deleteCard(id) {
  try {
    await api(`/api/memory-cards/${id}`, { method: "DELETE" });
    await loadCards();
  } catch (error) {
    els.state.textContent = `删除失败：${error.message}`;
  }
}
