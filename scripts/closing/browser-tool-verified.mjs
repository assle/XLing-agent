import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import assert from 'node:assert/strict';
const require = createRequire('/Users/assle/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const { chromium } = require('playwright-core');
const execute = promisify(execFile);
const out = resolve('.scratch/closing-evidence/tool-reliability-verified');
mkdirSync(out, { recursive: true });
const result = { status: 'running', scope: 'real public high-risk report, durable queue, Excel and local SMTP; first DATA deliberately rejected; synthetic messages only; no onward email delivery', observedAtUTC: new Date().toISOString(), observations: [] };
const save = () => writeFileSync(resolve(out,'result.json'), JSON.stringify(result,null,2)+'\n');
const browser = await chromium.launch({ channel:'msedge',headless:true });
const userContext = await browser.newContext();
const adminContext = await browser.newContext({viewport:{width:1400,height:980},recordVideo:{dir:out}});
const user = await userContext.newPage();
const admin = await adminContext.newPage();
async function login(page, name, password) {
  await page.goto('http://127.0.0.1:18381');
  await page.fill('#username',name); await page.fill('#password',password);
  const ready=page.waitForResponse(r=>r.url().endsWith(name==='admin'?'/api/admin/reviews':'/api/sessions'));
  await page.click('#loginForm button[type="submit"]'); await (await ready).finished();
  await page.waitForSelector('#loginForm[hidden]',{state:'attached'});
}
async function snapshot() {
  return admin.evaluate(async reportId => {
    const {api}=await import('/app.js');
    const read=async path=>await(await api(path)).json();
    return {observedAtUTC:new Date().toISOString(),
      jobs:(await read('/api/admin/tool-jobs')).filter(x=>x.reportId===reportId),
      alerts:(await read('/api/admin/alerts')).filter(x=>x.reportId===reportId),
      excel:(await read('/api/admin/excel-records')).filter(x=>x.reportId===reportId)};
  },result.reportId);
}
try {
  await login(admin,'admin','admin123'); await login(user,'student','student123');
  await user.click('#newSession');
  await user.fill('#messageInput','工程隔离演示消息：我已经准备自杀，计划今晚就行动，身边已经备好工具。');
  const ready=user.waitForResponse(r=>r.url().endsWith('/api/chat/stream'));
  await user.click('#sendButton'); const response=await ready; assert(response.ok()); await response.finished();
  result.sessionId=await user.evaluate(async()=> (await import('/app.js')).state.sessionId);
  const reviews=await admin.evaluate(async()=>{const {api}=await import('/app.js'); return await(await api('/api/admin/reviews')).json();});
  const review=reviews.find(x=>x.threadId===result.sessionId); assert(review);
  result.reportId=review.reportId; result.reviewId=review.reviewId;
  let failed=false, succeeded=false;
  for(let i=0;i<90;i++) {
    const state=await snapshot(); result.observations.push(state); save();
    if(!failed && state.alerts.some(x=>x.status==='FAILED')) {
      failed=true; await admin.click('#refreshAdmin');
      await admin.waitForFunction(()=>document.querySelector('#toolJobs').textContent.includes('等待重试'));
      await admin.locator('#toolJobs').scrollIntoViewIfNeeded();
      await admin.screenshot({path:resolve(out,'retry-pending.png'),fullPage:true});
    }
    if(state.jobs.length===2 && state.jobs.every(x=>x.status==='SUCCESS')) {succeeded=true; break;}
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  assert(failed && succeeded,'Must observe actual failure followed by automatic success');
  const duplicate=await execute('/opt/homebrew/bin/docker',['exec','-e','PYTHONPATH=/app','xling-closing-app-1','python','/tmp/closing-tool-idempotency.py',String(result.reportId)]);
  result.idempotency=JSON.parse(duplicate.stdout.split('\n').find(x=>x.startsWith('CLOSING_RESULT=')).slice(15));
  result.smtpEvents=JSON.parse(readFileSync(resolve('.scratch/closing-evidence/tool-reliability/smtp-events.json'),'utf8'));
  assert.equal(result.smtpEvents.filter(x=>x.status==='accepted-locally').length,1);
  result.finalState=await snapshot();
  await admin.click('#refreshAdmin');
  await admin.waitForFunction(()=>document.querySelector('#toolJobs').textContent.includes('成功'));
  await admin.screenshot({path:resolve(out,'retry-success-and-reuse.png'),fullPage:true});
  result.testReviewClosure=await admin.evaluate(async id=>{const {api}=await import('/app.js');return await(await api(`/api/admin/reviews/${id}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:'reject',note:'本地工具可靠性演示结束；使用固定安全回复收束合成用例。'})})).json();},result.reviewId);
  result.status='completed';
} catch(error) {result.status='failed';result.error={type:error.name,message:error.message};await admin.screenshot({path:resolve(out,'failure.png'),fullPage:true});throw error;}
finally {await userContext.close();await adminContext.close();result.video=await admin.video().path();save();await browser.close();console.log(JSON.stringify({status:result.status,reportId:result.reportId,idempotency:result.idempotency,video:result.video}));}
