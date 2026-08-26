// 管理后台：指标总览、报告列表、对话查看、知识库上传、人工审核队列。

import { api, displayTime } from "/app.js";

const els = {};

const HANDOFF_REASON_LABELS = {
  HIGH_RISK_KEYWORD: "高风险关键词",
  RISK_TRAJECTORY_RISING: "风险轨迹上升",
  SUSTAINED_NO_IMPROVEMENT: "持续无改善",
  USER_REQUEST: "用户请求",
  TIMEOUT: "超时未审"
};

const DECISION_LABELS = {
  approve: "放行",
  reject: "拒绝",
  refer: "转介",
  monitor: "持续关注"
};

const OUTCOME_LABELS = { ...DECISION_LABELS, timeout: "超时自动兜底" };

const STATUS_LABELS = {
  approved: "已放行",
  rejected: "已拒绝",
  referred: "已转介",
  monitoring: "持续关注中",
  escalated: "已自动升级"
};

export function initAdmin() {
  els.reports = document.querySelector("#reports");
  els.refreshAdmin = document.querySelector("#refreshAdmin");
  els.metricReports = document.querySelector("#metricReports");
  els.metricHigh = document.querySelector("#metricHigh");
  els.metricExcel = document.querySelector("#metricExcel");
  els.metricAlerts = document.querySelector("#metricAlerts");
  els.conversationState = document.querySelector("#conversationState");
  els.conversationDetail = document.querySelector("#conversationDetail");
  els.knowledgeUploadForm = document.querySelector("#knowledgeUploadForm");
  els.knowledgeFile = document.querySelector("#knowledgeFile");
  els.knowledgeUploadState = document.querySelector("#knowledgeUploadState");
  els.reviews = document.querySelector("#reviews");
  els.refreshReviews = document.querySelector("#refreshReviews");
  els.reviewShowAll = document.querySelector("#reviewShowAll");

  els.refreshAdmin?.addEventListener("click", loadAdminDashboard);
  els.knowledgeUploadForm?.addEventListener("submit", uploadKnowledgeFile);
  els.refreshReviews?.addEventListener("click", loadReviews);
  els.reviewShowAll?.addEventListener("change", loadReviews);
}

// ---------------------------------------------------------------------------
// Dashboard / reports / conversation / knowledge
// ---------------------------------------------------------------------------

export async function loadAdminDashboard() {
  const [reportsRes, excelRes, alertsRes] = await Promise.all([
    api("/api/admin/reports"),
    api("/api/admin/excel-records"),
    api("/api/admin/alerts")
  ]);
  const reports = await reportsRes.json();
  const excel = await excelRes.json();
  const alerts = await alertsRes.json();
  els.metricReports.textContent = reports.length;
  els.metricHigh.textContent = reports.filter((item) => item.riskLevel === "HIGH").length;
  els.metricExcel.textContent = excel.length;
  els.metricAlerts.textContent = alerts.length;
  renderReports(reports);
}

