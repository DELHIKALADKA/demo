/**
 * The web app in a real browser, used the way a student uses it.
 *
 * run.mjs proves the engine finds the right pages. This proves the *page* works:
 * served under /demo/ - exactly how GitHub Pages serves docs/ from main for this
 * repository - it loads with no console errors and no missing files, finds the four
 * pages of the sample lecture, lets you look at a page without un-ticking it, shows
 * that page at full size, writes a real PDF of the pages you kept, and lets you pick
 * the same video again. Then it opens the offline single-file copy straight from
 * disk and runs a video through that too.
 *
 * No npm packages: Chrome is driven over the DevTools protocol with the WebSocket
 * client built into Node 22+. Chrome is found on PATH (google-chrome, chromium, ...)
 * or named in PAGESNAP_CHROME; extra flags can go in PAGESNAP_CHROME_FLAGS.
 *
 *   node docs/test/browser.mjs
 *
 * Without Chrome it says so and skips (exit 0) - unless PAGESNAP_REQUIRE_BROWSER=1,
 * which CI sets, so CI can never skip it silently.
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import {
  createReadStream, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const DOCS = resolve(here, '..');
const SAMPLE = join(DOCS, 'sample', 'lecture.mp4');
const SAMPLE_PAGES = 4;          // the sample lecture holds four pages of notes
const SAMPLE_SIZE = [1280, 720]; // ...at 1280x720, so that is what a full-size capture is
const BASE = '/demo/';           // where GitHub Pages serves docs/ for this repository
const REQUIRED = process.env.PAGESNAP_REQUIRE_BROWSER === '1';

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

let passed = 0;
const failures = [];
function check(name, condition, detail = '') {
  if (condition) { passed++; console.log(`  PASS  ${name}`); }
  else { failures.push(name); console.log(`  FAIL  ${name}${detail ? `\n          ${detail}` : ''}`); }
  return !!condition;
}

function skip(reason) {
  if (REQUIRED) { console.error(`FAIL: ${reason}`); process.exit(1); }
  console.log(`SKIPPED: ${reason}\n(set PAGESNAP_REQUIRE_BROWSER=1 to make this an error)`);
  process.exit(0);
}

// ── page snippets ─────────────────────────────────────────────────────────────
const visible = (selector) =>
  `(() => { const el = document.querySelector(${JSON.stringify(selector)}); return !!el && el.getClientRects().length > 0; })()`;
const textOf = (selector) =>
  `((document.querySelector(${JSON.stringify(selector)}) || {}).textContent || '').trim()`;
const REVIEW = `${visible('#step-review')} && document.querySelectorAll('#pages .page').length`;
const TICKED = `document.querySelectorAll('#pages .tick.on').length`;
const PREVIEW_OPEN = `document.getElementById('lightbox').classList.contains('on')`;
const ERROR_SHOWN = `${visible('#err')} && document.getElementById('err').textContent.trim()`;

// ── finding and starting Chrome ───────────────────────────────────────────────
function findChrome() {
  if (process.env.PAGESNAP_CHROME) return process.env.PAGESNAP_CHROME;
  const names = process.platform === 'win32'
    ? ['chrome.exe']
    : ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'];
  for (const folder of (process.env.PATH || '').split(delimiter).filter(Boolean)) {
    for (const name of names) if (existsSync(join(folder, name))) return join(folder, name);
  }
  return [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  ].find((path) => existsSync(path)) || null;
}

function launchChrome(executable) {
  const profile = mkdtempSync(join(tmpdir(), 'pagesnap-chrome-'));
  const flags = [
    '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--no-first-run',
    '--no-default-browser-check', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    ...(process.env.PAGESNAP_CHROME_FLAGS || '').split(/\s+/).filter(Boolean),
    'about:blank',
  ];
  const child = spawn(executable, flags, { stdio: ['ignore', 'ignore', 'pipe'] });
  return new Promise((ready, fail) => {
    let log = '';
    const timer = setTimeout(() => fail(new Error(`Chrome did not start in 30 s:\n${log.slice(-1500)}`)), 30000);
    child.stderr.on('data', (chunk) => {
      log += chunk;
      const found = /DevTools listening on (ws:\/\/\S+)/.exec(log);
      if (found) { clearTimeout(timer); ready({ child, profile, endpoint: found[1] }); }
    });
    child.on('error', (error) => { clearTimeout(timer); fail(error); });
    child.on('exit', (code) => { clearTimeout(timer); fail(new Error(`Chrome exited (${code}) early:\n${log.slice(-1500)}`)); });
  });
}

// ── the DevTools protocol, over Node's own WebSocket ──────────────────────────
class DevTools {
  static open(endpoint) {
    return new Promise((ready, fail) => {
      const socket = new WebSocket(endpoint);
      socket.addEventListener('open', () => ready(new DevTools(socket)), { once: true });
      socket.addEventListener('error', () => fail(new Error(`cannot reach Chrome at ${endpoint}`)), { once: true });
    });
  }

  constructor(socket) {
    this.socket = socket;
    this.lastId = 0;
    this.waiting = new Map();
    this.listeners = new Set();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      const call = message.id && this.waiting.get(message.id);
      if (call) {
        this.waiting.delete(message.id);
        if (message.error) call.fail(new Error(`${call.method}: ${message.error.message}`));
        else call.ready(message.result);
      } else if (message.method) {
        for (const listener of this.listeners) listener(message);
      }
    });
    socket.addEventListener('close', () => {
      for (const call of this.waiting.values()) call.fail(new Error('Chrome closed the connection'));
      this.waiting.clear();
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = ++this.lastId;
    this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return new Promise((ready, fail) => this.waiting.set(id, { ready, fail, method }));
  }
}

/** One browser tab, phone-sized, that records every error and every request. */
class Tab {
  static async open(devtools, url) {
    const { targetId } = await devtools.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await devtools.send('Target.attachToTarget', { targetId, flatten: true });
    const tab = new Tab(devtools, sessionId);
    for (const domain of ['Page', 'Runtime', 'Log', 'Network', 'DOM']) await tab.send(`${domain}.enable`);
    await tab.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await tab.goto(url);
    return tab;
  }

