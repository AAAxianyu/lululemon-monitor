/** Offline Chromium contract tests. ALL browser network requests are fulfilled locally. */
import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {TARGET, Journal, validateCandidate, validateReview, readVisibleState, inspectReview,
  prepareCheckout, submitReview, observeSubmission, runCheckout} from './iab_checkout.mjs';
import {assertNotPaused} from './iab_runner.mjs';
const root = dirname(fileURLToPath(import.meta.url));
const driver = execFileSync(resolve(root, '.venv/bin/python'), ['-c',
  'import playwright; from pathlib import Path; print(Path(playwright.__file__).parent/"driver"/"package")'], {encoding:'utf8'}).trim();
const {chromium} = createRequire(import.meta.url)(driver);
const BASE = 'https://www.lululemon.com.au';
const REVIEW = BASE + '/en-au/shop/checkout/review';
let browser;
before(async () => { browser = await chromium.launch({channel:'chrome', headless:true}); });
after(async () => { await browser.close(); });

function candidate() {
  return {product_id:TARGET.productId, product_name:TARGET.name, sku:'123456789',
    color:{id:'77631', name:TARGET.color, colorGroup:'LAVR'}, us_size:'XS', au_size:'6-8', quantity:1,
    checked_at_utc:new Date().toISOString(), is_available:true, stock_source:'availability_endpoint',
    is_final_sale:false, price:{currencyCode:'AUD', listPrice:169}, unit_price:'169'};
}
function authorization(c) {
  return {productId:TARGET.productId, sku:String(c.sku), maxTotal:169, cardLast4:'0000', acceptSalesTerms:true, submitOnce:true};
}
async function fixture(t, change = {}) {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(1200);
  const dir = await mkdtemp(join(tmpdir(), 'lulu-iab-test-'));
  t.after(async () => { await context.close(); await rm(dir, {recursive:true, force:true}); });
  const state = {name:TARGET.name, color:TARGET.color, size:TARGET.size, quantity:1, total:169,
    card:'MasterCard ···· 0000', adds:0, submissions:0, authActions:0, mode:'challenge',
    initialBag:true, savedCard:true, ...change};
  const tile = () => `<article><div data-testid="product-tile-info">Free 30-day returns</div>
    <span data-testid="bag-product-name">${state.name}</span><span data-testid="bag-product-color">${state.color}</span>
    <span data-testid="bag-product-size-value">${state.size}</span><span data-testid="bag-item-cost-label-value">${state.quantity}</span></article>`;
  const totals = () => `<dl><dt id="order-summary-total-gst-label-id">Total (GST Included)</dt><dd>A$${state.total.toFixed(2)}</dd></dl>`;
  await context.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/test-place-order') {
      state.submissions++;
      return route.fulfill({json:{ok:true}});
    }
    if (path === '/test-add') { state.adds++; state.initialBag = true; return route.fulfill({json:{ok:true}}); }
    if (path === '/test-authorize') { state.authActions++; return route.fulfill({json:{ok:true}}); }
    if (path === '/test-challenge') return route.fulfill({contentType:'text/html; charset=utf-8', body:`
      <p>For security reasons, you will be authenticated with Bybit App. Open Bybit App directly from your phone and verify the payment.</p>
      <p>Your payment of $${(state.challengeAmount ?? 169).toFixed(2)} to Lululemon Athletica On.</p>
      <button onclick="fetch('/test-authorize')">Receive Email</button><button onclick="fetch('/test-authorize')">Cancel payment</button>`});
    let html;
    if (path.endsWith('/home')) {
      html = '<a href="/en-au/account/dashboard" aria-label="My Account (En-AU)">My Account</a>';
    } else if (path.endsWith('/review')) {
      const result = state.mode === 'challenge'
        ? `<div role="dialog" aria-label="Verify your payment"><h2>Verify your payment</h2><iframe name="threeDSIframe" src="/test-challenge"></iframe></div>`
        : state.mode === 'failed' ? '<h2>Authentication failed</h2>' : '';
      html = tile()+totals()+`<span data-testid="payment-summary-methods">${state.card}</span>
        <div style="display:none">${tile()}</div>
        <label><input id="review-terms-consent-checkbox" type="checkbox">By checking this box, I agree to the Terms and Conditions of this sale. *</label>
        <button data-testid="place-order-button" onclick="this.disabled=true;fetch('/test-place-order').then(()=>document.querySelector('#result').innerHTML=${JSON.stringify(result).replaceAll('"', '&quot;')})">Place order</button><div id="result"></div>`;
    } else if (path.endsWith('/shop/mybag')) {
      html = '<h1>Bag</h1>' + (state.initialBag ? tile()+totals()+
        `<button data-testid="bag-proceed-checkout-button" onclick="location.href='/en-au/shop/checkout'">Checkout</button>`
        : '<p data-testid="mybag-empty-subheading">Your bag is empty. Shop What\'s New</p>');
      if (state.savedItems) html += `<div data-testid="bag-saved-items"><h2>Saved Items</h2>
        <button data-testid="bag-product-name">Scuba Evolve Oversized Full-Zip Hoodie</button>
        <span data-testid="bag-product-color">Lavender Rock</span></div>`;
    } else if (path.endsWith('/search')) {
      html = `<a href="/en-au/p/scuba-evolve-classic-full-zip-hoodie/lopmi7nqah">${TARGET.name}</a>`;
    } else if (path.includes('/en-au/p/')) {
      html = `<h1>${TARGET.name}</h1><input type="radio" aria-label="Lavender Rock">
        <div data-testid="size-selector-alpine"><input type="radio" aria-label="Extra small, US, AU 6 to 8"></div>
        <p id="sku" ${state.resetSizeOnce?'hidden':''}>SKU - 123456789</p><button onclick="fetch('/test-add').then(()=>document.querySelector('#notice').innerHTML='<h2>Added to Bag</h2><p>Added to Bag</p>')">Add to bag A$169</button><div id="notice"></div>`;
      if(state.resetSizeOnce) html+=`<script>let attempts=0;document.querySelector('[aria-label="Extra small, US, AU 6 to 8"]').addEventListener('change',event=>{if(++attempts===1){setTimeout(()=>event.target.checked=false,200)}else{document.querySelector('#sku').hidden=false}})</script>`;
    } else if (path.endsWith('/shop/checkout')) {
      html = `<input type="email" aria-label="Email (required)"><button data-testid="contact-continue-button" onclick="location.href='/en-au/shop/checkout/shipping'">Continue</button>`;
    } else if (path.endsWith('/shipping')) {
      html = `<input type="radio" aria-label="Test approved address" checked>
        <input type="checkbox" aria-label="Authority to leave package without signature" checked>
        <button onclick="sessionStorage.setItem('leave',document.querySelector('input[type=checkbox]').checked);location.href='/en-au/shop/checkout/payment'">Continue</button>`;
    } else if (path.endsWith('/payment')) {
      html = '<h2>Payment</h2>' + tile() + (state.savedCard ? `<div data-testid="saved-card-details-test">${state.card}</div><input data-testid="saved-card-radio-test" type="radio">
        <button data-testid="saved-card-continue-button" onclick="location.href='/en-au/shop/checkout/review'">Continue</button>` : '<p>Enter payment details</p>');
    } else return route.fulfill({status:404, body:'Offline test: unknown route'});
    await route.fulfill({contentType:'text/html; charset=utf-8', body:html});
  });
  await page.goto(REVIEW);
  const tab = {playwright:page, url:async () => page.url(), goto:async url => {await page.goto(url)}, markHandoff:async()=>{}};
  const journal = new Journal(dir);
  const c = candidate();
  await journal.receipt(c);
  return {tab, page, state, journal, c};
}

