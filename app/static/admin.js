// 管理后台：指标总览、报告列表、对话查看、知识库上传、人工审核队列。

import { api, displayTime } from "/app.js";

const els = {};
const reportSessions = new Map();

const TASK_KIND_LABELS = { EXCEL_REPORT: "台账导出", RISK_ALERT: "预警通知" };
const TASK_STATUS_LABELS = { PENDING: "待执行", RUNNING: "执行中", SUCCESS: "成功", DEAD: "失败留存" };

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

/**
 * 绑定审核后台的刷新、上传和列表范围切换事件。
 * 可选访问允许部分页面缺少管理元素时跳过绑定。
 */
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
  els.toolJobs = document.querySelector("#toolJobs");
  els.deadLetters = document.querySelector("#deadLetters");
  els.refreshTasks = document.querySelector("#refreshTasks");

  els.refreshAdmin?.addEventListener("click", loadAdminDashboard);
  els.knowledgeUploadForm?.addEventListener("submit", uploadKnowledgeFile);
  els.refreshReviews?.addEventListener("click", loadReviews);
  els.reviewShowAll?.addEventListener("change", loadReviews);
  els.refreshTasks?.addEventListener("click", loadTasks);
}

// ---------------------------------------------------------------------------
// Dashboard / reports / conversation / knowledge
// ---------------------------------------------------------------------------

/**
 * 并行读取评估记录、表格记录和通知记录，更新指标与列表。
 * 数量按本次返回列表计算，接口有数量上限，因此不是数据库全量统计。
 */
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
  // 统计本次列表中高风险记录数量，不重新评估用户消息。
  els.metricHigh.textContent = reports.filter((item) => item.riskLevel === "HIGH").length;
  els.metricExcel.textContent = excel.length;
  els.metricAlerts.textContent = alerts.length;
  renderReports(reports);
  await loadTasks();
}

/**
 * 把安全评估记录绘制为可点击卡片。
 * 展示时间、风险和摘要，点击后按对应会话编号加载完整消息。
 */
function renderReports(reports) {
  els.reports.innerHTML = "";
  reportSessions.clear();
  if (!reports.length) {
    els.reports.innerHTML = `<div class="empty small"><strong>暂无报告</strong><p>用户心理支持或风险场景会在这里沉淀记录。</p></div>`;
    return;
  }
  for (const item of reports) {
    if (item.sessionId) reportSessions.set(item.id, item.sessionId);
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
    // 点击当前记录卡片后才加载它关联的会话。
    card.addEventListener("click", () => loadConversation(item.sessionId));
    els.reports.append(card);
  }
}

/** 读取后台任务和失败留存；两处独立显示加载、空列表或读取失败。 */
export async function loadTasks() {
  await Promise.all([
    loadTaskList(els.toolJobs, "/api/admin/tool-jobs", false),
    loadTaskList(els.deadLetters, "/api/admin/dead-letters", true)
  ]);
}

async function loadTaskList(target, path, deadLetters) {
  target.textContent = "读取中...";
  try {
    const response = await api(path);
    const items = await response.json();
    target.textContent = "";
    if (!items.length) {
      target.textContent = deadLetters ? "没有失败留存记录。" : "暂无后台任务。";
      return;
    }
    for (const item of items) target.append(renderTaskItem(item, deadLetters));
  } catch {
    target.textContent = deadLetters ? "失败留存读取失败，请刷新重试。" : "任务状态读取失败，请刷新重试。";
  }
}

function renderTaskItem(item, deadLetter) {
  const card = document.createElement("article");
  card.className = "review-item task-item";
  if (deadLetter) card.dataset.deadLetterId = item.id;
  else card.dataset.jobId = item.id;
  const head = document.createElement("div");
  head.className = "review-item-head";
  const title = document.createElement("strong");
  title.textContent = `${TASK_KIND_LABELS[item.kind] || "后台任务"} #${deadLetter ? item.jobId ?? "—" : item.id}`;
  const status = document.createElement("span");
  const retry = !deadLetter && item.status === "PENDING" && item.attempts > 0 && item.lastError;
  status.textContent = deadLetter ? "失败留存" : retry ? "等待重试" : TASK_STATUS_LABELS[item.status] || "状态未知";
  status.className = `pill ${deadLetter || item.status === "DEAD" ? "danger" : item.status === "SUCCESS" ? "ok" : "warn"}`;
  head.append(title, status);
  card.append(head);

  const metadata = document.createElement("p");
  metadata.className = "hint";
  const parts = [`报告 #${item.reportId}`];
  if (deadLetter) parts.push(`留存记录 #${item.id}`, `留存时间：${displayTime(item.createdAt)}`);
  else {
    parts.push(`尝试 ${item.attempts}/${item.maxAttempts} 次`, `创建：${displayTime(item.createdAt)}`, `更新：${displayTime(item.updatedAt)}`);
    if (item.dependsOnJobId) parts.push(`前置任务 #${item.dependsOnJobId}`);
    if (item.status === "PENDING") parts.push(`${retry ? "下次重试" : "排队时间"}：${displayTime(item.runAfter)}`);
  }
  metadata.textContent = parts.join(" · ");
  card.append(metadata);

  const failure = deadLetter ? item.reason : item.lastError;
  if (failure) {
    const reason = document.createElement("p");
    reason.textContent = taskFailureSummary(failure);
    card.append(reason);
  }
  const threadId = reportSessions.get(item.reportId);
  if (threadId) {
    const view = document.createElement("button");
    view.type = "button";
    view.className = "ghost";
    view.textContent = "查看关联对话";
    view.addEventListener("click", () => loadConversation(threadId));
    card.append(view);
  }
  return card;
}

