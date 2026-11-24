/** AU checkout through a caller-supplied, documented in-app-browser Tab.
 * No Chrome launch, CDP discovery, cookie export, PAN/CVV access, or payment API calls.
 */
import {open, readFile, writeFile, rename, mkdir, unlink} from 'node:fs/promises';
import {resolve} from 'node:path';

export const TARGET = Object.freeze({
  productId: 'lopmi7nqah', name: 'Scuba Evolve Classic Full-Zip Hoodie',
  color: 'Lavender Rock', colorId: '77631', colorGroup: 'LAVR',
  size: 'US XS (AU 6-8)', quantity: 1, last4: '0000', currency: 'AUD',
});
const BASE = 'https://www.lululemon.com.au';
const TERMS = 'By checking this box, I agree to the Terms and Conditions of this sale. *';
const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
export class CheckoutStop extends Error {
  constructor(stage, message) { super(message); this.stage = stage; }
}
const stop = (stage, message) => { throw new CheckoutStop(stage, message); };

export const lavenderFamily = c => /^lavender rock(\s*\/\s*[^/]+)?$/i.test(clean(c?.name))
  && clean(c?.colorGroup).toUpperCase().split('/')[0].trim() === 'LAVR' && /^\d+$/.test(String(c?.id));
export function validateCandidate(c, now = Date.now()) {
  const colorOk = TARGET.colorGroup === 'LAVR' && TARGET.color === 'Lavender Rock'
    ? lavenderFamily(c?.color)
    : (c?.color?.name === TARGET.color && String(c?.color?.id).replace(/^0+/, '') === TARGET.colorId
       && c?.color?.colorGroup === TARGET.colorGroup);
  if (!c || c.product_id !== TARGET.productId || c.product_name !== TARGET.name
      || c.us_size !== 'XS' || c.au_size !== '6-8' || c.quantity !== 1
      || !colorOk || !/^\d+$/.test(String(c.sku)))
    stop('candidate_mismatch', '候选必须是 Classic / Lavender Rock / US XS / 1 件');
  const age = now - Date.parse(c.checked_at_utc);
  if (!Number.isFinite(age) || age < 0 || age > 120000)
    stop('stale_inventory', '库存证据超过两分钟或时间无效，请重新查官方接口');
  if (c.is_available !== true || c.stock_source !== 'availability_endpoint'
      || c.is_final_sale !== false || c.price?.currencyCode !== 'AUD'
      || !Number.isFinite(Number(c.unit_price)) || Number(c.unit_price) <= 0)
    stop('unverified_candidate', '库存、价格或非 Final Sale 证据不完整');
  return c;
}

export function validateMerchandise(s) {
  if (s.names.length !== 1 || s.names[0] !== TARGET.name
      || s.colors.length !== 1 || s.colors[0] !== TARGET.color
      || s.sizes.length !== 1 || s.sizes[0] !== TARGET.size
      || s.quantities.length !== 1 || s.quantities[0] !== '1')
    stop('cart_mismatch', '购物袋不是唯一的 Classic / Lavender Rock / US XS / 1 件；保留原购物袋');
  if (s.finalSale) stop('final_sale', '页面显示 Final Sale');
}

export function validateReview(s, candidate, maxTotal) {
  validateCandidate(candidate);
  if (s.url !== BASE + '/en-au/shop/checkout/review') stop('wrong_page', '当前不是澳洲站 Review');
  validateMerchandise(s);
  if (s.cards.length !== 1 || !/^Master\s*Card\s*[·•●*xX]{2,}\s*0000$/i.test(s.cards[0]))
    stop('wrong_card', '付款摘要必须仅包含 Mastercard 尾号 0000');
  if (s.giftCards) stop('mixed_payment', '订单包含其他支付方式');
  const totals = s.totals.map(t => /^A\$\d[\d,]*(?:\.\d{2})?$/.test(t)
    ? Number(t.slice(2).replaceAll(',', '')) : NaN);
  if (!totals.length || totals.some(t => !Number.isFinite(t) || t <= 0 || t !== totals[0])
      || !Number.isFinite(maxTotal) || maxTotal <= 0 || totals[0] > maxTotal
      || totals[0] < Number(candidate.unit_price))
    stop('price_mismatch', '订单含运费总价不明确、发生冲突或超过本次上限');
  if (s.placeOrderCount !== 1 || !s.placeOrderEnabled || s.termsCount !== 1)
    stop('review_not_ready', '提交按钮或销售条款状态异常');
  return {total: totals[0], currency: 'AUD', cardLast4: '0000'};
}

