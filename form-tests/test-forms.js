const { chromium } = require('playwright');
// Serve the repo root first:  python3 -m http.server 8095 --bind 127.0.0.1
// then:  node form-tests/test-forms.js   (needs Playwright + Chromium)
// All webhook calls are intercepted; nothing is sent to the real n8n.
const BASE = process.env.FORMS_BASE || 'http://127.0.0.1:8095/';
const PIN = '482915';
const img = Buffer.from('ffd8ffe000104a46494600010100000100010000ffd9', 'hex');
let ok = 0, bad = 0;
const check = (name, cond, extra='') => { cond ? ok++ : bad++; console.log((cond ? 'PASS ' : 'FAIL ') + name + (extra ? '  [' + extra + ']' : '')); };

async function setup(page, behaviour) {
  const log = { uploads: [], submits: [] };
  let n = 0;
  await page.route('https://n8n.tail61f871.ts.net/**', async (route) => {
    const req = route.request();
    const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': 'POST, OPTIONS' };
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const url = req.url(); const hdr = req.headers()['x-audit-key'];
    if (url.endsWith('photo-upload')) {
      const body = req.postDataBuffer().toString('latin1');
      const fname = (/name="fileName"\r\n\r\n([^\r]*)/.exec(body) || [])[1];
      const orig = (/filename="([^"]*)"/.exec(body) || [])[1];
      log.uploads.push({ fname, hdr });
      const b = behaviour.upload ? await behaviour.upload(fname, orig) : {};
      if (b.delay) await new Promise(r => setTimeout(r, b.delay));
      if (b.status) return route.fulfill({ status: b.status, headers: cors, body: 'err' });
      n++;
      return route.fulfill({ status: 200, headers: { ...cors, 'Content-Type': 'application/json' },
        body: JSON.stringify({ link: b.link || ('https://drive.example/' + n + '/' + fname), fileId: 'f' + n }) });
    }
    if (url.endsWith('submit')) {
      log.submits.push({ hdr, payload: JSON.parse(req.postData()) });
      const st = behaviour.submit ? behaviour.submit(log.submits.length) : 200;
      return route.fulfill({ status: st, headers: cors, body: st === 200 ? '{"ok":true}' : 'no' });
    }
    return route.fulfill({ status: 404, headers: cors });
  });
  return log;
}
async function enterPin(page) { for (const d of PIN) await page.click(`.pin-key:text-is("${d}")`); }
const file = (name, mime='image/jpeg') => ({ name, mimeType: mime, buffer: img });
async function selectFirst(page, id) { const v = await page.$eval('#' + id, s => [...s.options].find(o => o.value)?.value); await page.selectOption('#' + id, v); }