test('rejects stale inventory, wrong colour/style/size, unavailable stock and bad price', () => {
  for (const change of [{product_name:'Scuba Evolve Oversized Full-Zip Hoodie'}, {us_size:'S'},
    {color:{id:'77631',name:'Lavender Rock',colorGroup:'PLFU'}}, {color:{id:'77631',name:'Lavender Mist/Gold',colorGroup:'LAVR/GOLD'}}, {is_available:false},
    {checked_at_utc:'2020-01-01T00:00:00Z'}, {unit_price:'NaN'}, {is_final_sale:true}])
    assert.throws(() => validateCandidate({...candidate(), ...change}));
});
test('real DOM reader ignores hidden responsive copies and sees exact totals/card', async t => {
  const {tab,c} = await fixture(t);
  const snapshot = await inspectReview(tab);
  assert.equal(snapshot.names.length, 1);
  assert.deepEqual(validateReview(snapshot,c,169), {total:169,currency:'AUD',cardLast4:'0000'});
});
test('wrong merchandise, quantity, card, price and mixed payment stop before submission', async t => {
  for (const change of [{name:'Scuba Evolve Oversized Full-Zip Hoodie'}, {size:'US S (AU 8-10)'},
    {color:'Black'}, {quantity:2}, {card:'MasterCard ···· 8888'}, {card:'PayPal 0000'}, {total:184}]) {
    const {tab,journal,c,state} = await fixture(t,change);
    await assert.rejects(submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c)}));
    assert.equal(state.submissions,0);
  }
  const {tab,page,journal,c,state} = await fixture(t);
  await page.evaluate(() => {const el=document.createElement('span');el.dataset.testid='payment-summary-gift-cards';el.textContent='Gift card';document.body.append(el);});
  await assert.rejects(submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c)}));
  assert.equal(state.submissions,0);
});
test('dry run does not accept terms or submit', async t => {
  const {tab,page,journal,c,state} = await fixture(t);
  const result = await submitReview(tab,c,{journal,maxTotal:169});
  assert.equal(result.stage,'ready_before_place_order');
  assert.equal(await page.locator('#review-terms-consent-checkbox').isChecked(),false);
  assert.equal(state.submissions,0);
});
test('missing explicit authorization and missing SKU receipt prevent a live submission', async t => {
  const {tab,journal,c,state} = await fixture(t);
  await assert.rejects(submitReview(tab,c,{journal,maxTotal:169,dryRun:false}), {stage:'authorization_required'});
  await rm(join(journal.dir,'verified-cart.json'));
  await assert.rejects(submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c)}), {stage:'missing_cart_receipt'});
  assert.equal(state.submissions,0);
});
test('one authorized submission stops at Bybit iframe without email/cancel/authorization clicks', async t => {
  const {tab,journal,c,state} = await fixture(t);
  const result = await submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c),timeoutMs:3000});
  assert.equal(result.stage,'awaiting_bybit_authorization');
  assert.equal(result.amount,169);
  assert.equal(state.submissions,1);
  assert.equal(state.authActions,0);
  assert.equal(result.retry,false);
});
test('different challenge amount is reported and never authorized', async t => {
  const {tab,journal,c,state} = await fixture(t,{challengeAmount:184});
  const result = await submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c),timeoutMs:3000});
  assert.equal(result.stage,'verification_amount_mismatch');
  assert.equal(state.submissions,1);
  assert.equal(state.authActions,0);
});
test('authentication failure is terminal, and a reload/restart cannot resubmit', async t => {
  const {tab,page,journal,c,state} = await fixture(t,{mode:'failed'});
  const opts = {journal,maxTotal:169,dryRun:false,authorization:authorization(c),timeoutMs:3000};
  assert.equal((await submitReview(tab,c,opts)).stage,'authentication_failed');
  await page.goto(REVIEW);
  const restarted = new Journal(journal.dir);
  await assert.rejects(submitReview(tab,c,{...opts,journal:restarted}), {stage:'attempt_already_recorded'});
  assert.equal(state.submissions,1);
});
test('blank verification or ambiguous submission times out without retries', async t => {
  const {tab,journal,c,state} = await fixture(t,{mode:'blank'});
  const result = await submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c),timeoutMs:300});
  assert.equal(result.stage,'submission_outcome_unknown');
  assert.equal(state.submissions,1);
});
test('concurrent durable claims allow one submit attempt', async t => {
  const {journal} = await fixture(t);
  const results = await Promise.allSettled([journal.claim('submit','123456789'),new Journal(journal.dir).claim('submit','123456789')]);
  assert.equal(results.filter(x => x.status === 'fulfilled').length,1);
});
test('an expired execution window does not submit', async t => {
  const {tab,journal,c,state} = await fixture(t);
  await assert.rejects(submitReview(tab,c,{journal,maxTotal:169,dryRun:false,authorization:authorization(c),deadlineAt:Date.now()-1}), {stage:'window_finished'});
  assert.equal(state.submissions,0);
});
test('cart to review reuses saved address/card, adds once, and never submits', async t => {
  const {tab,page,journal,c,state} = await fixture(t,{initialBag:false});
  const prepared = await prepareCheckout(tab,c,{journal,shipping:{email:'test@example.invalid',selectedAddressLabel:'Test approved address'}});
  assert.equal(prepared.stage,'ready_for_review_check');
  validateReview(await readVisibleState(tab),c,169);
  assert.equal(state.adds,1);
  assert.equal(state.submissions,0);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('leave')),'false');
});
test('unsaved card stops for manual setup without reading or filling payment fields', async t => {
  const {tab,journal,c,state} = await fixture(t,{savedCard:false});
  const result = await prepareCheckout(tab,c,{journal,shipping:{selectedAddressLabel:'Test approved address'}});
  assert.equal(result.stage,'needs_payment_setup');
  assert.equal(state.submissions,0);
});