  constructor(devtools, sessionId) {
    this.devtools = devtools;
    this.sessionId = sessionId;
    this.problems = [];      // console errors, uncaught exceptions, errors the browser logged
    this.requests = [];      // every URL the page asked for
    this.loads = 0;
    devtools.listeners.add((message) => {
      if (message.sessionId === sessionId) this.event(message.method, message.params);
    });
  }

  send(method, params = {}) { return this.devtools.send(method, params, this.sessionId); }

  event(method, params) {
    if (method === 'Page.loadEventFired') this.loads++;
    else if (method === 'Network.requestWillBeSent') this.requests.push(params.request.url);
    else if (method === 'Runtime.exceptionThrown') {
      const details = params.exceptionDetails;
      this.problems.push(`uncaught: ${((details.exception && details.exception.description) || details.text).split('\n')[0]}`);
    } else if (method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(params.type)) {
      this.problems.push(`console.${params.type}: ${params.args.map((a) => a.value ?? a.description ?? '').join(' ')}`);
    } else if (method === 'Log.entryAdded' && params.entry.level === 'error') {
      this.problems.push(`browser: ${params.entry.text}${params.entry.url ? ` (${params.entry.url})` : ''}`);
    }
  }

  async goto(url) {
    const before = this.loads;
    await this.send('Page.navigate', { url });
    const deadline = Date.now() + 30000;
    while (this.loads === before) {
      if (Date.now() > deadline) throw new Error(`${url} did not finish loading`);
      await sleep(50);
    }
  }

  async eval(expression) {
    const { result, exceptionDetails } = await this.send('Runtime.evaluate',
      { expression, awaitPromise: true, returnByValue: true });
    if (exceptionDetails) throw new Error((exceptionDetails.exception || {}).description || exceptionDetails.text);
    return result.value;
  }

