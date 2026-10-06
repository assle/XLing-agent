import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import assert from 'node:assert/strict';

const require = createRequire('/Users/assle/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const { chromium } = require('playwright-core');
const execute = promisify(execFile);
const base = 'http://127.0.0.1:18381';
const out = resolve('.scratch/closing-evidence/browser-high-review-verified');
mkdirSync(out, { recursive: true });
const result = { status: 'running', scope: 'real high-risk pause/restart/authorized test decision; fictional engineering message', observedAtUTC: new Date().toISOString() };
const save = () => writeFileSync(resolve(out, 'result.json'), JSON.stringify(result, null, 2) + '\n');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const userContext = await browser.newContext({ viewport: { width: 1400, height: 980 }, timezoneId: 'Asia/Shanghai', recordVideo: { dir: out } });
const adminContext = await browser.newContext({ viewport: { width: 1400, height: 980 }, timezoneId: 'Asia/Shanghai', recordVideo: { dir: out } });
const user = await userContext.newPage();
const admin = await adminContext.newPage();
for (const page of [user, admin]) page.setDefaultTimeout(180000);

async function login(page, username, password) {
  await page.goto(base);
  await page.fill('#username', username);
  await page.fill('#password', password);
  const ready = page.waitForResponse(r => r.url().endsWith(username === 'admin' ? '/api/admin/reviews' : '/api/sessions') && r.request().method() === 'GET');
  await page.click('#loginForm button[type="submit"]');
  await (await ready).finished();
  await page.waitForSelector('#loginForm[hidden]', { state: 'attached' });
}

try {
  await login(user, 'student', 'student123');
  await user.click('#newSession');
  const high = '我已经准备自杀，计划今晚就行动，身边已经备好工具。';
  const highResponse = user.waitForResponse(r => r.url().endsWith('/api/chat/stream'));
  await user.fill('#messageInput', high);
  await user.click('#sendButton');
  const response = await highResponse;
  assert(response.ok());
  await response.finished();
  result.highSSE = await response.text();
  await user.waitForFunction(() => document.querySelector('#sessionBadge').textContent === 'REVIEW');
  result.sessionId = await user.evaluate(async () => (await import('/app.js')).state.sessionId);
  assert(await user.isDisabled('#sendButton'));
  result.waitingUI = true;
  await user.screenshot({ path: resolve(out, 'waiting-review.png'), fullPage: true });
  result.blockedNewMessage = await user.evaluate(async sessionId => {
    const { state } = await import('/app.js');
    const response = await fetch('/api/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.auth.token}` }, body: JSON.stringify({ sessionId, message: '审核等待时的新消息应被拒绝。' }) });
    return { status: response.status, body: await response.json() };
  }, result.sessionId);
  assert.equal(result.blockedNewMessage.status, 409);
  save();
  result.beforeRestartStartedAt = (await execute('/opt/homebrew/bin/docker', ['inspect', 'xling-closing-app-1', '--format', '{{.State.StartedAt}}'])).stdout.trim();
  await execute('/opt/homebrew/bin/docker', ['compose', '-p', 'xling-closing', '-f', 'docker-compose.yml', '-f', 'scripts/closing/compose.override.yml', 'restart', 'app']);
  for (let attempt = 0; attempt < 60; attempt++) {
    try { if ((await user.request.get(base + '/actuator/health')).ok()) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  result.afterRestartStartedAt = (await execute('/opt/homebrew/bin/docker', ['inspect', 'xling-closing-app-1', '--format', '{{.State.StartedAt}}'])).stdout.trim();
  assert.notEqual(result.beforeRestartStartedAt, result.afterRestartStartedAt);
  result.pendingAfterRestart = await user.evaluate(async sessionId => {
    const { api } = await import('/app.js');
    return await (await api('/api/sessions/' + sessionId)).json();
  }, result.sessionId);
  assert.equal(result.pendingAfterRestart.pendingReview, true);
  save();
  await login(admin, 'admin', 'admin123');
  await admin.waitForSelector('#reviews .decide-approve');
  const reviews = await admin.evaluate(async () => { const { api } = await import('/app.js'); return await (await api('/api/admin/reviews')).json(); });
  const review = reviews.find(item => item.threadId === result.sessionId);
  assert(review);
  result.reviewId = review.reviewId;
  const card = admin.locator('#reviews .review-item').filter({ hasText: `#${review.reviewId} ·` });
  await card.locator('.decide-approve').click();
  await card.locator('.review-note-row input').fill('工程验证：授权辅助回复；不代表风险解除或现实专业服务已经完成。');
  const decisionPromise = admin.waitForResponse(r => r.url().endsWith(`/reviews/${review.reviewId}/decision`) && r.request().method() === 'POST');
  await card.locator('.review-note-row button').click();
  const decided = await decisionPromise;
  assert(decided.ok());
  result.decision = await decided.json();
  await user.waitForFunction(() => !document.querySelector('#messageInput').disabled, null, { timeout: 180000 });
  result.resumedUI = true;
  result.afterDecisionConversation = await user.evaluate(async sessionId => { const { api } = await import('/app.js'); return await (await api('/api/sessions/' + sessionId)).json(); }, result.sessionId);
  assert.equal(result.afterDecisionConversation.pendingReview, false);
  result.repeatDecision = await admin.evaluate(async reviewId => {
    const { state } = await import('/app.js');
    const response = await fetch(`/api/admin/reviews/${reviewId}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.auth.token}` }, body: JSON.stringify({ decision: 'approve', note: '重复请求验证' }) });
    return { status: response.status, body: await response.json() };
  }, review.reviewId);
  const repeated = await user.evaluate(async sessionId => { const { api } = await import('/app.js'); return await (await api('/api/sessions/' + sessionId)).json(); }, result.sessionId);
  assert.equal(repeated.messages.length, result.afterDecisionConversation.messages.length);
  await user.screenshot({ path: resolve(out, 'after-restart-decision.png'), fullPage: true });
  await admin.check('#reviewShowAll');
  await admin.screenshot({ path: resolve(out, 'decision-record.png'), fullPage: true });
  result.status = 'completed';
} catch (error) {
  result.status = 'failed';
  result.error = { type: error.name, message: error.message };
  await user.screenshot({ path: resolve(out, 'failure-user.png'), fullPage: true });
  await admin.screenshot({ path: resolve(out, 'failure-admin.png'), fullPage: true });
  throw error;
} finally {
  await userContext.close();
  await adminContext.close();
  result.videos = [await user.video().path(), await admin.video().path()];
  save();
  await browser.close();
  console.log(JSON.stringify({ status: result.status, sessionId: result.sessionId, waitingUI: result.waitingUI, resumedUI: result.resumedUI, videos: result.videos }));
}