test('payment-only mode stops before selecting even a saved wrong card, regardless of live options', async t => {
  const {tab,page,journal,c,state} = await fixture(t,{initialBag:false,card:'MasterCard ···· 8888'});
  const result = await runCheckout(tab,c,{journal,stopAt:'payment',dryRun:false,
    authorization:authorization(c),maxTotal:169,
    shipping:{email:'test@example.invalid',selectedAddressLabel:'Test approved address'},
    refreshCandidate:async()=>{throw new Error('Must not reach Review');}});
  assert.equal(result.stage,'ready_for_manual_card');
  assert.equal(page.url(),BASE+'/en-au/shop/checkout/payment');
  assert.equal(await page.getByTestId('saved-card-radio-test').isChecked(),false);
  assert.equal(state.adds,1);
  assert.equal(state.submissions,0);
  assert.equal(result.submitted,false);
});

test('payment-only mode rejects existing wrong merchandise without removing or adding items', async t => {
  const {tab,journal,c,state} = await fixture(t,{name:'Scuba Evolve Oversized Full-Zip Hoodie'});
  await assert.rejects(prepareCheckout(tab,c,{journal,stopAt:'payment'}),{stage:'cart_mismatch'});
  assert.equal(state.adds,0);
  assert.equal(state.submissions,0);
});