  /** Poll until the expression is truthy; returns its value, or undefined on timeout. */
  async waitFor(expression, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const value = await this.eval(expression).catch(() => undefined);
      if (value) return value;
      if (Date.now() > deadline) return undefined;
      await sleep(150);
    }
  }

  /** A real tap: the mouse goes down and up over the middle of the element. */
  async click(selector) {
    const point = await this.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      el.scrollIntoView({ block: 'center', inline: 'center' });
      const box = el.getBoundingClientRect();
      return box.width && box.height ? { x: box.left + box.width / 2, y: box.top + box.height / 2 } : null;
    })()`);
    if (!point) throw new Error(`nothing visible to tap at ${selector}`);
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
    for (const type of ['mousePressed', 'mouseReleased']) {
      await this.send('Input.dispatchMouseEvent', { type, x: point.x, y: point.y, button: 'left', clickCount: 1 });
    }
  }

  async press(key, keyCode) {
    for (const type of ['keyDown', 'keyUp']) {
      await this.send('Input.dispatchKeyEvent', { type, key, code: key, windowsVirtualKeyCode: keyCode });
    }
  }

  /** What the file picker does when someone chooses a file. */
  async chooseFile(selector, path) {
    const { root } = await this.send('DOM.getDocument', { depth: 0 });
    const { nodeId } = await this.send('DOM.querySelector', { nodeId: root.nodeId, selector });
    if (!nodeId) throw new Error(`no ${selector} on the page`);
    await this.send('DOM.setFileInputFiles', { files: [path], nodeId });
  }
}

// ── docs/ served the way GitHub Pages serves it ───────────────────────────────
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.mp4': 'video/mp4', '.md': 'text/markdown; charset=utf-8', '.json': 'application/json',
};

function serveDocs() {
  const site = { blocked: new Set(), responses: [] };
  const server = createServer((request, response) => {
    const path = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    const answer = (status, headers, body = '') => {
      site.responses.push({ path, status });
      response.writeHead(status, headers);
      response.end(body);
    };
    if (!path.startsWith(BASE)) return answer(404, {}, `not under ${BASE}`);
    let relative = path.slice(BASE.length);
    if (relative === '' || relative.endsWith('/')) relative += 'index.html';
    const file = resolve(DOCS, relative);
    if (site.blocked.has(relative) || !file.startsWith(DOCS + sep) || !existsSync(file) || statSync(file).isDirectory()) {
      return answer(404, {}, 'not found');
    }
    // Byte ranges, as Pages answers them: a browser seeks inside a video with them.
    const size = statSync(file).size;
    const headers = { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream', 'Accept-Ranges': 'bytes' };
    let start = 0, end = size - 1, status = 200;
    const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.range || '');
    if (range && (range[1] || range[2])) {
      start = range[1] ? Number(range[1]) : Math.max(0, size - Number(range[2]));
      end = range[1] && range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
      status = 206;
      headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
    }
    headers['Content-Length'] = end - start + 1;
    site.responses.push({ path, status });
    response.writeHead(status, headers);
    if (request.method === 'HEAD') return response.end();
    createReadStream(file, { start, end }).pipe(response);
  });
  return new Promise((ready) => server.listen(0, '127.0.0.1', () => {
    site.url = `http://127.0.0.1:${server.address().port}${BASE}`;
    site.close = () => server.close();
    ready(site);
  }));
}

// ── reading back what was downloaded ──────────────────────────────────────────
async function waitForFile(folder, name, timeoutMs = 20000) {
  const path = join(folder, name);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(path) && !readdirSync(folder).some((f) => f.endsWith('.crdownload'))) {
      await sleep(200);
      return readFileSync(path);
    }
    await sleep(100);
  }
  return null;
}

/** Check a PDF the way a reader would: header, trailer, every xref offset, pages, images. */
function inspectPdf(bytes) {
  if (!bytes) return { problems: ['nothing was downloaded'], pages: 0, images: [] };
  const text = bytes.toString('latin1');
  const problems = [];
  if (!text.startsWith('%PDF-')) problems.push('it does not start with %PDF-');
  const tail = /startxref\s+(\d+)\s+%%EOF\s*$/.exec(text);
  if (!tail || !text.startsWith('xref', Number(tail[1]))) {
    problems.push('startxref does not point at the cross-reference table');
  } else {
    const table = /^xref\s+0 (\d+)\s+([\s\S]*?)trailer/.exec(text.slice(Number(tail[1])));
    const rows = table ? table[2].trim().split(/\s*\n/).slice(1) : [];
    if (!table || rows.length !== Number(table[1]) - 1) problems.push('the cross-reference table is malformed');
    rows.forEach((row, i) => {
      const offset = Number(row.slice(0, 10));
      if (!text.startsWith(`${i + 1} 0 obj`, offset)) problems.push(`object ${i + 1} is not where the table says`);
    });
  }
  const count = /\/Type \/Pages \/Kids \[[^\]]*\] \/Count (\d+)/.exec(text);
  const images = [...text.matchAll(/\/Subtype \/Image \/Width (\d+) \/Height (\d+)/g)].map((m) => [+m[1], +m[2]]);
  return { problems, pages: count ? Number(count[1]) : 0, images };
}