async function assertSite(tab) {
  const url = new URL(await tab.url());
  if (url.origin !== BASE) stop('wrong_site', '浏览器离开 lululemon 澳洲站');
}
async function click(tab, locator, beforeAction = async()=>{}) {
  await assertSite(tab);
  if (await locator.count() !== 1) stop('ambiguous_control', '页面按钮不唯一');
  await beforeAction();
  await locator.click();
}

// One DOM read, limited to merchandise, totals, masked payment summary and checkbox state.
// Never read input values, storage, frontend state, hidden credentials or payment tokens.
export async function readVisibleState(tab) {
  const url = await tab.url();
  const data = await tab.playwright.evaluate(() => {
    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const tidy = text => (text ?? '').replace(/\s+/g, ' ').trim();
    const nodes = id => Array.from(document.querySelectorAll(`[data-testid="${id}"]`))
      .filter(el => visible(el) && !el.closest('[data-testid="bag-saved-items"]'));
    const texts = id => nodes(id).map(el => tidy(el.textContent));
    const totalLabels = Array.from(document.querySelectorAll('[id="order-summary-total-gst-label-id"]')).filter(visible);
    const totals = totalLabels.map(el => tidy(el.nextElementSibling?.textContent));
    const quantities = texts('quantity-value');
    const buttons = nodes('place-order-button');
    const terms = Array.from(document.querySelectorAll('#review-terms-consent-checkbox')).filter(visible);
    return {names:texts('bag-product-name'), colors:texts('bag-product-color'),
      sizes:texts('bag-product-size-value'),
      quantities:quantities.length ? quantities : texts('bag-item-cost-label-value'),
      cards:texts('payment-summary-methods'), giftCards:nodes('payment-summary-gift-cards').length > 0,
      finalSale:texts('product-tile-info').some(t => /final sale/i.test(t)), totals,
      placeOrderCount:buttons.length, placeOrderEnabled:buttons.length === 1 && !buttons[0].disabled,
      termsCount:terms.length, termsChecked:terms.length === 1 && terms[0].checked,
      emptyBag:nodes('bag-empty-container').length === 1
        || texts('mybag-empty-subheading').some(t => /^Your bag is empty\./.test(t))};
  });
  return {url, ...data};
}

export async function inspectReview(tab, beforeAction) {
  await assertSite(tab);
  const expand = tab.playwright.getByRole('button', {name:'Order Summary See more (1)', exact:true});
  if (await expand.count() === 1 && await expand.isVisible()) await click(tab, expand, beforeAction);
  return readVisibleState(tab);
}