function renderReports(reports) {
  els.reports.innerHTML = "";
  if (!reports.length) {
    els.reports.innerHTML = `<div class="empty small"><strong>暂无报告</strong><p>学生咨询或风险场景会在这里沉淀记录。</p></div>`;
    return;
  }
  for (const item of reports) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `report risk-${item.riskLevel.toLowerCase()}`;
    card.dataset.sessionId = item.sessionId;

    const head = document.createElement("div");
    head.className = "report-head";
    const title = document.createElement("strong");
    title.textContent = `${item.displayName} · ${item.riskLevel}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.createdAt);
    head.append(title, time);

    const summary = document.createElement("p");
    summary.textContent = item.summary;
    const content = document.createElement("small");
    content.textContent = item.content;
    const action = document.createElement("span");
    action.className = "report-action";
    action.textContent = "查看完整对话";

    card.append(head, summary, content, action);
    card.addEventListener("click", () => loadConversation(item.sessionId));
    els.reports.append(card);
  }
}

async function loadConversation(sessionId) {
  if (!sessionId) {
    els.conversationState.textContent = "该报告缺少会话 ID";
    return;
  }
  els.conversationState.textContent = "正在读取...";
  els.conversationDetail.innerHTML = `<div class="empty small"><strong>加载中</strong><p>正在读取完整对话。</p></div>`;
  for (const card of els.reports.querySelectorAll(".report")) {
    card.classList.toggle("active", card.dataset.sessionId === sessionId);
  }
  try {
    const response = await api(`/api/admin/conversations/${encodeURIComponent(sessionId)}`);
    renderConversation(await response.json());
  } catch (error) {
    els.conversationState.textContent = "读取失败";
    els.conversationDetail.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty small";
    const title = document.createElement("strong");
    title.textContent = "无法查看对话";
    const detail = document.createElement("p");
    detail.textContent = error.message;
    empty.append(title, detail);
    els.conversationDetail.append(empty);
  }
}

function renderConversation(conversation) {
  const messages = conversation.messages || [];
  els.conversationState.textContent = `${conversation.title || conversation.sessionId} · ${messages.length} 条消息`;
  els.conversationDetail.innerHTML = "";
  if (!messages.length) {
    els.conversationDetail.innerHTML = `<div class="empty small"><strong>暂无消息</strong><p>这个会话还没有写入消息记录。</p></div>`;
    return;
  }
  for (const message of messages) {
    const role = (message.role || "").toLowerCase();
    const row = document.createElement("article");
    row.className = `conversation-message ${role}`;

    const meta = document.createElement("div");
    meta.className = "conversation-meta";
    const label = document.createElement("strong");
    label.textContent = roleLabel(message.role);
    const time = document.createElement("span");
    time.textContent = displayTime(message.createdAt);
    meta.append(label, time);

    const bubble = document.createElement("div");
    bubble.className = "conversation-bubble";
    bubble.textContent = message.content || "";

    row.append(meta, bubble);
    els.conversationDetail.append(row);
  }
}

function roleLabel(role) {
  const value = (role || "").toUpperCase();
  if (value === "USER") return "学生";
  if (value === "ASSISTANT") return "Xling";
  if (value === "SYSTEM") return "系统";
  return role || "未知角色";
}

async function uploadKnowledgeFile(event) {
  event.preventDefault();
  const file = els.knowledgeFile.files?.[0];
  if (!file) {
    els.knowledgeUploadState.textContent = "请先选择文件";
    return;
  }
  const data = new FormData();
  data.append("file", file);
  els.knowledgeUploadState.textContent = "正在切分入库...";
  try {
    const response = await api("/api/admin/knowledge/file", { method: "POST", body: data });
    const result = await response.json();
    els.knowledgeUploadState.textContent = `${result.source} 已入库 ${result.chunks} 个片段`;
    els.knowledgeFile.value = "";
  } catch (error) {
    els.knowledgeUploadState.textContent = `上传失败：${error.message}`;
  }
}

// ---------------------------------------------------------------------------
// Human review queue (expanded: handoff reason, desensitized summary, 4 decisions)
// ---------------------------------------------------------------------------

export async function loadReviews() {
  els.reviews.innerHTML = `<p class="hint">读取中...</p>`;
  try {
    const includeAll = els.reviewShowAll.checked;
    const response = await api(`/api/admin/reviews${includeAll ? "?all=true" : ""}`);
    renderReviews(await response.json());
  } catch (error) {
    els.reviews.innerHTML = `<p class="hint">审核队列读取失败：${error.message}</p>`;
  }
}

function renderReviews(items) {
  els.reviews.innerHTML = "";
  if (!items.length) {
    els.reviews.innerHTML = `<p class="hint">当前没有待审核的记录。</p>`;
    return;
  }
  for (const item of items) {
    els.reviews.append(renderReviewItem(item));
  }
}

function renderReviewItem(item) {
  const box = document.createElement("div");
  const decided = item.status && item.status !== "pending";
  box.className = `review-item${decided ? " decided" : ""}`;

  const head = document.createElement("div");
  head.className = "review-item-head";
  const reason = document.createElement("span");
  reason.className = `badge reason-${item.handoffReason || "TIMEOUT"}`;
  reason.textContent = HANDOFF_REASON_LABELS[item.handoffReason] || item.handoffReason || "未知原因";
  const meta = document.createElement("span");
  meta.className = "hint";
  const risk = item.riskLevel ? `风险 ${item.riskLevel}` : "风险未知";
  meta.textContent = `#${item.reviewId} · ${risk} · ${displayTime(item.createdAt)}`;
  head.append(reason, meta);
  box.append(head);

  if (item.riskSummary) {
    const summary = document.createElement("p");
    summary.textContent = item.riskSummary;
    box.append(summary);
  }
  if (item.desensitizedSummary) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "review-summary-toggle";
    toggle.textContent = "展开脱敏摘要 ▾";
    const detail = document.createElement("div");
    detail.className = "review-summary";
    detail.textContent = item.desensitizedSummary;
    detail.hidden = true;
    toggle.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      toggle.textContent = detail.hidden ? "展开脱敏摘要 ▾" : "收起脱敏摘要 ▴";
    });
    box.append(toggle, detail);
  }

  if (decided) {
    const outcome = document.createElement("p");
    outcome.className = "review-outcome";
    const decision = item.reviewerDecision ? (OUTCOME_LABELS[item.reviewerDecision] || item.reviewerDecision) : (STATUS_LABELS[item.status] || item.status);
    const parts = [`处理结果：${decision}`];
    if (item.reviewedBy) parts.push(`审核人：${item.reviewedBy}`);
    if (item.reviewerNote) parts.push(`备注:${item.reviewerNote}`);
    if (item.referralTarget) parts.push(`转介对象：${item.referralTarget}`);
    if (item.nextStep) parts.push(`下一步：${item.nextStep}`);
    if (item.followUpOwner) parts.push(`跟进负责人：${item.followUpOwner}`);
    if (item.followUpAt) parts.push(`跟进时间：${displayTime(item.followUpAt)}`);
    outcome.textContent = parts.join(" · ");
    box.append(outcome);
    return box;
  }

  const decisions = document.createElement("div");
  decisions.className = "review-decisions";
  for (const [value, label] of Object.entries(DECISION_LABELS)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `decide-${value}`;
    button.textContent = label;
    button.addEventListener("click", () => showNoteRow(box, item.reviewId, value, label));
    decisions.append(button);
  }
  box.append(decisions);
  return box;
}

