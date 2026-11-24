/** Bridge read-only Python product checks to the existing IAB Tab.
 * Import from the supported browser runtime, pass its Tab explicitly.
 * Plain `node` cannot attach to the in-app browser; no hidden browser endpoints are used.
 */
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';
import {Journal, runCheckout, validateCandidate, CheckoutStop} from './iab_checkout.mjs';
const exec = promisify(execFile);
const ROOT = dirname(fileURLToPath(import.meta.url));
const DEFAULT_WINDOW = {start:'2026-11-17T00:50:00+08:00', end:'2026-11-17T06:00:00+08:00'};

export async function assertNotPaused(path = resolve(ROOT,'.private/availability-notifier/user-pause.json')) {
  let state;
  try { state=JSON.parse(await readFile(path,'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return; throw new CheckoutStop('paused_by_user','暂停标记无法核验，停止'); }
  if (state.paused !== false) throw new CheckoutStop('paused_by_user','用户已暂停监控和结账准备');
}

export async function checkCandidate() {
  // Uses only the two verified public product/inventory GET endpoints. No checkout HTTP.
  const {stdout} = await exec(resolve(ROOT, '.venv/bin/python'), ['-c',
    'import json; from lululemon_monitor import check; from checkout_guards import select_candidate; r=check(us_size="XS"); failed=r.get("has_request_error") or r["status"]=="query_failed"; delays=[str(x.get("response_headers",{}).get("retry-after","")) for x in r.get("requests",[])]; print(json.dumps({"catalog_status":r["status"],"request_failed":bool(failed),"retry_after_seconds":max([int(x) for x in delays if x.isdigit()]+[0]),"candidate":None if failed else select_candidate(r)}))'],
    {cwd:ROOT, timeout:35000, maxBuffer:1024 * 1024});
  const data = JSON.parse(stdout);
  if (data.candidate) validateCandidate(data.candidate);
  return data;
}

export async function notifyLocal(result) {
  const message = result.stage === 'awaiting_bybit_authorization'
    ? `现在打开 Bybit App：A$${result.total}，卡尾号 0000。请在银行页面倒计时内处理。`
    : `lululemon：${result.stage}，请查看浏览器。`;
  // Arguments are passed separately, never shell-interpolated.
  await exec('osascript', ['-e', 'on run argv\ndisplay notification (item 1 of argv) with title "lululemon" sound name "Glass"\nend run', message]);
}

export async function runOnce(tab, {dryRun = true, maxTotal, authorization, resume = false,
  stopAt = 'review', expectedSku, journalDirectory = resolve(ROOT, '.private/iab-run'),
  deadlineAt = Infinity,
  settingsPath = resolve(ROOT, '.private/iab-settings.json')} = {}) {
  const journal = new Journal(journalDirectory);
  try {
    await assertNotPaused();
    if (Date.now() >= deadlineAt) return journal.save('window_finished');
    const checked = await checkCandidate();
    if (checked.request_failed) return journal.save('query_failed', {retry_after_seconds:checked.retry_after_seconds});
    if (!checked.candidate) return journal.save('monitoring', {catalog_status:checked.catalog_status});
    if (expectedSku && String(checked.candidate.sku) !== String(expectedSku))
      throw new CheckoutStop('candidate_changed', '复查SKU与触发通知的精确SKU不一致，停止');
    if (Date.now() >= deadlineAt) return journal.save('window_finished');
    const settings = JSON.parse(await readFile(settingsPath, 'utf8'));
    return await runCheckout(tab, checked.candidate, {journal, shipping:settings.shipping,
      maxTotal, authorization, dryRun, resume, stopAt, deadlineAt, beforeAction:assertNotPaused, notify:notifyLocal,
      refreshCandidate:async () => (await checkCandidate()).candidate});
  } catch (error) {
    // Never print raw browser errors: they might contain entered values.
    const result = await journal.save(error instanceof CheckoutStop ? error.stage : 'needs_attention',
      {message:error instanceof CheckoutStop ? error.message : `执行中断（${error.name}）；请查看浏览器，不自动重试。`});
    try { await notifyLocal(result); } catch { /* status is still retained */ }
    return result;
  } finally { await tab.markHandoff?.(); }
}

/** The event handler can only reach manual card entry. No live/payment options are exposed. */
export async function preparePaymentOnce(tab, {expectedSku, deadlineAt,
  settingsPath = resolve(ROOT, '.private/iab-settings.json')} = {}) {
  if (!/^\d+$/.test(String(expectedSku)) || !Number.isFinite(deadlineAt))
    throw new CheckoutStop('invalid_handoff', '必须提供已确认的真实SKU及执行截止时间');
  const journalDirectory = resolve(ROOT, '.private/iab-payment-run');
  const journal = new Journal(journalDirectory);
  await assertNotPaused();
  if (deadlineAt > Date.parse(DEFAULT_WINDOW.end))
    throw new CheckoutStop('invalid_handoff','截止时间超出已授权的本次窗口');
  if (Date.now()<Date.parse(DEFAULT_WINDOW.start)) return journal.save('waiting_for_window');
  if (Date.now()>=deadlineAt) return journal.save('window_finished');
  try { await journal.claim('prepare', String(expectedSku)); }
  catch (error) {
    if (error instanceof CheckoutStop) return {stage:'preparation_already_attempted',retry:false};
    throw error;
  }
  const result = await runOnce(tab, {expectedSku, deadlineAt, settingsPath, journalDirectory,
    stopAt:'payment',dryRun:true,resume:false});
  try { await notifyLocal(result); } catch { /* The durable result is authoritative. */ }
  return result;
}

/** One bounded monitoring tick, intended for a caller that keeps the IAB runtime active.
 * Persisted cooldown, >=30-second cadence, explicit Beijing time window; no hidden background task.
 */
export async function monitorTick(tab, options = {}) {
  const journal = new Journal(resolve(ROOT, '.private/iab-monitor'));
  const start = Date.parse(options.start ?? DEFAULT_WINDOW.start);
  const end = Date.parse(options.end ?? DEFAULT_WINDOW.end);
  const interval = options.intervalSeconds ?? 30;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end-start > 8*3600000
      || !Number.isFinite(interval) || interval < 30) throw new Error('Invalid bounded monitoring window');
  return journal.exclusive(async () => {
    const now = Date.now();
    if (now < start) return journal.save('scheduled', {start:new Date(start).toISOString(), end:new Date(end).toISOString()});
    if (now >= end) return journal.save('window_finished');
    let last = {};
    try { last = JSON.parse(await readFile(resolve(journal.dir, 'status.json'), 'utf8')); } catch {}
    if (last.stage === 'halted') return last;
    if (last.nextCheckAt > now) return {stage:'cooldown', nextCheckAt:last.nextCheckAt};
    // Fail closed across a crash: after dispatch, only an explicit caller may resume execution.
    await journal.save('halted', {reason:'tick_in_progress'});
    const outcome = await runOnce(tab, {...options, deadlineAt:end});
    if (['monitoring','query_failed'].includes(outcome.stage)) {
      const failures = outcome.stage === 'query_failed' ? (last.failures ?? 0) + 1 : 0;
      const delay = Math.max(Math.min(interval * 2 ** Math.min(failures,4),900), outcome.retry_after_seconds ?? 0);
      return journal.save(outcome.stage, {catalog_status:outcome.catalog_status, failures,
        nextCheckAt:Date.now()+delay*1000});
    }
    await journal.save('halted', {executionStage:outcome.stage});
    return outcome;
  });
}