export class Journal {
  constructor(directory) { this.dir = resolve(directory); }
  async init() { await mkdir(this.dir, {recursive:true, mode:0o700}); }
  async save(stage, details = {}) {
    await this.init();
    const data = {stage, updated_at_utc:new Date().toISOString(), ...details};
    const temp = resolve(this.dir, `status-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`);
    await writeFile(temp, JSON.stringify(data, null, 2) + '\n', {mode:0o600});
    await rename(temp, resolve(this.dir, 'status.json'));
    return data;
  }
  async claim(kind, sku) {
    if (!['cart', 'submit', 'prepare'].includes(kind) || !/^\d+$/.test(String(sku))) throw new Error('Invalid journal key');
    await this.init();
    const path = resolve(this.dir, `${kind}-${TARGET.productId}-${sku}.json`);
    let file;
    try { file = await open(path, 'wx', 0o600); }
    catch (e) { if (e.code === 'EEXIST') stop('attempt_already_recorded', `${kind} 已有尝试记录，禁止自动重试`); throw e; }
    try {
      await file.writeFile(JSON.stringify({kind, sku:String(sku), at:new Date().toISOString()}));
      await file.sync();
    } finally { await file.close(); }
  }
  async receipt(candidate) {
    await this.init();
    await writeFile(resolve(this.dir, 'verified-cart.json'), JSON.stringify({
      sku:String(candidate.sku), productId:TARGET.productId, createdAt:Date.now(),
    }), {mode:0o600});
  }
  async assertReceipt(candidate) {
    let receipt;
    try { receipt = JSON.parse(await readFile(resolve(this.dir, 'verified-cart.json'), 'utf8')); }
    catch { stop('missing_cart_receipt', '没有本程序核对 PDP SKU 后的购物袋记录'); }
    if (receipt.sku !== String(candidate.sku) || receipt.productId !== TARGET.productId
        || Date.now() - receipt.createdAt < 0 || Date.now() - receipt.createdAt > 4 * 3600000)
      stop('stale_cart_receipt', '购物袋核对记录不符或已过期');
  }
  async exclusive(fn) {
    await this.init();
    const path = resolve(this.dir, 'running.lock');
    let lock;
    try { lock = await open(path, 'wx', 0o600); }
    catch (e) { if (e.code === 'EEXIST') stop('runner_locked', '已有执行器或未清理的中断记录'); throw e; }
    try { return await fn(); }
    finally { await lock.close(); await unlink(path); }
  }
}

async function selectRadio(tab, locator, beforeAction = async()=>{}) {
  await locator.waitFor({state:'visible', timeout:15000});
  if (await locator.count() !== 1) stop('ambiguous_option', '颜色或尺码选项不唯一');
  if (!await locator.evaluate(el => el.checked)) {
    await beforeAction();
    await assertSite(tab);
    await locator.press('Space');
  }
  if (!await locator.evaluate(el => el.checked)) stop('option_not_selected', '颜色或尺码未选中');
}

async function dismissLocation(tab, beforeAction) {
  const button = tab.playwright.getByRole('button', {name:'Stay in Australia', exact:true});
  if (await button.count() === 1 && await button.isVisible()) await click(tab, button, beforeAction);
}

