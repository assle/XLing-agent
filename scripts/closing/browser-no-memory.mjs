import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire('/Users/assle/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const { chromium } = require('playwright-core');
const out = resolve('.scratch/closing-evidence/browser-no-memory');
mkdirSync(out, { recursive: true });
const result = { status: 'running', observedAtUTC: new Date().toISOString(), scope: 'real UI and models; synthetic memory marker; structural trace checked separately', turns: [] };
const save = () => writeFileSync(resolve(out, 'result.json'), JSON.stringify(result, null, 2) + '\n');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 1400, height: 980 }, recordVideo: { dir: out } });
const page = await context.newPage();
page.setDefaultTimeout(180000);
async function send(message, name) {
  const ready = page.waitForResponse(r => r.url().endsWith('/api/chat/stream'));
  await page.fill('#messageInput', message);
  await page.click('#sendButton');
  const response = await ready;
  assert(response.ok());
  await response.finished();
  await page.waitForFunction(() => document.querySelector('#sessionBadge').textContent === 'DONE');
  const state = await page.evaluate(async () => {
    const { api, state } = await import('/app.js');
    return await (await api('/api/sessions/' + state.sessionId)).json();
  });
  result.turns.push({ name, state, stream: await response.text() });
  await page.screenshot({ path: resolve(out, name + '.png'), fullPage: true });
  save();
}
try {
  await page.goto('http://127.0.0.1:18381');
  await page.fill('#username', 'student');
  await page.fill('#password', 'student123');
  const ready = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'GET');
  await page.click('#loginForm button[type="submit"]');
  await (await ready).finished();
  await page.waitForSelector('#loginForm[hidden]', { state: 'attached' });
  await page.click('#openMemoryCards');
  await page.fill('#memoryCardNew', '工程验收代号是蓝帆-7821。此卡片仅用于工程隔离验证。');
  const created = page.waitForResponse(r => r.url().endsWith('/api/memory-cards') && r.request().method() === 'POST');
  await page.click('#memoryCardCreate button[type="submit"]');
  const cardResponse = await created;
  assert(cardResponse.ok());
  result.card = await cardResponse.json();
  assert.equal(result.card.confirmed, true);
  await page.waitForFunction(() => document.querySelector('#memoryCardList').textContent.includes('蓝帆-7821'));
  await page.screenshot({ path: resolve(out, 'confirmed-card.png'), fullPage: true });
  await page.click('[data-close="memoryCardsModal"]');
  await page.uncheck('#noMemoryCheck');
  await page.click('#newSession');
  await send('我的已确认记忆卡片中的工程验收代号是什么？只回答代号，如果不知道就说不知道。', 'normal-memory');
  await page.check('#noMemoryCheck');
  await page.click('#newSession');
  await send('我的已确认记忆卡片中的工程验收代号是什么？只回答代号，如果不知道就说不知道。', 'no-long-memory');
  assert.equal(result.turns.at(-1).state.noMemory, true);
  await send('本次会话的临时标记是青灯-4612。请记住这个本次对话标记。', 'same-session-marker');
  await send('我在本次对话中刚才给的临时标记是什么？只回答标记。', 'same-session-history');
  assert.equal(result.turns.at(-1).state.noMemory, true);
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
  console.log(JSON.stringify({ status: result.status, sessions: result.turns.map(x => x.state.sessionId), video: result.video }));
}