test('real empty-bag marker and Saved Items cannot contaminate cart or payment verification', async t => {
  const {tab,page,journal,c,state} = await fixture(t,{initialBag:false,savedItems:true});
  await page.goto(BASE+'/en-au/shop/mybag');
  const bag=await readVisibleState(tab);
  assert.equal(bag.emptyBag,true);
  assert.deepEqual(bag.names,[]);
  const outcome=await prepareCheckout(tab,c,{journal,stopAt:'payment',
    shipping:{email:'test@example.invalid',selectedAddressLabel:'Test approved address'}});
  assert.equal(outcome.stage,'ready_for_manual_card');
  assert.equal(state.adds,1);
  assert.equal(state.submissions,0);
});

test('expired or paused payment preparation stops before add-to-bag', async t => {
  const {tab,journal,c,state}=await fixture(t,{initialBag:false});
  await assert.rejects(prepareCheckout(tab,c,{journal,stopAt:'payment',deadlineAt:Date.now()-1}),
    {stage:'window_finished'});
  assert.equal(state.adds,0);
  const marker=join(journal.dir,'pause.json');
  await writeFile(marker,JSON.stringify({paused:true}));
  await assert.rejects(prepareCheckout(tab,c,{journal,stopAt:'payment',beforeAction:()=>assertNotPaused(marker)}),
    {stage:'paused_by_user'});
  assert.equal(state.adds,0);
  await writeFile(marker,'broken');
  await assert.rejects(assertNotPaused(marker),{stage:'paused_by_user'});
});

test('pause during checkout stops before the next button and never reaches payment', async t => {
  const {tab,page,journal,c,state}=await fixture(t,{initialBag:false});
  const guard=async()=>{
    if(page.url().endsWith('/checkout/shipping')) {
      const error=new Error('User paused');error.stage='paused_by_user';throw error;
    }
  };
  await assert.rejects(prepareCheckout(tab,c,{journal,stopAt:'payment',beforeAction:guard,
    shipping:{email:'test@example.invalid',selectedAddressLabel:'Test approved address'}}),{stage:'paused_by_user'});
  assert.equal(state.adds,1);
  assert.equal(page.url(),BASE+'/en-au/shop/checkout/shipping');
  assert.equal(state.submissions,0);
});

test('hydration resetting the selected size is recovered once before adding', async t => {
  const {tab,journal,c,state}=await fixture(t,{initialBag:false,resetSizeOnce:true});
  const result=await prepareCheckout(tab,c,{journal,stopAt:'payment',
    shipping:{email:'test@example.invalid',selectedAddressLabel:'Test approved address'}});
  assert.equal(result.stage,'ready_for_manual_card');
  assert.equal(state.adds,1);
  assert.equal(state.submissions,0);
});
