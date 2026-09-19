const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { chromium } = require('C:/Users/fordg/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const source = fs.readFileSync('\\\\wsl.localhost\\Ubuntu\\root\\array-operator-offtaker-audit-2026-09-19\\public\\reports.js', 'utf8');
const holds = source.slice(source.indexOf(' function renderDeliveryHolds()'), source.indexOf(' function renderPipeline()'));
const collection = source.slice(source.indexOf(' async function wireCollectionSettings()'), source.indexOf(' async function wireGlobalRate()'));

let browser;
(async () => {
  browser = await chromium.launch({headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const requests = [], posted = new Map();
  let failedOnce = false, reconciled = false;
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.hostname !== 'localhost') return route.abort();
    if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><h1>Isolated Norwich recovery smoke test</h1><div id="rbDeliveryHolds"></div><h2>Collection</h2><div id="rbCollectionBody"></div></body></html>'});
    let body;
    if (url.pathname.endsWith('/payment-policy')) body = {ok:true,policy:'offline'};
    else if (url.pathname.endsWith('/issued-invoices')) body = {ok:true,invoices:[{id:12,status:'accepted',customer_name:'Synthetic Norwich customer',invoice_number:'2026-08',outstanding_cents:failedOnce ? 7500 : 10000}]};
    else if (url.pathname.endsWith('/offline-payments')) {
      const payload = req.postDataJSON(); requests.push(payload);
      if (!posted.has(payload.request_key)) posted.set(payload.request_key,payload);
      if (!failedOnce) { failedOnce = true; return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic response lost after receipt was recorded'})}); }
      body={ok:true,duplicate:true};
    } else if (url.pathname.endsWith('/reconcile')) {
      assert.equal(req.postDataJSON().receipt_id,'provider-fixture-id'); reconciled=true; body={ok:true};
    } else return route.abort();
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  const setup = async () => {
    await page.goto('http://localhost:43434/');
    await page.addScriptTag({content:`
      var API='/v1/array-operator/billing';
      var authHeaders=()=>({Authorization:'Bearer isolated-fixture'}), jsonHdr=()=>({...authHeaders(),'Content-Type':'application/json'});
      var esc=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
      var moneyFmt=v=>'$'+Number(v).toFixed(2), OFFTAKERS=[{id:8,customer_name:'<img src=x onerror=alert(1)>'}], LAST_LIST_ARGS=null;
      var PIPE={holds:[{invoice_id:12,subscription_id:8,period:'2026-08',status:'held',amount_cents:10000,reason:'<script>bad</script>'}],dispatch_holds:[{id:7,kind:'invoice',status:'uncertain',attempts:1,reason:'Acceptance unknown'}]};
      var expandAccordion=()=>{}, loadPipeline=async()=>{PIPE.dispatch_holds=[];}, renderPipeline=()=>renderDeliveryHolds();
      ${holds}
      ${collection}
      renderDeliveryHolds(); wireCollectionSettings();
    `});
    await page.locator('#rbOfflinePayment:not([hidden])').waitFor();
  };
  await setup();
  assert.equal(await page.locator('#rbDeliveryHolds img, #rbDeliveryHolds script').count(),0);
  assert((await page.locator('#rbDeliveryHolds').innerText()).includes('<script>bad</script>'));
  await page.locator('[data-reconcile-dispatch] input').fill('provider-fixture-id');
  await page.locator('[data-reconcile-dispatch] button').click();
  await page.waitForFunction(()=>!document.querySelector('[data-reconcile-dispatch]'));
  assert(reconciled);
  await page.locator('#rbOfflinePayment [name=invoice]').selectOption('12');
  await page.locator('#rbOfflinePayment [name=amount]').fill('25.00');
  await page.locator('#rbOfflinePayment [name=received]').fill('2026-09-18');
  await page.locator('#rbOfflinePayment [name=note]').fill('Check 123');
  await page.locator('#rbOfflinePayment button').click();
  await page.waitForFunction(()=>document.querySelector('#rbCollectionStatus').textContent.includes('Synthetic response lost'));
  assert.equal(requests.length,1);
  await setup();
  await page.waitForFunction(()=>document.querySelector('#rbCollectionStatus').textContent.includes('request ID has been preserved'));
  assert.equal(await page.locator('#rbOfflinePayment [name=amount]').inputValue(),'25.00');
  assert.equal(await page.locator('#rbOfflinePayment [name=note]').inputValue(),'Check 123');
  await page.screenshot({path:path.join(__dirname,'frontend-recovery.png'),fullPage:true});
  await page.locator('#rbOfflinePayment button').click();
  await page.waitForFunction(()=>document.querySelector('#rbCollectionStatus').textContent==='Payment recorded.');
  assert.equal(requests.length,2); assert.equal(posted.size,1); assert.deepEqual(requests[0],requests[1]);
  console.log('PASS: escaped holds, provider-receipt recovery action, and identical offline receipt replay after response loss + reload. All network requests intercepted; one logical receipt.');
  await browser.close();
})().catch(async error=>{console.error(error);if(browser) await browser.close();process.exit(1);});