export async function prepareCheckout(tab, candidate, {journal, shipping, resume = false, stopAt = 'review',
  deadlineAt = Infinity, beforeAction = async()=>{}, deferShipping = false}) {
  const guard = async () => {
    await beforeAction();
    if (Date.now() >= deadlineAt) stop('window_finished', '执行窗口已结束，停止结账操作');
  };
  const go = async url => { await guard(); await tab.goto(url); };
  const act = locator => click(tab, locator, guard);
  await guard();
  if (!['payment','review'].includes(stopAt)) stop('invalid_stop', '结账停止位置无效');
  validateCandidate(candidate);
  await assertSite(tab);
  // A saved session is reused. Never launch another browser or rewrite the user's address.
  if (resume && (await tab.url()).endsWith('/checkout/review')) {
    if (stopAt === 'payment') stop('already_past_payment', '当前已经越过填卡页，请本人核对');
    await journal.assertReceipt(candidate);
    return journal.save('ready_for_review_check');
  }
  await go(BASE + '/en-au/home');
  await dismissLocation(tab,guard);
  const accountLink = tab.playwright.getByRole('link', {name:'My Account (En-AU)', exact:true});
  if (await accountLink.count() !== 1 || !await accountLink.isVisible()) {
    // Mobile navigation can hide My Account. A rendered account Profile confirms the session.
    await go(BASE + '/en-au/account/profile');
    try { await tab.playwright.getByRole('heading', {name:'Profile', exact:true}).waitFor({timeout:15000}); }
    catch { stop('login_required', '内置浏览器未确认登录，请先本人登录澳洲站'); }
  }
  await go(BASE + '/en-au/shop/mybag');
  await dismissLocation(tab,guard);
  await tab.playwright.getByRole('heading', {name:'Bag', exact:true}).waitFor({timeout:20000});
  let bag = await readVisibleState(tab);
  if (bag.emptyBag) {
    if (resume) stop('empty_bag_on_resume', '恢复时购物袋为空，禁止自动重新加购');
    // Candidate identity was validated against the official API; still verify the rendered selections/SKU.
    await go(BASE + '/en-au/p/scuba-evolve-classic-full-zip-hoodie/' + TARGET.productId
      + '?color=' + candidate.color.id + '&skuId=' + candidate.sku);
    await dismissLocation(tab,guard);
    await tab.playwright.getByRole('heading', {name:TARGET.name, exact:true}).waitFor({timeout:20000});
    await selectRadio(tab, tab.playwright.getByRole('radio', {name:TARGET.color, exact:true}),guard);
    // The initial HTML can show another colour until React finishes applying the URL.
    // Wait for the selected colour's rendered gallery before choosing a size.
    if (await tab.playwright.getByRole('region',{name:'Product media carousel',exact:true}).count())
      await tab.playwright.getByRole('img',{name:TARGET.color,exact:true}).first().waitFor({state:'visible',timeout:15000});
    await selectRadio(tab, tab.playwright.getByTestId('size-selector-alpine').getByRole('radio',
      {name:'Extra small, US, AU 6 to 8', exact:true}),guard);
    const skuText=tab.playwright.getByText(new RegExp('^SKU\\s*[-:]?\\s*' + candidate.sku + '$', 'i'));
    try { await skuText.waitFor({timeout:15000}); }
    catch(error) {
      // Hydration can reset a size selected on the initial server-rendered page.
      // Retry only this non-purchasing selection, once, while colour is still exact.
      const size=tab.playwright.getByTestId('size-selector-alpine').getByRole('radio',
        {name:'Extra small, US, AU 6 to 8',exact:true});
      if (!await tab.playwright.getByRole('radio',{name:TARGET.color,exact:true}).evaluate(el=>el.checked)
          || await size.evaluate(el=>el.checked)) throw error;
      await selectRadio(tab,size,guard);
      await skuText.waitFor({timeout:15000});
    }
    if (!await tab.playwright.getByRole('radio',{name:TARGET.color,exact:true}).evaluate(el=>el.checked)
        || !await tab.playwright.getByTestId('size-selector-alpine').getByRole('radio',
          {name:'Extra small, US, AU 6 to 8',exact:true}).evaluate(el=>el.checked))
      stop('option_not_selected','加购前颜色或尺码发生变化，停止');
    validateCandidate(candidate);
    await guard();
    await journal.claim('cart', candidate.sku);
    await act(tab.playwright.getByRole('button', {name:/^Add to bag(?:\s+A\$[\d,.]+)?$/i}));
    await tab.playwright.getByText(/^Added to Bag/i).first().waitFor({timeout:20000});
    await go(BASE + '/en-au/shop/mybag');
    await tab.playwright.getByRole('heading', {name:'Bag', exact:true}).waitFor({timeout:20000});
    bag = await readVisibleState(tab);
    validateMerchandise(bag);
    await journal.receipt(candidate);
  } else {
    validateMerchandise(bag);
    await journal.assertReceipt(candidate);
  }
  await act(tab.playwright.getByTestId('bag-proceed-checkout-button'));
  await tab.playwright.waitForURL('**/shop/checkout**', {timeout:20000});
  if ((await tab.url()).endsWith('/shop/checkout')) {
    const email = tab.playwright.getByRole('textbox', {name:'Email (required)', exact:true});
    if (shipping?.email) { await guard(); await email.fill(shipping.email); }
    const signup = tab.playwright.getByRole('checkbox', {name:'Sign me up for lululemon marketing and promotional emails.', exact:true});
    if (await signup.count() === 1 && await signup.evaluate(el => el.checked)) { await guard(); await signup.press('Space'); }
    await act(tab.playwright.getByTestId('contact-continue-button'));
    await tab.playwright.waitForURL('**/checkout/shipping', {timeout:20000});
  }
  if ((await tab.url()).endsWith('/checkout/shipping')) {
    // Only reuse the already selected address. Do not fill/reorder address fields.
    if (!shipping?.selectedAddressLabel) stop('needs_shipping_setup', '需要事先核对已选收货地址');
    const address = tab.playwright.getByRole('radio', {name:shipping.selectedAddressLabel, exact:true});
    await address.waitFor({state:'visible',timeout:15000});
    if (await address.count() !== 1 || !await address.evaluate(el => el.checked))
      stop('shipping_mismatch', '当前选中的地址与已核对地址不符，需要本人处理');
    const leave = tab.playwright.getByRole('checkbox', {name:'Authority to leave package without signature', exact:true});
    if (await leave.count() !== 1) stop('shipping_mismatch', '无法核对无需签名留件选项');
    if (await leave.evaluate(el => el.checked)) { await guard(); await leave.press('Space'); }
    if (deferShipping) {
      await guard();
      return journal.save('ready_for_native_shipping', {sku:String(candidate.sku),submitted:false});
    }
    await act(tab.playwright.getByRole('button', {name:'Continue', exact:true}));
    await tab.playwright.waitForURL('**/checkout/payment', {timeout:20000});
  }
  if ((await tab.url()).endsWith('/checkout/payment')) {
    if (stopAt === 'payment') {
      await tab.playwright.getByRole('heading', {name:'Payment', exact:true}).waitFor({timeout:15000});
      await guard();
      validateMerchandise(await inspectReview(tab,guard));
      await journal.assertReceipt(candidate);
      return journal.save('ready_for_manual_card', {productId:TARGET.productId,sku:String(candidate.sku),
        name:TARGET.name,color:TARGET.color,size:TARGET.size,quantity:1,submitted:false,
        message:'已到填卡页，等待本人操作；未选择银行卡、未继续到Review、未提交订单。'});
    }
    // No full-card reads/fills. A card not saved with the merchant needs manual preparation.
    const cards = await tab.playwright.locator('[data-testid^="saved-card-details-"]').all();
    const matching = [];
    for (const card of cards) if (/^Master\s*Card\s*[·•●*xX]{2,}\s*0000$/i.test(clean(await card.innerText()))) matching.push(card);
    if (matching.length !== 1) return journal.save('needs_payment_setup', {cardLast4:'0000',
      message:'请本人在官网填卡并 Continue；程序不读取卡号或安全码，也不自动保存卡。'});
    const id = (await matching[0].getAttribute('data-testid')).replace('saved-card-details-', '');
    await selectRadio(tab, tab.playwright.getByTestId('saved-card-radio-' + id));
    await click(tab, tab.playwright.getByTestId('saved-card-continue-button'));
    await tab.playwright.waitForURL('**/checkout/review', {timeout:20000});
  }
  if (stopAt === 'payment') stop('unexpected_checkout_state', '未停在填卡页，停止所有后续结账动作');
  if (!(await tab.url()).endsWith('/checkout/review')) stop('unexpected_checkout_state', '结账页面状态未知');
  return journal.save('ready_for_review_check');
}

