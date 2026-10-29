import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire('/Users/assle/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const { chromium } = require('playwright-core');
const out = resolve('.scratch/closing-evidence/browser-support-verified');
mkdirSync(out, { recursive: true });
const result = { status: 'running', observedAtUTC: new Date().toISOString(), scope: 'real UI and models; fictional engineering persona', scenes: [], chats: [] };
const save = () => writeFileSync(resolve(out, 'result.json'), JSON.stringify(result, null, 2) + '\n');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 1400, height: 980 }, timezoneId: 'Asia/Shanghai', recordVideo: { dir: out, size: { width: 1400, height: 980 } } });
const page = await context.newPage();
page.setDefaultTimeout(180000);
const observations = [];
page.on('response', response => {
  if (response.url().endsWith('/api/chat/stream')) {
    observations.push((async () => {
      const text = await response.text();
      const events = [];
      let event;
      for (const line of text.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) events.push({ event, data: JSON.parse(line.slice(5).trim()) });
      }
      result.chats.push({ status: response.status(), events });
      save();
    })());
  }
});

async function send(text, name) {
  const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/chat/stream'));
  await page.fill('#messageInput', text);
  await page.click('#sendButton');
  const response = await responsePromise;
  await response.finished();
  assert(response.ok(), `chat returned HTTP ${response.status()}`);
  await Promise.all(observations);
  await page.waitForFunction(() => ['DONE', 'REVIEW'].includes(document.querySelector('#sessionBadge').textContent));
  result.scenes.push({ name, message: text, badge: await page.textContent('#sessionBadge'), visibleText: await page.textContent('#messages') });
  await page.screenshot({ path: resolve(out, name + '.png'), fullPage: true });
  save();
}

try {
  await page.goto('http://127.0.0.1:18381');
  await page.fill('#username', 'student');
  await page.fill('#password', 'student123');
  const sessionsReady = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'GET');
  await page.click('#loginForm button[type="submit"]');
  await page.waitForSelector('#loginForm[hidden]', { state: 'attached' });
  await (await sessionsReady).finished();
  await page.waitForSelector('#studentView:not([hidden])');
  await page.click('#newSession');
  await send('请用两句话说明 Python 的列表和元组有什么不同。', 'daily');
  assert.equal(await page.textContent('#sessionBadge'), 'DONE');
  await page.click('#newSession');
  await send('最近工作汇报让我有些紧张，想到明天要向团队介绍进展就担心表达不清。', 'support-first-turn');
  await send('触发事件是明天的团队汇报；我的想法是担心准备不够、表达不好；身体反应是心跳快、手心出汗；我的行为是反复修改幻灯片、暂时回避打开提纲。', 'support-dimensions');
  await page.waitForSelector('#planPanel:not([hidden])');
  result.planText = await page.textContent('#planPanel');
  const completeBox = page.locator('#planItems input[type="checkbox"]').first();
  if (await completeBox.count()) await completeBox.check();
  await page.click('#checkinStart');
  await page.check('input[name="improvement"][value="improved"]');
  await page.fill('#checkinNotes', '工程复现中提前反馈：完成了一个小步骤，压力有所缓解。');
  const feedbackPromise = page.waitForResponse(r => r.url().endsWith('/api/check-ins') && r.request().method() === 'POST');
  await page.click('#checkinForm button[type="submit"]');
  const feedback = await feedbackPromise;
  result.feedback = await feedback.json();
  assert(feedback.ok());
  assert.equal(result.feedback.escalated, false);
  await page.screenshot({ path: resolve(out, 'support-feedback.png'), fullPage: true });
  result.status = 'completed';
} catch (error) {
  result.status = 'failed';
  result.error = { type: error.name, message: error.message };
  await page.screenshot({ path: resolve(out, 'failure.png'), fullPage: true });
  throw error;
} finally {
  await context.close();
  result.video = await page.video().path();
  save();
  await browser.close();
  console.log(JSON.stringify({ status: result.status, scenes: result.scenes.map(s => s.name), video: result.video }));
}