function showNoteRow(box, reviewId, decision, label) {
  box.querySelector(".review-note-row")?.remove();
  const row = document.createElement("div");
  row.className = "review-note-row";
  const fields = {};
  const addField = (name, placeholder, type = "text") => {
    const input = document.createElement("input");
    input.type = type;
    input.placeholder = placeholder;
    fields[name] = input;
    row.append(input);
  };
  addField("note", `备注（可选），将随「${label}」一起记录`);
  if (decision === "refer") {
    addField("referralTarget", "转介对象（必填）");
    addField("nextStep", "下一步（必填）");
  }
  if (decision === "monitor") {
    addField("followUpOwner", "跟进负责人（必填）");
    addField("followUpAt", "跟进时间（必填）", "datetime-local");
  }
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.textContent = "确认";
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      await api(`/api/admin/reviews/${reviewId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          note: fields.note.value.trim(),
          referralTarget: fields.referralTarget?.value.trim() || null,
          nextStep: fields.nextStep?.value.trim() || null,
          followUpOwner: fields.followUpOwner?.value.trim() || null,
          followUpAt: fields.followUpAt?.value || null,
        })
      });
      await loadReviews();
    } catch (error) {
      confirm.disabled = false;
      fields.note.value = "";
      alert(`操作失败：${error.message}`);
    }
  });
  row.append(confirm);
  box.append(row);
  fields[decision === "refer" ? "referralTarget" : decision === "monitor" ? "followUpOwner" : "note"].focus();
}