// ── the checks ────────────────────────────────────────────────────────────────
async function hostedApp(devtools, site, scratch) {
  console.log(`\n[1] the hosted app, served under ${BASE} the way GitHub Pages serves docs/`);
  const tab = await Tab.open(devtools, site.url);
  check('the page loads', /PageSnap/.test(await tab.eval('document.title')));
  check('it opens on "choose a video"', await tab.eval(visible('#step-pick')));

  await tab.click('#sample');
  const found = await tab.waitFor(REVIEW, 120000);
  check(`the sample lecture comes back with its ${SAMPLE_PAGES} pages`, found === SAMPLE_PAGES,
        `review shows ${found}; ${await tab.eval(textOf('#err'))}`);
  check('the summary counts them', await tab.eval(textOf('#summary')) === `${SAMPLE_PAGES} pages found`);
  const summary = await tab.eval(textOf('#summary2'));
  check('...and says how long the video is', /^4 selected · \d+\.\ds of video$/.test(summary), summary);

  // Looking at a page must not change what goes into the PDF.
  await tab.click('#pages .page:nth-child(2) img');
  check('tapping a picture opens it', await tab.waitFor(PREVIEW_OPEN, 3000));
  const ticked = await tab.eval(TICKED);
  check('...without un-ticking it', ticked === SAMPLE_PAGES, `${ticked} of ${SAMPLE_PAGES} still ticked`);
  const shown = await tab.waitFor(`(() => { const img = document.getElementById('lightbox-img');
    return img.complete && img.naturalWidth ? img.naturalWidth + 'x' + img.naturalHeight : ''; })()`, 5000);
  check('...at full size, not as the thumbnail', shown === SAMPLE_SIZE.join('x'), `shown at ${shown}`);
  await tab.press('Escape', 27);
  check('Escape closes it', !(await tab.eval(PREVIEW_OPEN)));

  await tab.click('#pages .page:nth-child(2) .tick');
  check('the tick box un-ticks a page', await tab.eval(TICKED) === SAMPLE_PAGES - 1);
  const after = await tab.eval(textOf('#summary2'));
  check('the summary still says how long the video is', /^3 selected · \d+\.\ds of video$/.test(after), after);
  const button = await tab.eval(textOf('#download'));
  check('the button counts the pages that will be written', button === 'Download the PDF (3 pages)', button);

  await tab.eval(`(() => { const title = document.getElementById('title');
    title.value = 'Biology Chapter 6'; title.dispatchEvent(new Event('input')); })()`);
  check('the file is named after the notes', (await tab.eval(textOf('#filename'))).endsWith('Biology Chapter 6.pdf'));
  await tab.click('#download');
  const pdf = inspectPdf(await waitForFile(scratch, 'Biology Chapter 6.pdf'));
  check('Download gives a well-formed PDF', !pdf.problems.length, pdf.problems.join('; '));
  check('...holding exactly the 3 pages still ticked', pdf.pages === 3 && pdf.images.length === 3,
        `pages=${pdf.pages}, images=${pdf.images.length}`);
  check('...each one the full-size capture',
        pdf.images.length > 0 && pdf.images.every(([w, h]) => w === SAMPLE_SIZE[0] && h === SAMPLE_SIZE[1]),
        JSON.stringify(pdf.images));

  await tab.click('#startover');
  await tab.chooseFile('#file', SAMPLE);
  check('choosing a video from the device runs it', await tab.waitFor(REVIEW, 120000) === SAMPLE_PAGES);
  await tab.click('#startover');
  await tab.chooseFile('#file', SAMPLE);
  const restarted = await tab.waitFor(`${visible('#step-work')} || ${visible('#step-review')}`, 5000);
  check('choosing the SAME video again after "Start over" runs it again',
        restarted && await tab.waitFor(REVIEW, 120000) === SAMPLE_PAGES);

  const notVideo = join(scratch, 'not-a-video.mp4');
  writeFileSync(notVideo, 'These are typed notes, not a video.\n'.repeat(40));
  await tab.click('#startover');
  await tab.chooseFile('#file', notVideo);
  const refusal = await tab.waitFor(ERROR_SHOWN, 20000);
  check('a file that is not a video is refused, with the reason', /could not be read as a video/i.test(refusal || ''),
        refusal || 'nothing was shown');
  check('...and the page is back on "choose a video"', await tab.eval(visible('#step-pick')));
  await tab.chooseFile('#file', SAMPLE);
  check('a real video after that works', await tab.waitFor(REVIEW, 120000) === SAMPLE_PAGES);

  check('no console errors or uncaught exceptions', !tab.problems.length, tab.problems.join('\n          '));
  const failed = site.responses.filter((r) => r.status >= 400);
  check('no request failed (no 404s)', !failed.length, failed.map((r) => `${r.status} ${r.path}`).join(', '));

  // If the sample cannot be fetched, say that - not "this is not a video".
  site.blocked.add('sample/lecture.mp4');
  await tab.goto(site.url);
  await tab.click('#sample');
  const sampleError = await tab.waitFor(ERROR_SHOWN, 20000);
  check('a sample that will not download is reported as exactly that', /could not load the sample/i.test(sampleError || ''),
        sampleError || 'nothing was shown');
  site.blocked.clear();
}