(async () => {
  const browser = await chromium.launch();
  // ───────────────────────── FORM 1 ─────────────────────────
  {
    const ctx = await browser.newContext({ viewport: { width: 360, height: 740 } });
    const page = await ctx.newPage(); const errs = []; page.on('pageerror', e => errs.push(e.message));
    let failB = true;
    const log = await setup(page, {
      upload: async (fname, orig) => {
        if (/_2\.jpg$/.test(fname) && !globalThis.__slowDone) { globalThis.__slowDone = 1; return { delay: 1500 }; }
        if (globalThis.__phaseB && failB) return { status: 503 };
        if (orig === 'evil.jpg') return { link: 'javascript:alert(1)' };
        return {};
      },
      submit: (i) => i <= 3 ? 500 : 200,   // all 3 automatic attempts fail; manual Retry succeeds
    });
    const src = await (await page.request.get(BASE + 'form1_on_reaching_store.html')).text();
    check('F1 no PIN value in page source', !/ACCESS_PIN|"1234"/.test(src));
    await page.goto(BASE + 'form1_on_reaching_store.html');
    check('F1 PIN pad has 6 dots', (await page.$$('.pin-dot')).length === 6);
    await enterPin(page);
    check('F1 form opens after PIN', await page.isVisible('#form-wrapper'));
    check('F1 photos locked before team/store', await page.$eval('#team-photo', e => e.disabled));
    await page.selectOption('#tl-name', 'teamA'); await page.fill('#tl-phone', '9876543210');
    await page.fill('#store-code', 'MUM-042');
    check('F1 photos unlocked after team/store', !(await page.$eval('#team-photo', e => e.disabled)));
    for (const id of ['store-brand','mall-status','store-category','audit-type']) await selectFirst(page, id);
    await page.fill('#city', 'Mumbai'); await page.fill('#reach-time', '10:30'); await page.fill('#store-mgr', 'Priya');
    await page.fill('#store-contact', '9812345678');
    await page.fill('#store-email', 'not-an-email'); await page.fill('#arm-email', 'arm@brand.com'); await page.fill('#rm-email', 'rm@brand.com');
    // race: pick a1+a2 (a2 slow), then immediately re-pick b (fails)
    await page.setInputFiles('#team-photo', [file('a1.jpg'), file('a2.jpg')]);
    await page.waitForTimeout(300);
    globalThis.__phaseB = true;
    await page.setInputFiles('#team-photo', [file('b.jpg')]);
    await page.waitForTimeout(7000);  // a2 finishes + b's 3 attempts fail
    const st = await page.textContent('#team-photo-status');
    check('F1 race: old photos discarded, new failure visible', /Failed/.test(st) && !/Saved/.test(st), st.replace(/\s+/g,' ').trim());
    check('F1 race: Retry button shown', (await page.$$('#team-photo-status .retry-btn')).length === 1);
    failB = false; globalThis.__phaseB = false; await page.click('#team-photo-status .retry-btn'); await page.waitForTimeout(800);
    check('F1 retry succeeds', /Saved/.test(await page.textContent('#team-photo-status')));
    await page.evaluate(() => window.__x = 0);
    await page.setInputFiles('#backroom-photo', [file('<img src=x onerror=window.__x=1>.png', 'image/png')]);
    await page.waitForTimeout(800);
    check('F1 malicious file name not executed', await page.evaluate(() => window.__x === 0));
    check('F1 file name uses MIME ext + team name', log.uploads.some(u => /^TeamA_.*_MUM042_Photo_of_Backroom\.png$/.test(u.fname)), log.uploads.at(-1).fname);
    check('F1 every upload carries X-Audit-Key = PIN', log.uploads.every(u => u.hdr === PIN));
    check('F1 bad email blocks submit', await page.$eval('#copy-btn', b => b.disabled) && /store email/.test(await page.textContent('#btn-hint')), await page.textContent('#btn-hint'));
    await page.fill('#store-email', 'store@brand.com'); await page.dispatchEvent('#store-email', 'input');
    check('F1 valid form enables submit', !(await page.$eval('#copy-btn', b => b.disabled)), await page.textContent('#btn-hint'));
    const summary = await page.textContent('#summary-text');
    check('F1 summary shows team name not key', /Team Leader\s+: Team A/.test(summary));
    check('F1 summary links Form 2 with audit id', /form2_on_store_sealing\?audit=[0-9a-f-]{36}&team=teamA&store=MUM-042/.test(summary));
    const wide = await page.evaluate(() => document.documentElement.scrollWidth);
    check('F1 no sideways scroll at 360px', wide <= 360, 'scrollWidth=' + wide);
    // submit: first attempt server error x3 retries -> banner; then retry -> success
    const popupPromise = ctx.waitForEvent('page', { timeout: 3000 }).catch(() => null);
    await page.click('#copy-btn');
    check('F1 WhatsApp opened in the tap', !!(await popupPromise));
    await page.waitForSelector('#submit-error', { state: 'visible', timeout: 15000 }).catch(() => {});
    check('F1 failed submit: NOT "Submitted", error + retry shown',
      !(await page.isVisible('#thank-you-screen')) && await page.isVisible('#submit-error') && await page.isVisible('#form-wrapper'));
    // all 3 retry attempts used 500 only for the 1st; later ones succeeded? (first submit call=500, retries=200)
    await page.waitForTimeout(500);
    if (await page.isVisible('#submit-error')) await page.click('#submit-retry');
    await page.waitForSelector('#thank-you-screen', { state: 'visible', timeout: 15000 }).catch(() => {});
    check('F1 thank-you only after server confirms', await page.isVisible('#thank-you-screen'));
    const pl = log.submits.at(-1).payload;
    check('F1 payload: auditId, teamKey, display name, header', pl.auditId && pl.teamKey === 'teamA' && pl.teamLeaderName === 'Team A' && log.submits.at(-1).hdr === PIN);
    check('F1 payload: exactly the one re-picked photo (no stale a1/a2 link)', pl.photoLinks['team-photo'].length === 1 && !/_2\.jpg/.test(pl.photoLinks['team-photo'][0].name));
    check('F1 no page errors', errs.length === 0, errs.join(' | '));
    await ctx.close();
  }
  // ───────────────────────── FORM 1: wrong PIN (401) ─────────────────────────
  {
    const ctx = await browser.newContext(); const page = await ctx.newPage();
    await setup(page, { upload: async () => ({ status: 401 }) });
    await page.goto(BASE + 'form1_on_reaching_store.html'); await enterPin(page);
    await page.selectOption('#tl-name', 'teamA'); await page.fill('#tl-phone', '9876543210'); await page.fill('#store-code', 'MUM-042');
    await page.setInputFiles('#team-photo', [file('a.jpg')]); await page.waitForTimeout(800);
    check('F1 PIN rejected by server -> PIN screen returns', await page.isVisible('#pin-gate') && /not accepted/.test(await page.textContent('#pin-error')));
    await ctx.close();
  }
  // ───────────────────────── FORM 2 ─────────────────────────
  {
    const ctx = await browser.newContext({ viewport: { width: 360, height: 740 }, timezoneId: 'America/New_York' });
    const page = await ctx.newPage(); const errs = []; page.on('pageerror', e => errs.push(e.message));
    const log = await setup(page, {});
    const aid = '3f2b8c1e-1d2a-4c3b-9e8f-0a1b2c3d4e5f';
    await page.goto(BASE + `form2_on_store_sealing.html?audit=${aid}&team=teamA&store=MUM-042`);
    await enterPin(page);
    check('F2 prefilled from Form 1 link', (await page.inputValue('#tl-name')) === 'teamA' && (await page.inputValue('#store-code')) === 'MUM-042');
    await page.fill('#tl-phone', '9876543210'); await page.dispatchEvent('#tl-phone', 'input');
    await page.fill('#sealing-date', '2031-01-01'); await page.dispatchEvent('#sealing-date', 'change');
    check('F2 future sealing date rejected', /sealing date/.test(await page.textContent('#btn-hint')), await page.textContent('#btn-hint'));
    await page.fill('#sealing-date', '2026-11-05'); await page.dispatchEvent('#sealing-date', 'change');
    await page.fill('#team-size', '-5'); await page.dispatchEvent('#team-size', 'input');
    check('F2 negative team size rejected', /team size/.test(await page.textContent('#btn-hint')));
    await page.fill('#team-size', '5'); await page.dispatchEvent('#team-size', 'input');
    await page.fill('#sealing-time', '21:00');
    for (const id of ['gc-status','manual-bills','records-status','next-open-time','tl-verification']) await selectFirst(page, id);
    await page.check('#dec-1'); await page.check('#dec-2');
    for (const id of ['gc-photo','seal-sign-photo','store-seal-photo','billing-photos']) await page.setInputFiles('#' + id, [file(id + '.jpg')]);
    await page.waitForTimeout(2000);
    const s = await page.textContent('#summary-text');
    check('F2 date not shifted in New York timezone', /Date of Execution: 05November2026/.test(s), (s.match(/Date of Execution: \S+/) || [''])[0]);
    check('F2 summary carries linked audit id, no Form 3 placeholder', s.includes(aid) && !/not linked/.test(s) && !/Form 3|form3/.test(s));
    check('F2 submit enabled when complete', !(await page.$eval('#copy-btn', b => b.disabled)), await page.textContent('#btn-hint'));
    await page.click('#copy-btn');
    await page.waitForSelector('#thank-you-screen', { state: 'visible', timeout: 15000 }).catch(() => {});
    const pl = log.submits.at(-1)?.payload || {};
    check('F2 saved: formType, auditId, team name, 4 photo fields',
      pl.formType === 'Form2_OnStoreSealing' && pl.auditId === aid && pl.linkedFromForm1 === true && pl.teamLeaderName === 'Team A' && Object.values(pl.photoLinks || {}).every(a => a.length === 1));
    check('F2 no sideways scroll at 360px', (await page.evaluate(() => document.documentElement.scrollWidth)) <= 360);
    check('F2 no page errors', errs.length === 0, errs.join(' | '));
    await ctx.close();
  }
  await browser.close();
  console.log(`\n${ok} passed, ${bad} failed`);
})();
