// Xling 前端适配 — Playwright 端到端验证
// 覆盖 PRD 验证清单：隐私页 / 登录 / 画像 / CBT+进度标签 / 行动计划面板 /
// check-in / 记忆卡片 / 无记忆会话 / 量表筛查 / 数据删除 / 管理端扩展人审
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const BASE = "http://127.0.0.1:8080";
const SHOTS = new URL("./shots/", import.meta.url).pathname;
mkdirSync(SHOTS, { recursive: true });

let failures = 0;
function assert(cond, label) {
  if (cond) console.log(`  PASS  ${label}`);
  else { failures += 1; console.error(`  FAIL  ${label}`); }
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", (err) => console.error("  [pageerror]", err.message));

const shot = (name) => page.screenshot({ path: `${SHOTS}${name}.png`, fullPage: false });
const visible = (sel) => page.isVisible(sel);

async function login(username, password) {
  await page.fill("#username", username);
  await page.fill("#password", password);
  await page.click('#loginForm button[type="submit"]');
  await page.waitForFunction(() => document.querySelector("#accountBadge")?.hidden === false, null, { timeout: 8000 });
}

async function logout() {
  await page.click("#switchAccount");
  await page.waitForSelector("#loginForm:not([hidden])", { timeout: 8000 });
}

async function sendChat(message) {
  await page.fill("#messageInput", message);
  await page.click("#sendButton");
  await page.waitForFunction(
    () => ["DONE", "ERROR"].includes(document.querySelector("#sessionBadge")?.textContent),
    null, { timeout: 15000 }
  );
}

// ---------------------------------------------------------------- 1 隐私页
console.log("1. 隐私说明页（无需登录）");
await page.goto(`${BASE}/privacy.html`, { waitUntil: "networkidle" });
await page.waitForSelector(".privacy-card h1", { timeout: 8000 });
assert(await visible(".privacy-card"), "隐私说明内容渲染");
const noticeText = await page.textContent(".privacy-card");
assert(noticeText.includes("我们收集什么") && noticeText.includes("无记忆会话"), "说明包含关键章节");
assert(await visible(".back-link"), "返回应用链接存在");
await shot("01-privacy");

// ---------------------------------------------------------------- 2 登录
console.log("2. 学生登录");
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await login("student", "student123");
assert(await visible("#studentView"), "学生视图可见");
assert(await visible("#profilePanel"), "画像面板可见");
await shot("02-login");

// ---------------------------------------------------------------- 3 画像
console.log("3. 用户画像（备考阶段）");
assert(await visible("#profileOnboarding"), "首次进入显示填写/跳过提示");
await shot("03-profile-onboarding");
await page.click("#profileFill");
assert(await visible("#profileForm"), "点击填写后展开表单");
await page.selectOption("#profileStage", "冲刺");
await page.fill("#profileTargetExam", "考研");
await page.fill("#profileExamDate", "2026-12-21");
await page.click('#profileForm button[type="submit"]');
await page.waitForFunction(
  () => document.querySelector("#profileSummaryText")?.textContent.includes("冲刺"),
  null, { timeout: 8000 }
);
const summary = await page.textContent("#profileSummaryText");
assert(summary.includes("冲刺阶段") && summary.includes("考研") && summary.includes("2026-12-21"), `画像摘要正确（${summary.trim()}）`);
await shot("04-profile-filled");

// 编辑并清空可选字段
await page.click("#profileEdit");
await page.fill("#profileExamDate", "");
await page.click('#profileForm button[type="submit"]');
await page.waitForFunction(
  () => !document.querySelector("#profileSummaryText")?.textContent.includes("2026-12-21"),
  null, { timeout: 8000 }
);
assert(true, "清空可选字段保存成功");
// 恢复日期，供后续演示
await page.click("#profileEdit");
await page.fill("#profileExamDate", "2026-12-21");
await page.click('#profileForm button[type="submit"]');
await page.waitForFunction(
  () => document.querySelector("#profileSummaryText")?.textContent.includes("2026-12-21"),
  null, { timeout: 8000 }
);

// ---------------------------------------------------------------- 4 CBT + 进度标签
console.log("4. CBT 结构化追问 + 进度标签");
await sendChat("最近考研复习压力很大");
assert(await visible("#cbtTag"), "CBT 进度标签出现");
const tag1 = await page.textContent("#cbtTag");
assert(tag1.includes("结构化支持") && tag1.includes("可随时退出"), `标签文案正确（${tag1.trim()}）`);
await shot("05-cbt-progress");
await sendChat("我觉得自己肯定考不上，很焦虑");
await sendChat("最近总是失眠，心跳很快");
const tag3 = await page.textContent("#cbtTag");
assert(tag3.includes("3/4"), `进度推进到 3/4（${tag3.trim()}）`);
await sendChat("我开始逃避复习，压力很大一直拖延");
assert(!(await visible("#cbtTag")), "CBT 完成后标签消失");

// ---------------------------------------------------------------- 5 行动计划面板
console.log("5. 行动计划面板");
assert(await visible("#planPanel"), "行动计划面板出现");
const itemCount = await page.locator("#planItems .plan-item").count();
assert(itemCount >= 1, `计划包含 ${itemCount} 个条目`);
const progressText = await page.textContent("#planProgress");
assert(/0\/\d+ 已完成/.test(progressText), `进度显示（${progressText.trim()}）`);
await shot("06-action-plan");

// 勾选完成第一条
await page.locator("#planItems .plan-item input[type=checkbox]").first().check();
await page.waitForFunction(
  () => document.querySelector("#planProgress")?.textContent.startsWith("1/"),
  null, { timeout: 8000 }
);
assert(true, "勾选条目后进度更新为 1/N");

// 替换第二条
const secondContent = await page.locator("#planItems .plan-item .plan-item-text").nth(1).textContent();
// 已完成条目没有替换按钮，此时第一个替换按钮属于第二条
await page.locator("#planItems .plan-item:not(.done) .replace-btn").first().click();
await page.locator("#planItems .plan-replace-row input").first().fill("去操场慢走 10 分钟");
await page.locator("#planItems .plan-replace-row button").first().click();
await page.waitForFunction(
  (old) => document.querySelectorAll("#planItems .plan-item .plan-item-text")[1]?.textContent !== old,
  secondContent, { timeout: 8000 }
);
const newContent = await page.locator("#planItems .plan-item .plan-item-text").nth(1).textContent();
assert(newContent === "去操场慢走 10 分钟", `条目替换成功（${newContent}）`);
await shot("07-plan-item-updated");

// ---------------------------------------------------------------- 6 check-in
console.log("6. 次日 check-in");
assert(await visible("#checkinStart"), "check-in 按钮出现在面板内");
await page.click("#checkinStart");
assert(await visible("#checkinForm"), "check-in 表单展开");
await page.check('input[name="improvement"][value="improved"]');
await page.fill("#checkinNotes", "今天状态好一些");
await page.click('#checkinForm button[type="submit"]');
await page.waitForFunction(
  () => document.querySelector("#planPanel")?.hidden === true,
  null, { timeout: 8000 }
);
assert(true, "提交好转反馈后计划完成、面板收起");
await shot("08-checkin-done");

// ---------------------------------------------------------------- 7 记忆卡片
console.log("7. 记忆卡片");
await page.click("#openMemoryCards");
await page.waitForSelector("#memoryCardList .memory-card, #memoryCardList .hint", { timeout: 8000 });
await page.fill("#memoryCardNew", "我习惯晚上复习，白天效率低");
await page.click('#memoryCardCreate button[type="submit"]');
await page.waitForSelector('#memoryCardList .memory-card', { timeout: 8000 });
assert((await page.locator("#memoryCardList .memory-card").count()) >= 1, "新建卡片出现在列表中");
// 编辑
await page.locator('#memoryCardList .memory-card button:text-is("编辑")').first().click();
await page.locator("#memoryCardList .card-edit-row input").first().fill("我习惯晚上 9 点后复习");
await page.locator('#memoryCardList .card-edit-row button:text-is("保存")').first().click();
await page.waitForFunction(
  () => document.querySelector("#memoryCardList .memory-card p")?.textContent.includes("晚上 9 点"),
  null, { timeout: 8000 }
);
assert(true, "编辑卡片成功");
// 删除
await page.locator('#memoryCardList .memory-card button:text-is("删除")').first().click();
await page.waitForFunction(
  () => !document.querySelector("#memoryCardList .memory-card p")?.textContent.includes("晚上 9 点"),
  null, { timeout: 8000 }
);
assert(true, "删除卡片成功");
await shot("09-memory-cards");
await page.click('[data-close="memoryCardsModal"]');

// ---------------------------------------------------------------- 8 无记忆会话
console.log("8. 无记忆会话");
await page.check("#noMemoryCheck");
await page.click("#newSession");
assert(await visible("#noMemoryBadge"), "无记忆模式标签出现在聊天头部");
await shot("10-no-memory");
await page.uncheck("#noMemoryCheck");
await page.click("#newSession");
assert(!(await visible("#noMemoryBadge")), "取消勾选后新会话不显示标签");

// ---------------------------------------------------------------- 9 量表筛查
console.log("9. 量表筛查");
await page.click("#openScreening");
await page.waitForSelector("#screeningModal:not([hidden])", { timeout: 8000 });
await page.click('.scale-choice[data-scale="GAD-7"]');
await page.waitForSelector("#scaleIntro:not([hidden])", { timeout: 8000 });
const introText = await page.textContent("#scaleIntroBody");
assert(introText.includes("计分规则") && introText.includes("不作诊断"), "说明页含计分规则与非诊断声明");
await shot("11-screening-intro");
await page.click("#scaleStart");
await page.waitForSelector("#scaleQuestions:not([hidden])", { timeout: 8000 });
const qCount = await page.locator("#scaleQuestions .scale-question").count();
assert(qCount === 7, `GAD-7 共 7 题（实际 ${qCount}）`);
await shot("12-screening-questions");
// 全部选「有几天」
for (let i = 0; i < qCount; i += 1) {
  await page.check(`input[name="q${i}"][value="1"]`);
}
await page.click('#scaleQuestions button[type="submit"]');
await page.waitForSelector("#scaleResult:not([hidden])", { timeout: 8000 });
const resultText = await page.textContent("#scaleResult");
assert(resultText.includes("7 分") && resultText.includes("GAD-7"), `结果展示分数（${resultText.trim().slice(0, 40)}…）`);
await shot("13-screening-result");
// 历史记录 tab
await page.click("#historyTabBtn");
await page.waitForSelector("#screeningHistory .history-item", { timeout: 8000 });
assert((await page.locator("#screeningHistory .history-item").count()) === 1, "历史记录显示本次结果");
await page.click('[data-close="screeningModal"]');

// ---------------------------------------------------------------- 10 管理端扩展人审
console.log("10. 管理端扩展人审");
await logout();
await login("admin", "admin123");
assert(await visible("#adminView"), "管理视图可见");
assert(await visible("#reviewPanel"), "人审面板可见");
// 等待审核列表加载完成（已造数一条待审记录，必须出现）
await page.waitForSelector("#reviews .review-item", { timeout: 8000 });
const reviewCount = await page.locator("#reviews .review-item").count();
assert(reviewCount >= 1, `审核队列有待审条目（${reviewCount}）`);
// 接管原因 badge
const badge = await page.locator("#reviews .review-item .badge").first().textContent();
assert(["高风险关键词", "风险轨迹上升", "持续无改善", "用户请求", "超时未审"].includes(badge.trim()), `接管原因 badge（${badge.trim()}）`);
// 脱敏摘要折叠/展开
await page.locator(".review-summary-toggle").first().click();
assert(await page.locator(".review-summary").first().isVisible(), "脱敏摘要可展开");
await shot("15-admin-reviews");
// 四决定按钮存在
for (const cls of ["decide-approve", "decide-reject", "decide-refer", "decide-monitor"]) {
  assert(await page.locator(`#reviews .review-item .${cls}`).first().isVisible(), `按钮存在：${cls}`);
}
// 选择「持续关注」+ 备注
await page.locator("#reviews .review-item .decide-monitor").first().click();
await page.fill(".review-note-row input", "先持续关注两天");
await page.click(".review-note-row button");
await page.waitForFunction(
  () => document.querySelectorAll("#reviews .review-item").length === 0
    || !document.querySelector("#reviews .review-item .review-decisions"),
  null, { timeout: 8000 }
);
assert(true, "提交决定后条目离开待审队列");
// 显示已处理：只读展示决定结果
await page.check("#reviewShowAll");
await page.waitForSelector("#reviews .review-item.decided", { timeout: 8000 });
const outcome = await page.locator("#reviews .review-item.decided .review-outcome").first().textContent();
assert(outcome.includes("持续关注") && outcome.includes("先持续关注两天"), `已决项显示决定与备注（${outcome.trim()}）`);
assert((await page.locator("#reviews .review-item.decided .review-decisions").count()) === 0, "已决项不可再次操作");
await shot("16-admin-decision");

// ---------------------------------------------------------------- 11 数据删除
console.log("11. 数据删除");
await logout();
await login("student", "student123");
await page.click("#openDataDelete");
await page.waitForSelector("#dataDeleteModal:not([hidden])", { timeout: 8000 });
const impactText = await page.textContent("#dataDeleteModal");
assert(impactText.includes("不可撤销") && impactText.includes("记忆卡片") && impactText.includes("量表筛查"), "影响清单与不可逆警告完整");
await shot("14-data-delete");
await page.click("#confirmDataDelete");
await page.waitForSelector("#loginForm:not([hidden])", { timeout: 10000 });
assert(true, "删除后退出到登录页");
// 账号已失效：再次登录应失败
await page.fill("#username", "student");
await page.fill("#password", "student123");
await page.click('#loginForm button[type="submit"]');
await page.waitForFunction(
  () => document.querySelector("#loginState")?.textContent.includes("登录失败"),
  null, { timeout: 8000 }
);
assert(true, "删除后账号无法再登录");

console.log(`\n${failures === 0 ? "ALL PASS" : failures + " FAILURES"}`);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