/** 原始异常可能带 SQL、消息正文或连接信息；页面只显示类别和操作相关概括。 */
function taskFailureSummary(error) {
  const type = String(error).split(":", 1)[0];
  const reasons = {
    SMTPDataError: "邮件服务拒绝接收通知",
    SMTPRecipientsRefused: "邮件服务未接受收件人",
    SMTPSenderRefused: "邮件服务未接受发件人",
    SMTPAuthenticationError: "邮件服务认证失败",
    SMTPConnectError: "无法连接邮件服务",
    SMTPServerDisconnected: "邮件服务连接中断",
    ConnectionRefusedError: "服务连接被拒绝",
    TimeoutError: "服务响应超时",
    ReadTimeout: "服务响应超时",
    PermissionError: "台账文件写入权限不足",
    FileNotFoundError: "任务所需文件不存在",
    OperationalError: "业务数据暂时不可写",
    RuntimeError: "服务配置或执行条件未满足"
  };
  const safeType = /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(type);
  return `${reasons[type] || "任务执行未成功，请核对服务配置和诊断记录"}${safeType ? `（${type}）` : ""}。`;
}

/**
 * 读取选中记录关联的会话并更新详情区域。
 * 缺少编号直接提示；读取时先高亮卡片，失败则替换为错误说明。
 */
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

/**
 * 按服务端返回顺序绘制消息及角色和时间。
 * 正文使用 textContent 显示，不将用户文字解释为页面标签。
 */
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

/**
 * 将服务端角色代码转成当前界面使用的文字。
 * 未知角色保留原值，空值显示占位说明；此函数不参与权限判断。
 */
function roleLabel(role) {
  const value = (role || "").toUpperCase();
  if (value === "USER") return "用户";
  if (value === "ASSISTANT") return "Xling";
  if (value === "SYSTEM") return "系统";
  return role || "未知角色";
}

/**
 * 把用户选择的文件作为表单上传并显示导入片段数。
 * 没有选文件时不请求，上传成功才清空文件选择。
 */
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

/**
 * 根据是否显示全部记录读取人工审核队列。
 * 加载期间显示提示，失败显示原因；排序由服务端提供。
 */
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

/**
 * 清空旧队列并依次插入审核记录卡片。
 * 空列表使用统一提示，具体状态和可操作按钮由单条渲染处理。
 */
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

/**
 * 展示审核原因、风险、可展开摘要及当前处理结果。
 * 已处理项只读展示决定与后续行动；待处理项才提供四类决定按钮。
 */
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
    // 切换摘要区域可见状态，同时同步展开或收起的按钮文字。
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
    // 把所选决定和当前审核编号传入详情表单。
    button.addEventListener("click", () => showNoteRow(box, item.reviewId, value, label));
    decisions.append(button);
  }
  box.append(decisions);
  return box;
}

/**
 * 按审核决定生成所需备注和后续行动输入框。
 * 转介收集去向和下一步，持续关注收集负责人和时间；确认后提交并刷新队列，失败恢复按钮。
 */
function showNoteRow(box, reviewId, decision, label) {
  box.querySelector(".review-note-row")?.remove();
  const row = document.createElement("div");
  row.className = "review-note-row";
  const fields = {};
  // 创建一个输入框并按字段名登记，便于提交时读取对应值。
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
  // 确认时禁用按钮避免重复点击，提交审核详情后重新读取队列。
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
          followUpAt: fields.followUpAt?.value ? new Date(fields.followUpAt.value).toISOString() : null,
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