export async function observeSubmission(tab) {
  const pw = tab.playwright;
  const failed = pw.getByRole('heading', {name:'Authentication failed', exact:true});
  if (await failed.count() === 1 && await failed.isVisible()) return {stage:'authentication_failed', retry:false};
  const modal = pw.getByRole('dialog', {name:'Verify your payment', exact:true});
  if (await modal.count() === 1 && await modal.isVisible()) {
    // The outer accessibility tree may be blank: inspect the documented 3DS iframe.
    const frame = pw.frameLocator('iframe[name="threeDSIframe"]');
    const prompt = frame.getByText('For security reasons, you will be authenticated with Bybit App. Open Bybit App directly from your phone and verify the payment.', {exact:true});
    if (await prompt.count() === 1) {
      const payment = frame.getByText(/^Your payment of /);
      const text = await payment.count() === 1 ? clean(await payment.innerText()) : '';
      const match = /^Your payment of \$([\d,.]+) to (.+)\.$/.exec(text);
      return {stage:'awaiting_bybit_authorization', amount:match ? Number(match[1].replaceAll(',', '')) : null,
        merchant:match?.[2] ?? null, retry:false, message:'现在打开 Bybit App 手动授权；程序已停止点击。'};
    }
    return {stage:'verification_loading', retry:false};
  }
  if ((await tab.url()) !== BASE + '/en-au/shop/checkout/review')
    return {stage:'submission_outcome_unknown', retry:false, message:'页面已跳转；需核对订单结果，不自动重试。'};
  return {stage:'submission_pending', retry:false};
}