async function offlineCopy(devtools, scratch) {
  console.log('\n[2] the offline single-file copy, opened straight from disk');
  const url = pathToFileURL(join(DOCS, 'pagesnap-offline.html')).href;
  const tab = await Tab.open(devtools, url);
  await sleep(300);
  check('it opens with no errors', !tab.problems.length, tab.problems.join('\n          '));
  check('it does not tell you to open it from a web link instead', !(await tab.eval(visible('#err'))),
        await tab.eval(textOf('#err')));
  const others = tab.requests.filter((u) => u !== url && !u.startsWith('data:') && !u.startsWith('blob:'));
  check('it loads nothing but itself', !others.length, others.join(', '));
  check('it has no sample button (no sample travels with it)', !(await tab.eval(`!!document.getElementById('sample')`)));

  await tab.chooseFile('#file', SAMPLE);
  const found = await tab.waitFor(REVIEW, 120000);
  check(`picking a video finds its ${SAMPLE_PAGES} pages`, found === SAMPLE_PAGES,
        `review shows ${found}; ${await tab.eval(textOf('#err'))}`);
  await tab.click('#pages .page:nth-child(1) img');
  check('tapping a picture opens it without un-ticking it',
        await tab.waitFor(PREVIEW_OPEN, 3000) && await tab.eval(TICKED) === SAMPLE_PAGES);
  await tab.click('#lightbox');
  check('tapping the preview closes it', !(await tab.eval(PREVIEW_OPEN)));
  await tab.click('#download');
  const pdf = inspectPdf(await waitForFile(scratch, 'lecture.pdf'));
  check(`Download gives a well-formed ${SAMPLE_PAGES}-page PDF`, !pdf.problems.length && pdf.pages === SAMPLE_PAGES,
        pdf.problems.join('; ') || `pages=${pdf.pages}`);
  check('no console errors or uncaught exceptions', !tab.problems.length, tab.problems.join('\n          '));
}

async function openedFromDisk(devtools) {
  console.log('\n[3] index.html opened from disk, where browsers refuse its modules');
  const tab = await Tab.open(devtools, pathToFileURL(join(DOCS, 'index.html')).href);
  const message = await tab.waitFor(ERROR_SHOWN, 5000);
  check('it says so, and points to the offline copy', /pagesnap-offline\.html/.test(message || ''),
        message || 'nothing was shown');
}

async function main() {
  if (typeof WebSocket === 'undefined') {
    skip(`this drives Chrome with the WebSocket built into Node 22+, and this is Node ${process.versions.node}`);
  }
  const executable = findChrome();
  if (!executable) skip('no Chrome or Chromium found - put it on PATH, or set PAGESNAP_CHROME to its path');
  console.log(`Chrome: ${executable}`);

  const site = await serveDocs();
  const chrome = await launchChrome(executable);
  const scratch = mkdtempSync(join(tmpdir(), 'pagesnap-browser-test-'));
  try {
    const devtools = await DevTools.open(chrome.endpoint);
    console.log(`       ${(await devtools.send('Browser.getVersion')).product}`);
    await devtools.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: scratch });
    // Each part runs on its own, so a failure in one cannot hide what the others find.
    const parts = [
      ['hosted app', () => hostedApp(devtools, site, scratch)],
      ['offline copy', () => offlineCopy(devtools, scratch)],
      ['index.html from disk', () => openedFromDisk(devtools)],
    ];
    for (const [name, part] of parts) {
      try { await part(); } catch (error) {
        check(`the ${name} checks ran to the end`, false, (error && error.stack) || String(error));
      }
    }
  } catch (error) {
    check('Chrome could be driven', false, (error && error.stack) || String(error));
  } finally {
    chrome.child.kill('SIGKILL');
    site.close();
    for (const folder of [scratch, chrome.profile]) {
      try { rmSync(folder, { recursive: true, force: true }); } catch (_) { /* Chrome may still hold it */ }
    }
  }

  console.log(`\n${'='.repeat(74)}\n${passed} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
}

main();
