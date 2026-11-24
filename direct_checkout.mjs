/** Standalone payment-entry preparation. Never calls review/submission functions. */
import {readFile, mkdir, access} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {checkCandidate,assertNotPaused} from './iab_runner.mjs';
const exec=promisify(execFile), ROOT=dirname(fileURLToPath(import.meta.url));
const py=async(code,args=[]) => (await exec(resolve(ROOT,'.venv/bin/python'),['-c',code,...args],
  {cwd:ROOT,timeout:40000,maxBuffer:1024*1024})).stdout.trim();
const mode=process.argv[2], expectedSku=process.argv[3];
if(!['control','production','preflight'].includes(mode)) throw Error('Invalid mode');
const control=mode==='control';
let engine=await import('./iab_checkout.mjs');
if(control) {
  // Isolated exact control identity. Production module and monitor are unchanged.
  const source=await readFile(resolve(ROOT,'iab_checkout.mjs'),'utf8');
  const target="color: 'Lavender Rock', colorId: '77631', colorGroup: 'LAVR'";
  if(source.split(target).length!==2) throw Error('Unexpected checkout module');
  engine=await import('data:text/javascript;base64,'+Buffer.from(source.replace(target,
    "color: 'Black', colorId: '1', colorGroup: 'BLK'")).toString('base64'));
}
const runArg=process.argv.find(x=>x.startsWith('--run-id='));
const runId=runArg?.slice('--run-id='.length);
if(runId && (!control || !/^[a-z0-9-]{1,40}$/.test(runId))) throw Error('Invalid control run ID');
const directory=resolve(ROOT,control?'.private/direct-control'+(runId?'/runs/'+runId:''):'.private/direct-payment');
const journal=new engine.Journal(directory);
const started=Date.now();
let page;
try {
  await assertNotPaused();
  const endpoint=await py('from checkout_browser import launch; print(launch())');
  const driver=await py('import playwright; from pathlib import Path; print(Path(playwright.__file__).parent/"driver"/"package")');
  const {chromium}=createRequire(import.meta.url)(driver);
  const browser=await chromium.connectOverCDP(endpoint,{noDefaults:true});
  const context=browser.contexts()[0];
  const pages=context.pages().filter(p=>p.url().startsWith('https://www.lululemon.com.au/'));
  if(pages.length>1) throw new engine.CheckoutStop('ambiguous_tab','专用浏览器有多个官网标签，请保留一个');
  page=pages[0]??await context.newPage();
  page.setDefaultTimeout(15000);
  await page.bringToFront();
  if(mode==='preflight') {
    await page.goto('https://www.lululemon.com.au/en-au/account/profile');
    try {await page.getByRole('heading',{name:'Profile',exact:true}).waitFor({timeout:15000});}
    catch {throw new engine.CheckoutStop('login_required','请在专用Chrome登录，随后重新执行测试');}
    console.log(JSON.stringify(await journal.save('login_ready',{submitted:false})));
  } else {
    // Open-ended monitoring: each attempt gets its own 5-minute budget.
    const deadlineAt=Date.now()+5*60000;
    let candidate;
    if(control) candidate=JSON.parse(await py('import runpy; runpy.run_path("control_candidate.py",run_name="__main__")'));
    else candidate=(await checkCandidate()).candidate;
    engine.validateCandidate(candidate);
    if(!control) {
      // Bind the checkout engine to the exact colour the API returned (e.g. Lavender Rock/Gold).
      const source=await readFile(resolve(ROOT,'iab_checkout.mjs'),'utf8');
      const target="color: 'Lavender Rock', colorId: '77631', colorGroup: 'LAVR'";
      if(source.split(target).length!==2) throw Error('Unexpected checkout module');
      const c=candidate.color, id=String(c.id).replace(/^0+/,'');
      engine=await import('data:text/javascript;base64,'+Buffer.from(source.replace(target,
        `color: ${JSON.stringify(c.name)}, colorId: ${JSON.stringify(id)}, colorGroup: ${JSON.stringify(c.colorGroup)}`)).toString('base64'));
      engine.validateCandidate(candidate);
    }
    if((control && String(candidate.sku)!=='159402216') || (!control && String(candidate.sku)!==expectedSku))
      throw new engine.CheckoutStop('candidate_changed','实际SKU与触发SKU不符');
    const settings=JSON.parse(await readFile(resolve(ROOT,'.private/iab-settings.json'),'utf8'));
    // Existing claim is never deleted. A pre-cart login failure can be resumed only explicitly.
    if(control && process.argv.includes('--retry-before-cart')) {
      await access(resolve(directory,`prepare-lopmi7nqah-${candidate.sku}.json`));
      for(const name of [`cart-lopmi7nqah-${candidate.sku}.json`,'verified-cart.json']) {
        try {await access(resolve(directory,name));throw new engine.CheckoutStop('cart_attempt_exists','已有加购尝试，禁止重新加购');}
        catch(e) {if(e.code!=='ENOENT') throw e;}
      }
      await journal.save('explicit_control_pre_cart_retry',{submitted:false});
    } else await journal.claim('prepare',candidate.sku);
    const navigate=async url=>{
      if(url.includes('/p/scuba-evolve-classic-full-zip-hoodie/lopmi7nqah')) {
        // The observed storefront search uses client navigation; direct PDP GET can be denied.
        await page.goto('https://www.lululemon.com.au/en-au/search?Ntt=Scuba%20Evolve%20Classic');
        const link=page.getByRole('link',{name:engine.TARGET.name,exact:true})
          .filter({hasText:/^Scuba Evolve Classic Full-Zip Hoodie$/});
        await link.waitFor({timeout:20000});
        const href=await link.getAttribute('href');
        if(new URL(href,'https://www.lululemon.com.au').pathname!==
          '/en-au/p/scuba-evolve-classic-full-zip-hoodie/lopmi7nqah')
          throw new engine.CheckoutStop('wrong_product_link','官网搜索结果ID不符');
        await assertNotPaused();
        if(Date.now()>=deadlineAt) throw new engine.CheckoutStop('window_finished','执行窗口结束');
        await link.click();
        await page.waitForURL('**/p/scuba-evolve-classic-full-zip-hoodie/lopmi7nqah**');
      } else await page.goto(url);
    };
    const tab={playwright:page,url:async()=>page.url(),goto:navigate,markHandoff:async()=>{}};
    if(page.url()==='about:blank') await page.goto('https://www.lululemon.com.au/en-au/home');
    const result=await journal.exclusive(()=>engine.prepareCheckout(tab,candidate,{
      journal,shipping:settings.shipping,stopAt:'payment',deadlineAt,beforeAction:assertNotPaused,
      deferShipping:true}));
    if(result.stage==='ready_for_native_shipping') {
      // CDP-attached Chrome gets GE401001 on setShippingInfo; finish shipping in ordinary Chrome.
      // native_shipping.py restarts the same profile without debugging and stops on Payment.
      await assertNotPaused();
      const color=control?'Black':candidate.color.name;
      let out;
      try {
        out=(await exec(resolve(ROOT,'.venv/bin/python'),['native_shipping.py',color,String(deadlineAt/1000)],
          {cwd:ROOT,timeout:240000,maxBuffer:1024*1024})).stdout;
      } catch(e) { out=e.stdout??''; }
      const line=String(out).trim().split('\n').pop();
      let native; try {native=JSON.parse(line);} catch {native={stage:'native_shipping_unknown'};}
      console.log(JSON.stringify(await journal.save(native.stage==='ready_for_manual_card'?'ready_for_manual_card':'native_shipping_stopped',
        {...native,elapsed_seconds:(Date.now()-started)/1000,submitted:false,standalone:true,
         message:native.stage==='ready_for_manual_card'?'已到填卡页（普通Chrome），等待本人填卡':'已加购并停在收货页（普通Chrome已打开），请本人点 Continue 后填卡'})));
      process.exitCode=native.stage==='ready_for_manual_card'?0:1;
      process.exit(process.exitCode);
    }
    if(result.stage!=='ready_for_manual_card') throw Error('Payment not reached');
    // Wait for actual entry controls, without reading values or selecting/filling a card.
    await page.getByRole('textbox',{name:'Credit card number',exact:false}).waitFor({timeout:20000});
    await mkdir(resolve(ROOT,'checkout_data/rehearsal'),{recursive:true});
    const screenshot=resolve(ROOT,`checkout_data/rehearsal/direct-${control?'black-xs':'lavender-xs'}-payment.png`);
    await page.screenshot({path:screenshot,fullPage:true});
    console.log(JSON.stringify(await journal.save(result.stage,{...result,card_fields_loaded:true,
      elapsed_seconds:(Date.now()-started)/1000,screenshot,submitted:false,standalone:true})));
  }
} catch(error) {
  console.log(JSON.stringify(await journal.save(error.stage??'needs_attention',{
    message:error.stage?error.message:`执行停止：${error.name}`,page_path:page?new URL(page.url()).pathname:null,elapsed_seconds:(Date.now()-started)/1000,
    submitted:false,standalone:true})));
  process.exitCode=1;
} finally {
  // Disconnect on process exit; never close the user's browser/tab.
  process.exit(process.exitCode??0);
}