export async function submitReview(tab, candidate, {
  journal, maxTotal, authorization, dryRun = true, timeoutMs = 30000, notify = async () => {},
  deadlineAt = Infinity,
}) {
  return journal.exclusive(async () => {
    const snapshot = await inspectReview(tab);
    const verified = validateReview(snapshot, candidate, maxTotal);
    if (dryRun) return journal.save('ready_before_place_order', {...verified, submitted:false});
    if (!authorization || authorization.productId !== TARGET.productId
        || authorization.sku !== String(candidate.sku) || authorization.maxTotal !== maxTotal
        || authorization.acceptSalesTerms !== true || authorization.submitOnce !== true
        || authorization.cardLast4 !== TARGET.last4)
      stop('authorization_required', '缺少本次精确商品、金额、银行卡和销售条款的提交授权');
    await journal.assertReceipt(candidate);
    const checkbox = tab.playwright.getByRole('checkbox', {name:TERMS, exact:true});
    if (!snapshot.termsChecked) await checkbox.press('Space');
    const final = await readVisibleState(tab);
    validateReview(final, candidate, maxTotal);
    if (!final.termsChecked) stop('terms_not_checked', '条款勾选未生效，未提交订单');
    if (Date.now() >= deadlineAt) stop('window_finished', '时间窗已结束，未提交订单');
    // Durable claim BEFORE the click. A timeout/crash must never trigger a second order.
    await journal.claim('submit', candidate.sku);
    await journal.save('submission_attempted', {...verified, sku:String(candidate.sku), retry:false});
    try { await click(tab, tab.playwright.getByRole('button', {name:'Place order', exact:true})); }
    catch { return journal.save('submission_outcome_unknown', {retry:false, message:'点击结果不明，请核对官网和 Bybit；禁止自动重试。'}); }
    const deadline = Date.now() + timeoutMs;
    let outcome;
    try {
      do {
        outcome = await observeSubmission(tab);
        if (!['submission_pending', 'verification_loading'].includes(outcome.stage)) break;
        // Short bounded observation polling; no further checkout/payment actions.
        await new Promise(resolve => setTimeout(resolve, 250));
      } while (Date.now() < deadline);
    } catch {
      outcome = {stage:'submission_outcome_unknown', retry:false, message:'提交后无法读取验证结果，需人工核对；禁止自动重试。'};
    }
    if (outcome.stage === 'awaiting_bybit_authorization' && outcome.amount !== verified.total)
      outcome = {stage:'verification_amount_mismatch', retry:false, message:'Bybit 验证金额与订单不符，请勿授权并人工核对。'};
    if (['submission_pending', 'verification_loading'].includes(outcome.stage))
      outcome = {stage:'submission_outcome_unknown', retry:false, message:'提交后等待超时，结果未知；禁止自动重试。'};
    const result = await journal.save(outcome.stage, {...outcome, ...verified});
    await tab.markHandoff?.();
    await notify(result);
    return result;
  });
}

export async function runCheckout(tab, candidate, options) {
  await tab.markHandoff?.();
  // This caller holds the cart lease; submitReview has a separate one to avoid concurrent submissions.
  const prepared = await options.journal.exclusive(() => prepareCheckout(tab, candidate, options));
  if (prepared.stage !== 'ready_for_review_check') return prepared;
  // Refresh inventory after contact/shipping/manual delays, just before final checks.
  if (typeof options.refreshCandidate !== 'function') stop('fresh_inventory_required', '提交前必须重新查询库存');
  const fresh = validateCandidate(await options.refreshCandidate());
  if (String(fresh.sku) !== String(candidate.sku) || Number(fresh.unit_price) !== Number(candidate.unit_price))
    stop('candidate_changed', '复查发现 SKU 或价格变化');
  return submitReview(tab, fresh, options);
}
