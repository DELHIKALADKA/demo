/**
 * Runs the real app in a real browser: it decodes real videos, finds real pages,
 * downloads a real PDF and reads that PDF back.
 *
 *     node test/browser.mjs
 *
 * Why this exists: the engine tests hand `signature()` frames pulled out by ffmpeg, so
 * they say nothing about whether the *page* works - whether a video element in a
 * hidden card can still be drawn (iOS cannot), whether a click on a picture also
 * unticks the page, whether the download really lands. Those need a browser.
 *
 * It needs a browser and `puppeteer-core`. Give it one with PAGESNAP_CHROME=/path/to/
 * chrome, or it looks in the usual places (including the Chrome on a GitHub runner).
 * With no browser at all it prints SKIPPED and exits 0: a machine without Chrome is
 * not a broken app. Videos come from test/make_media.py.
 *
 * What a headless Chrome on Linux *cannot* answer, and this suite therefore does not
 * claim: how iOS Safari behaves (see docs/AUDIT.md for what is untested there).
 */

import { createServer } from 'node:http';
import { readFile, readdir, mkdtemp, stat, rm, unlink } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, extname, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const DOCS = join(here, '..');
const ROOT = join(DOCS, '..');
const MEDIA = join(here, 'media');
const SAMPLE = join(DOCS, 'sample', 'lecture.mp4');

let checks = 0, failures = 0, skipped = 0;
function ok(condition, label, detail = '') {
  checks++;
  if (condition) console.log(`  [PASS] ${label}`);
  else { failures++; console.log(`  [FAIL] ${label}${detail ? ` — ${detail}` : ''}`); }
}
function note(text) { console.log(`  [info] ${text}`); }
function skip(text) { skipped++; console.log(`  [SKIP] ${text}`); }
function section(title) { console.log(`\n=== ${title} ===`); }
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------- //
// plumbing
// --------------------------------------------------------------------------- //

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.md': 'text/markdown; charset=utf-8',
  '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
  '.mkv': 'video/x-matroska', '.json': 'application/json',
};

/** Serve docs/ the way GitHub Pages does - modules will not load from file://. */
async function serveDocs() {
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    let path = join(DOCS, decodeURIComponent(url.pathname));
    if (!path.startsWith(DOCS)) { res.writeHead(403).end('no'); return; }
    try {
      const info = await stat(path);
      if (info.isDirectory()) path = join(path, 'index.html');
      const body = await readFile(path);
      res.writeHead(200, {
        'content-type': MIME[extname(path)] || 'application/octet-stream',
        'content-length': body.length,
      });
      res.end(body);
    } catch (_) {
      res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, port: server.address().port };
}

/** The browser to drive: an env var, the usual install locations, or nothing. */
function findChrome() {
  const candidates = [
    process.env.PAGESNAP_CHROME,
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium', '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((path) => existsSync(path)) || null;
}

async function waitFor(page, fn, { timeout = 60000, every = 250, label = 'condition' } = {}) {
  const started = Date.now();
  for (;;) {
    if (await page.evaluate(fn)) return Date.now() - started;
    if (Date.now() - started > timeout) throw new Error(`timed out waiting for ${label}`);
    await wait(every);
  }
}

/** True once the app has finished with a file, one way or the other. */
/**
 * Console errors caused by the test, not by the app.
 *
 * Every console error is recorded. A section that deliberately asks for something that
 * cannot load calls `accountForDeliberate(errors, mark, ...)` with the index it started
 * at, and that call removes exactly the errors it provoked - printing each one, so
 * nothing is hidden. Anything else stays and fails the final check.
 */
const EXPECTED_LOAD_FAILURE = [
  /Failed to load resource: net::ERR_CONNECTION_FAILED/,
  /Failed to load resource: net::ERR_FAILED/,
  /Failed to load resource: the server responded with a status of \d+/,
];

async function accountForDeliberate(errors, mark, label) {
  // The browser logs a failed load a moment after the app has already reacted, so
  // looking straight away misses it and the error gets counted at the end.
  await wait(500);
  const provoked = errors.slice(mark);
  if (!provoked.length) return;
  const unexpected = provoked.filter((line) => !EXPECTED_LOAD_FAILURE.some((p) => p.test(line)));
  if (unexpected.length) return;                 // leave them to be caught
  errors.length = mark;                          // they were ours; drop them
  for (const line of provoked) console.log(`  [info] ${label} logged: ${line}`);
}

const SETTLED = () => {
  const review = document.getElementById('step-review');
  const err = document.getElementById('err');
  const pages = document.getElementById('pages');
  const doneReviewing = review && review.style.display !== 'none' && pages && pages.children.length > 0;
  const failed = err && err.style.display === 'block' && err.textContent.trim().length > 0;
  return !!(doneReviewing || failed);
};

const STATE = () => {
  const err = document.getElementById('err');
  return {
    step: ['pick', 'work', 'review'].find((s) =>
      getComputedStyle(document.getElementById(`step-${s}`)).display !== 'none') || 'none',
    pages: document.getElementById('pages').children.length,
    error: err.style.display === 'block' ? err.textContent.trim() : '',
    summary: document.getElementById('summary').textContent,
    summary2: document.getElementById('summary2').textContent,
    download: document.getElementById('download').textContent,
    downloadDisabled: document.getElementById('download').disabled,
    filename: document.getElementById('filename').textContent,
  };
};

const timesOnScreen = () => [...document.querySelectorAll('#pages .meta span:last-child')]
  .map((el) => parseFloat(el.textContent));

/** Load the app in a fresh page and collect anything it throws. */
async function openApp(browser, base, { file = 'index.html', viewport } = {}) {
  const page = await browser.newPage();
  if (viewport) await page.setViewport(viewport);
  const errors = [];
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    errors.push(`console: ${m.text()}`);
  });
  await page.goto(`${base}/${file}`, { waitUntil: 'load' });
  await page.evaluate(() => new Promise((resolve) => {
    if (document.readyState === 'complete') resolve();
    else window.addEventListener('load', resolve, { once: true });
  }));
  return { page, errors };
}

/** Type into the title box the way a person would, so the app hears it. */
async function setTitle(page, value) {
  await page.$eval('#title', (el, text) => {
    el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, value);
}

/** Be sitting on the review screen with pages, whatever ran before this. */
async function ensureReview(page, videoPath) {
  const state = await page.evaluate(STATE);
  if (state.step === 'review' && state.pages > 0) return state;
  await backToPick(page);
  return chooseFile(page, videoPath);
}

/** Get back to the pick screen, whether or not the review card is showing. */
async function backToPick(page) {
  const showing = await page.evaluate(() =>
    getComputedStyle(document.getElementById('step-review')).display !== 'none');
  if (showing) await page.click('#startover');
}

/** Choose a file the way the file picker does, and wait for the job to finish. */
async function chooseFile(page, path, { timeout = 120000 } = {}) {
  const input = await page.$('#file');
  await input.uploadFile(path);
  await waitFor(page, SETTLED, { timeout, label: `the app to finish with ${path.split('/').pop()}` });
  return page.evaluate(STATE);
}

// --------------------------------------------------------------------------- //
// reading the PDF back
// --------------------------------------------------------------------------- //

function pythonForProbe() {
  const candidates = [
    process.env.PAGESNAP_PYTHON,
    join(ROOT, '.venv', 'bin', 'python'),
    join(ROOT, '.venv', 'Scripts', 'python.exe'),
    'python3', 'python', 'py',
  ].filter(Boolean);
  for (const python of candidates) {
    const probe = spawnSync(python, ['-c', 'import pypdf, PIL'], { stdio: 'ignore' });
    if (probe.status === 0) return python;
  }
  return null;
}

/** Page count, page order and what is printed on each page. */
function probePdf(path) {
  const python = pythonForProbe();
  if (python) {
    const run = spawnSync(python, [join(here, 'pdfprobe.py'), path], { encoding: 'utf8' });
    if (run.status === 0) return { ...JSON.parse(run.stdout), how: 'pypdf + Pillow' };
  }
  // No python with pypdf here: still check the structure, and say which was used.
  const bytes = readFileSync(path);
  const text = bytes.toString('latin1');
  const count = Number((/\/Count (\d+)/.exec(text) || [])[1] || 0);
  return {
    header: text.slice(0, 8), pageCount: count,
    pages: Array.from({ length: count }, () => ({ images: [] })),
    how: 'structure only (no pypdf available)',
  };
}

/** red / green / blue - which channel dominates this page's picture. */
function dominantColour(page) {
  const mean = page.images?.[0]?.mean;
  if (!mean) return null;
  const [r, g, b] = mean;
  if (r > g + 40 && r > b + 40) return 'red';
  if (g > r + 40 && g > b + 40) return 'green';
  if (b > r + 40 && b > g + 40) return 'blue';
  return `other(${mean.join(',')})`;
}

// --------------------------------------------------------------------------- //
// the checks
// --------------------------------------------------------------------------- //

async function main() {
  const chrome = findChrome();
  if (!chrome) {
    console.log('\nSKIPPED: no browser found (set PAGESNAP_CHROME=/path/to/chrome).');
    console.log('The engine, the sample video and the PDF writer are still covered by run.mjs.\n');
    process.exit(0);
  }

  let puppeteer;
  try {
    puppeteer = (await import('puppeteer-core')).default;
  } catch (_) {
    console.log('\nSKIPPED: puppeteer-core is not installed (npm install --prefix docs).\n');
    process.exit(0);
  }

  const missingMedia = ['colours.mp4', 'phone.mov', 'slides.webm', 'slides.mkv', 'hevc.mp4',
    'fake.mp4', 'broken.mp4'].filter((name) => !existsSync(join(MEDIA, name)));
  if (missingMedia.length) {
    console.log(`\nSKIPPED: test media missing (${missingMedia.join(', ')}).`);
    console.log('  run: python3 docs/test/make_media.py\n');
    process.exit(0);
  }

  const { server, port } = await serveDocs();
  const base = `http://127.0.0.1:${port}`;
  const downloads = await mkdtemp(join(tmpdir(), 'pagesnap-downloads-'));
  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required',
      '--allow-file-access-from-files'],
    // A UTF-8 locale, because a download folder in the C locale turns every non-ASCII
    // filename into a file called "download" - which would look like the app's fault
    // when it is the environment's. Measured, not assumed: see docs/AUDIT.md.
    env: { ...process.env, LANG: process.env.LANG || 'C.UTF-8', LC_ALL: process.env.LC_ALL || 'C.UTF-8' },
  });
  console.log(`browser: ${chrome}`);
  console.log(`page:    ${base}/index.html`);

  const { page, errors } = await openApp(browser, base, { viewport: { width: 390, height: 844 } });
  const cdp = await page.createCDPSession();
  await cdp.send('Browser.setDownloadBehavior', {
    behavior: 'allow', downloadPath: downloads, eventsEnabled: true,
  });
  const suggestedNames = [];
  cdp.on('Browser.downloadWillBegin', (event) => suggestedNames.push(event.suggestedFilename));

  const downloadOnce = async (label) => {
    // Empty the folder first: two downloads of the same title would otherwise either
    // overwrite or be renamed "(1)", and "which file is new" stops being reliable.
    for (const entry of await readdir(downloads)) await unlink(join(downloads, entry));
    const seen = new Set(await readdir(downloads));
    const before = suggestedNames.length;
    const started = Date.now();
    await page.click('#download');
    let name = null;
    while (Date.now() - started < 30000) {
      if (suggestedNames.length > before) { name = suggestedNames[suggestedNames.length - 1]; break; }
      await wait(200);
    }
    if (!name) throw new Error(`${label}: the browser never started a download`);
    let file = null;
    while (Date.now() - started < 40000) {
      const fresh = (await readdir(downloads))
        .filter((entry) => !seen.has(entry) && !entry.endsWith('.crdownload'));
      if (fresh.length) { file = join(downloads, fresh[0]); break; }
      await wait(200);
    }
    if (!file) throw new Error(`${label}: no finished file appeared`);
    await wait(400);                       // let the writer flush the last bytes
    return { name, file, bytes: (await stat(file)).size };
  };

  const settledAfter = async (label) => {
    const state = await page.evaluate(STATE);
    note(`${label}: step=${state.step} pages=${state.pages} ${state.error ? `error="${state.error}"` : ''}`);
    return state;
  };

  // ---- [1] does it load, and does it tell the truth about formats ---------- //
  section('[1] the page loads, and the format message matches the browser');
  ok(errors.length === 0, 'no JavaScript errors on load', errors.join(' | '));
  const formatText = await page.$eval('#formats', (el) => el.textContent);
  const formatNote = await page.$eval('#format-note', (el) => el.textContent);
  const support = await page.evaluate(() => {
    const probe = document.createElement('video');
    const plays = (t) => { try { return probe.canPlayType(t) !== ''; } catch (_) { return ''; } };
    return {
      mp4: plays('video/mp4; codecs="avc1.42E01E"'),
      webm: plays('video/webm; codecs="vp8"'),
      mkv: plays('video/x-matroska; codecs="avc1.42E01E"'),
    };
  });
  note(`this browser reports: mp4="${support.mp4}" webm="${support.webm}" mkv="${support.mkv}"`);
  note(`the page says: ${formatText}`);
  ok(!/mp4, mov, mkv, webm/.test(formatText), 'the old one-size-fits-all list is gone');
  ok(support.mp4 ? /mp4/.test(formatText) : !/mp4/.test(formatText),
    'the message names mp4 exactly when the browser reports it can play mp4');
  ok(support.webm ? /webm/.test(formatText) : !/webm/.test(formatText),
    'the message names webm exactly when the browser reports it can play webm');
  ok(support.mkv ? /mkv/.test(formatText) : !/mkv/.test(formatText),
    'the message never promises mkv to a browser that cannot play it');
  ok(/codec/i.test(formatNote), 'the note explains that the codec inside the file is what matters');
  if (!support.mkv) ok(/mkv/i.test(formatNote), 'mkv is called out where it cannot be played');

  // ---- [2] the sample, decoded for real ----------------------------------- //
  section('[2] the sample video, decoded by the browser itself');
  const canH264 = support.mp4 !== '';
  if (!canH264) {
    skip('this browser reports no H.264 support, so the H.264 sample cannot be decoded here');
    skip('the checks below that need the sample (its pages, the PDF download) are skipped too');
  }
  if (canH264) {
  await page.click('#sample');
  let sawVideoRendered = false;
  for (let i = 0; i < 20; i++) {
    const shown = await page.evaluate(() => {
      const v = document.getElementById('video');
      return { display: getComputedStyle(v).display, visibility: getComputedStyle(v).visibility };
    });
    if (shown.display !== 'none' && shown.visibility !== 'hidden') sawVideoRendered = true;
    await wait(200);
  }
  await waitFor(page, SETTLED, { timeout: 180000, label: 'the sample to finish' });
  let state = await settledAfter('the sample run');
  ok(state.step === 'review', 'the sample reaches the review screen', `step=${state.step} ${state.error}`);
  ok(state.summary === '4 pages found', 'the sample really yields the 4 pages the button promises', state.summary);
  const sampleTimes = await page.evaluate(timesOnScreen);
  note(`captured at ${sampleTimes.map((t) => `${t}s`).join(', ')}`);
  ok(sampleTimes.some((t) => Math.abs(t - 1) <= 0.4), 'a page was captured at the opening page (1s)');
  ok(sampleTimes.some((t) => Math.abs(t - 13) <= 0.4), 'a page was captured where the writing finishes (13s)');
  ok(sampleTimes.some((t) => Math.abs(t - 16) <= 0.4), 'a page was captured at the 16s cut');
  ok(sampleTimes.some((t) => t >= 24.4 && t <= 33.7),
    'a page was captured from the final page (on screen from ~24.7s)');
  ok(sawVideoRendered, 'the video element stayed rendered while it was being read (iOS needs this)');
  const shots = await page.evaluate(() => [...document.querySelectorAll('#pages img')].map((img) => ({
    src: img.src.slice(0, 5), w: img.naturalWidth, h: img.naturalHeight,
  })));
  ok(shots.length === 4 && shots.every((s) => s.w >= 1000),
    'every captured page is kept at full resolution', JSON.stringify(shots.slice(0, 1)));
  ok(shots.every((s) => s.src === 'blob:'), 'the pictures are blob URLs, not 300px thumbnails');
  }

  // ---- [3] the full-size view --------------------------------------------- //
  // Written against whatever video is on screen (the sample when it can be decoded,
  // otherwise the WebM), so the same checks run on any browser.
  section('[3] "Show the full picture" (was dead markup) and the full-size view');
  await ensureReview(page, join(MEDIA, 'slides.webm'));
  const cards = await page.evaluate(() => ({
    total: document.querySelectorAll('#pages .page').length,
    shotWidth: document.querySelector('#pages img').naturalWidth,
    shotSrc: document.querySelector('#pages img').src.slice(0, 5),
  }));
  note(`on screen: ${cards.total} pages, captured ${cards.shotWidth}px wide`);
  ok(cards.total >= 2, 'there are pages to look at');
  ok(cards.shotWidth > 320 && cards.shotSrc === 'blob:',
    'the captures are full-size pictures, not 300px thumbnails', `${cards.shotWidth}px`);

  ok(await page.$eval('#bigger', (el) => getComputedStyle(el).display !== 'none'),
    'the button is visible once there are pages');
  const keptBefore = (await page.evaluate(STATE)).summary2;
  await page.click('#pages .page:nth-child(2) .shot');
  const viewer = await page.evaluate(() => ({
    open: document.getElementById('lightbox').classList.contains('on'),
    width: document.getElementById('lightbox-img').naturalWidth,
    src: document.getElementById('lightbox-img').src,
    count: document.getElementById('lb-count').textContent,
    focus: document.activeElement.id,
    scrollLocked: document.body.classList.contains('lb-open'),
    prevDisabled: document.getElementById('lb-prev').disabled,
  }));
  const gridSrc = await page.$eval('#pages .page:nth-child(2) img', (img) => img.src);
  ok(viewer.open, 'clicking a page opens the full-size view');
  ok(viewer.width === cards.shotWidth,
    'it shows the capture at full resolution, not the thumbnail', `${viewer.width}px`);
  ok(viewer.src === gridSrc, 'and it is the very same picture the grid showed');
  ok(viewer.count === `Page 2 of ${cards.total}`, 'it says which page is on screen', viewer.count);
  ok(viewer.focus === 'lb-close', 'focus moves into the viewer so a keyboard can use it', viewer.focus);
  ok(viewer.scrollLocked, 'the page behind it stops scrolling');
  ok(!viewer.prevDisabled, 'the previous button is usable from page 2');
  const keptAfter = (await page.evaluate(STATE)).summary2;
  ok(keptBefore === keptAfter, 'opening a picture does not also untick the page', `${keptBefore} -> ${keptAfter}`);

  await page.keyboard.press('ArrowLeft');
  const atFirst = await page.evaluate(() => ({
    count: document.getElementById('lb-count').textContent,
    prevDisabled: document.getElementById('lb-prev').disabled,
  }));
  ok(atFirst.count === `Page 1 of ${cards.total}`, 'arrow keys move through the pages', atFirst.count);
  ok(atFirst.prevDisabled, 'the previous button is disabled on the first page');
  await page.keyboard.press('ArrowRight');
  ok(await page.$eval('#lb-count', (el) => el.textContent) === `Page 2 of ${cards.total}`,
    'and back again');
  await page.keyboard.press('Escape');
  const afterEscape = await page.evaluate(() => ({
    open: document.getElementById('lightbox').classList.contains('on'),
    focusClass: document.activeElement.className,
    focusLabel: document.activeElement.getAttribute('aria-label') || '',
  }));
  ok(!afterEscape.open, 'Escape closes it');
  ok(/shot/.test(afterEscape.focusClass) && /page 2/i.test(afterEscape.focusLabel),
    'focus goes back to the page that was open', `${afterEscape.focusClass} "${afterEscape.focusLabel}"`);

  const third = Math.min(3, cards.total);
  await page.click(`#pages .page:nth-child(${third}) .tick`);       // select that page
  await page.click('#bigger');
  const bigger = await page.evaluate(() => ({
    count: document.getElementById('lb-count').textContent,
    width: document.getElementById('lightbox-img').naturalWidth,
    label: document.getElementById('bigger').getAttribute('aria-label'),
  }));
  ok(bigger.count === `Page ${third} of ${cards.total}`, 'the button opens the page that is selected', bigger.count);
  ok(bigger.width === cards.shotWidth, 'and at full size', `${bigger.width}px`);
  ok(new RegExp(`page ${third} of ${cards.total}`, 'i').test(bigger.label),
    'its accessible name says which page will open', bigger.label);

  // ---- [4] start over, and the same file again ---------------------------- //
  section('[4] Start over');
  // The viewer covers the page (it is a modal). Closing it first is not a detail: an
  // earlier version of this suite clicked straight through it and tested nothing.
  await page.keyboard.press('Escape');
  ok(await page.evaluate(() => !document.getElementById('lightbox').classList.contains('on')),
    'the viewer is closed before touching anything behind it');
  await page.click('#startover');
  const afterReset = await page.evaluate(() => ({
    step: getComputedStyle(document.getElementById('step-pick')).display === 'none' ? 'hidden' : 'pick',
    pages: document.getElementById('pages').children.length,
    fileValue: document.getElementById('file').value,
    inputValue: document.getElementById('file').value === '' ? 'empty' : 'still set',
    title: document.getElementById('title').value,
    download: document.getElementById('download').textContent,
    bigger: getComputedStyle(document.getElementById('bigger')).display,
    videoSrc: document.getElementById('video').getAttribute('src'),
    changeWouldFire: document.getElementById('file').value === '',
  }));
  ok(afterReset.step === 'pick', 'it goes back to the pick-a-video screen');
  ok(afterReset.pages === 0, 'the old review is cleared', `${afterReset.pages} cards left`);
  ok(afterReset.inputValue === 'empty', 'the file input is cleared');
  ok(afterReset.changeWouldFire, 'so choosing the same file again really fires (the old bug: no event at all)');
  ok(afterReset.download === 'Download the PDF', 'the button is reset', afterReset.download);
  ok(afterReset.bigger === 'none', 'the full-size button is hidden again');
  ok(!afterReset.videoSrc, 'the video no longer points at a released URL');

  const webm = join(MEDIA, 'slides.webm');
  const again = await chooseFile(page, webm);
  ok(again.step === 'review' && again.pages === 2, 'the same file can be processed again', JSON.stringify(again));
  await page.click('#startover');
  const other = await chooseFile(page, join(MEDIA, 'slides.mkv'));
  ok(other.step === 'review' && other.pages === 2, 'and a different file works after that', JSON.stringify(other));

  // ---- [5] the sample being missing or damaged ---------------------------- //
  section('[5] when the sample cannot be loaded');
  await page.setRequestInterception(true);
  let sampleMode = '404';
  const interceptSample = (request) => {
    if (request.url().includes('sample/lecture.mp4')) {
      if (sampleMode === '404') {
        request.respond({ status: 404, contentType: 'text/html', body: '<h1>Not found</h1>' });
      } else if (sampleMode === 'damaged') {
        request.respond({ status: 200, contentType: 'text/html', body: 'this is not a video' });
      } else if (sampleMode === 'offline') {
        // What a dropped connection looks like: fetch() itself rejects, so there is no
        // response at all to check. Tested because it takes a different path through
        // the app than a 404 does.
        request.abort('connectionfailed');
      } else {
        request.continue();
      }
      return;
    }
    request.continue();
  };
  page.on('request', interceptSample);
  let deliberateMark = errors.length;
  await page.click('#startover');
  await page.click('#sample');
  await waitFor(page, () => document.getElementById('err').style.display === 'block',
    { timeout: 20000, label: 'the 404 to be reported' });
  const notFound = await page.evaluate(STATE);
  note(`404 -> "${notFound.error}"`);
  ok(/sample/i.test(notFound.error), 'the 404 message names the sample');
  ok(/404|missing/i.test(notFound.error), 'it says the sample is missing and reports the status');
  ok(/choose a video/i.test(notFound.error), 'it offers the way out: choose a video from the device');
  ok(notFound.step === 'pick', 'the page is still usable afterwards');
  const sampleLabel = await page.$eval('#sample', (el) => ({ text: el.textContent, disabled: el.disabled }));
  ok(!sampleLabel.disabled && /sample lecture video/.test(sampleLabel.text),
    'the sample button is usable again', JSON.stringify(sampleLabel));

  await accountForDeliberate(errors, deliberateMark, 'the sample 404');

  // A reply that is not a video at all: 200 OK, wrong content.
  sampleMode = 'damaged';
  deliberateMark = errors.length;
  await page.click('#sample');
  await waitFor(page, () => /damaged|incomplete/i.test(document.getElementById('err').textContent),
    { timeout: 20000, label: 'the damaged reply to be reported' });
  const damaged = await page.evaluate(STATE);
  note(`bad reply -> "${damaged.error}"`);
  ok(/damaged|incomplete/i.test(damaged.error),
    'a wrong-content reply is reported as damaged, not as a video error');
  await accountForDeliberate(errors, deliberateMark, 'the damaged reply');

  // A dropped connection: fetch() itself rejects, so there is no response to inspect.
  // This does not need a decodable sample, so it runs on every browser.
  sampleMode = 'offline';
  deliberateMark = errors.length;
  await page.click('#sample');
  // Wait for a *fresh* message: retrying clears the previous one, so a stale error
  // cannot be mistaken for this attempt's result.
  await waitFor(page, () => {
    const box = document.getElementById('err');
    return box.style.display === 'block' && /downloaded|offline/i.test(box.textContent);
  }, { timeout: 20000, label: 'the dropped connection to be reported' });
  const dropped = await page.evaluate(STATE);
  note(`dropped connection -> "${dropped.error}"`);
  ok(/offline|could not be downloaded/i.test(dropped.error),
    'a network failure is reported as a download failure, not as a broken video');
  ok(/choose a video/i.test(dropped.error), 'and it still offers the way out');
  ok(dropped.step === 'pick', 'the page is usable after a network failure');
  await accountForDeliberate(errors, deliberateMark, 'the dropped connection');

  if (canH264) {
    sampleMode = 'ok';
    await page.click('#sample');
    await waitFor(page, () => {
      const review = document.getElementById('step-review');
      return getComputedStyle(review).display !== 'none'
        && document.getElementById('pages').children.length === 4;
    }, { timeout: 180000, label: 'the real sample after the failures' });
    const recovered = await page.evaluate(STATE);
    ok(recovered.step === 'review' && recovered.pages === 4, 'and the real sample still works after that');
  } else {
    skip('the sample cannot be decoded here, so "it works again afterwards" was not run');
  }
  page.off('request', interceptSample);
  await page.setRequestInterception(false);

  // ---- [6] which files this browser can actually decode ------------------- //
  section('[6] real decoding, file by file');
  const files = ['colours.mp4', 'phone.mov', 'slides.webm', 'slides.mkv', 'hevc.mp4',
    'fake.mp4', 'broken.mp4'];
  const outcomes = {};
  for (const name of files) {
    await backToPick(page);
    const before = errors.length;
    const state = await chooseFile(page, join(MEDIA, name));
    outcomes[name] = state;
    const label = state.step === 'review' ? `decoded — ${state.pages} page(s)` : `refused — "${state.error.slice(0, 60)}…"`;
    note(`${name.padEnd(12)} ${label}`);
    ok(errors.length === before, `${name} throws nothing at the page`, errors.slice(before).join(' | '));
    if (state.step !== 'review') {
      ok(state.error.length > 0 && /could not be read|no picture|damaged|length/i.test(state.error),
        `${name} is refused with a clear, actionable message`);
      ok(/mp4|codec|another|device|damaged/i.test(state.error),
        `${name} message says what to try instead`);
    }
  }
  if (canH264) {
    ok(outcomes['colours.mp4'].pages === 3, 'the three-page test video gives 3 pages',
      JSON.stringify(outcomes['colours.mp4'].summary));
    ok(outcomes['phone.mov'].pages === 2, 'the .mov from a phone gives its 2 pages',
      outcomes['phone.mov'].summary);
  } else {
    skip('colours.mp4 and phone.mov are H.264, which this browser does not decode');
    if (outcomes['colours.mp4'].step === 'review') {
      note('it decoded them anyway, so the refusal path cannot be judged here');
    } else {
      ok(/could not be read|codec/i.test(outcomes['colours.mp4'].error),
        'an mp4 it cannot decode is refused with a message about the codec',
        outcomes['colours.mp4'].error);
    }
  }
  ok(outcomes['slides.webm'].pages === 2, 'VP8 in WebM gives its 2 pages (works in every browser)',
    outcomes['slides.webm'].summary);
  ok(outcomes['slides.mkv'].pages === 2, 'VP8 in Matroska gives its 2 pages',
    outcomes['slides.mkv'].summary);
  ok(outcomes['fake.mp4'].step === 'pick', 'a text file named .mp4 is refused, not treated as a video');
  ok(outcomes['broken.mp4'].step === 'pick', 'a half-downloaded mp4 is refused');
  const hevcOutcome = outcomes['hevc.mp4'].step === 'review' ? 'decoded' : 'refused with a clear message';
  note(`HEVC/H.265 in MP4: ${hevcOutcome} (this browser ${support.mp4 ? 'does' : 'does not'} support H.264)`);

  // ---- [7] the PDF that actually lands ------------------------------------ //
  section('[7] the PDF that actually downloads');
  if (!canH264) {
    skip('the PDF checks use colours.mp4 (H.264), which this browser cannot decode');
  } else {
  await backToPick(page);
  const orderRun = await chooseFile(page, join(MEDIA, 'colours.mp4'));
  ok(orderRun.pages === 3, 'the colour video is on screen as 3 pages', orderRun.summary);


  await setTitle(page, 'Biology Chapter 6');
  const filenameLine = await page.$eval('#filename', (el) => el.textContent);
  ok(/Biology Chapter 6\.pdf/.test(filenameLine), 'the page says which name it will save as', filenameLine);

  const first = await downloadOnce('first download');
  ok(first.name === 'Biology Chapter 6.pdf', 'the file is saved under that name', first.name);
  const saveNote = await page.$eval('#save-note', (el) => ({
    shown: getComputedStyle(el).display !== 'none', text: el.textContent,
  }));
  ok(saveNote.shown && /Biology Chapter 6\.pdf/.test(saveNote.text),
    'the page tells you what it asked the browser to save', saveNote.text.slice(0, 80));
  ok(/Downloads|Save to Files/.test(saveNote.text),
    'and where to find it on a phone (untested here, but the guidance is on the page)');
  ok(first.bytes > 10000, 'the downloaded file has contents', `${first.bytes} bytes`);
  const probed = probePdf(first.file);
  note(`read back with ${probed.how}`);
  ok(probed.header.startsWith('%PDF'), 'it is a PDF', probed.header);
  ok(probed.pageCount === 3, 'it has one page per captured page', `${probed.pageCount} pages`);
  ok(probed.pages.filter((p) => p.images && p.images.length).length === 3,
    'every page carries one picture');
  ok(probed.pages.every((p) => !p.images || p.images.every((i) => i.jpeg)),
    'the pictures inside are JPEGs');
  const order = probed.pages.map(dominantColour);
  note(`page colours, in order: ${order.join(', ')}`);
  ok(order.join(',') === 'red,green,blue', 'the pages are in the order they were shown', order.join(','));

  // deselect the middle page: the PDF must follow the ticks, in order
  await page.click('#pages .page:nth-child(2) .tick');
  const twoKept = await page.evaluate(STATE);
  ok(/2 pages/.test(twoKept.download), 'the button says two pages are selected', twoKept.download);
  const second = await downloadOnce('second download');
  const probed2 = probePdf(second.file);
  const order2 = probed2.pages.map(dominantColour);
  note(`after unticking the middle page: ${probed2.pageCount} pages, colours ${order2.join(', ')}`);
  ok(probed2.pageCount === 2, 'unticking a page removes it from the PDF', `${probed2.pageCount} pages`);
  ok(order2.join(',') === 'red,blue', 'the remaining pages keep their order', order2.join(','));

  // a title written in Hindi has to survive into the filename
  await setTitle(page, 'अध्याय ६');
  const third = await downloadOnce('Hindi download');
  ok(third.name === 'अध्याय ६.pdf', 'a Hindi title is used for the filename', third.name);
  ok(third.file.endsWith('अध्याय ६.pdf'),
    'and the file really lands under that name, not as "download" or notes.pdf',
    third.file.split('/').pop());
  ok(probePdf(third.file).pageCount === 2, 'the Hindi download is a usable PDF too');

  // A download the browser refuses (some do, for a programmatic click). The app must
  // say the PDF could not be written rather than leaving the visitor thinking it saved.
  await page.evaluate(() => {
    window.__realAnchorClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { throw new Error('blocked by the browser'); };
  });
  const beforeFailure = suggestedNames.length;
  await page.click('#download');
  await waitFor(page, () => /could not be written/.test(document.getElementById('save-note').textContent),
    { timeout: 15000, label: 'the write failure to be reported' });
  const failureState = await page.evaluate(() => ({
    note: document.getElementById('save-note').textContent,
    shown: getComputedStyle(document.getElementById('save-note')).display !== 'none',
    button: { disabled: document.getElementById('download').disabled,
      text: document.getElementById('download').textContent },
  }));
  note(`write failure -> "${failureState.note}"`);
  ok(failureState.shown && /blocked by the browser/.test(failureState.note),
    'a PDF that could not be written says so, with the reason', failureState.note);
  ok(!/Asked the browser to save/.test(failureState.note),
    'and it does not claim to have saved anything');
  ok(!failureState.button.disabled && /Download the PDF/.test(failureState.button.text),
    'the button comes back so the visitor can try again', JSON.stringify(failureState.button));
  ok(suggestedNames.length === beforeFailure, 'nothing was downloaded');
  await page.evaluate(() => { HTMLAnchorElement.prototype.click = window.__realAnchorClick; });

  await page.click('#none');
  const noneState = await page.evaluate(STATE);
  ok(noneState.downloadDisabled && /Tick at least one page/.test(noneState.download),
    'with nothing ticked the download is disabled', noneState.download);
  const beforeNone = suggestedNames.length;
  await page.click('#download');
  await wait(1200);
  ok(suggestedNames.length === beforeNone, 'clicking it anyway downloads nothing');
  await page.click('#all');
  }

  // ---- [8] what can be checked about phones ------------------------------- //
  section('[8] phone-shaped checks (a proxy, not Safari)');
  const attrs = await page.evaluate(() => {
    const v = document.getElementById('video');
    return { playsInline: v.playsInline, muted: v.muted, preload: v.preload, controls: v.controls };
  });
  ok(attrs.playsInline === true, 'the video is playsinline (iOS plays it inside the page)');
  ok(attrs.muted === true, 'the video is muted (iOS needs this to decode without a tap)');
  ok(attrs.preload === 'auto', 'the video preloads');
  ok(attrs.controls === false, 'no player controls are shown for a video the visitor never sees');

  await ensureReview(page, join(MEDIA, 'slides.webm'));
  const reviewLayout = await page.evaluate(() => ({
    width: window.innerWidth,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    downloadHeight: Math.round(document.getElementById('download').getBoundingClientRect().height),
    startOverHeight: Math.round(document.getElementById('startover').getBoundingClientRect().height),
  }));
  note(`review at ${reviewLayout.width}px: overflow ${reviewLayout.overflow}px, `
    + `download ${reviewLayout.downloadHeight}px, start over ${reviewLayout.startOverHeight}px`);
  ok(reviewLayout.overflow <= 1, 'the review screen does not scroll sideways on a 390px phone');
  ok(reviewLayout.downloadHeight >= 44 && reviewLayout.startOverHeight >= 44,
    'the review controls are thumb-sized (>=44px)');

  await page.click('#pages .page:nth-child(1) .shot');
  const closeBox = await page.evaluate(() => {
    const rect = document.getElementById('lb-close').getBoundingClientRect();
    return { inside: rect.top >= 0 && rect.left >= 0 && rect.right <= window.innerWidth && rect.bottom <= window.innerHeight,
      size: Math.round(rect.width) };
  });
  ok(closeBox.inside && closeBox.size >= 44, 'the viewer closes with a reachable 44px button', JSON.stringify(closeBox));
  await page.keyboard.press('Escape');
  await page.click('#startover');
  const pickLayout = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    focusOnDrop: document.activeElement.id,
    // measured now, because the pick card is hidden while the review screen is up
    dropHeight: Math.round(document.getElementById('drop').getBoundingClientRect().height),
    sampleHeight: Math.round(document.getElementById('sample').getBoundingClientRect().height),
    formats: document.getElementById('formats').textContent.length,
  }));
  note(`pick at 390px: overflow ${pickLayout.overflow}px, drop ${pickLayout.dropHeight}px, `
    + `sample button ${pickLayout.sampleHeight}px`);
  ok(pickLayout.overflow <= 1, 'the pick screen does not scroll sideways either',
    `${pickLayout.overflow}px`);
  ok(pickLayout.dropHeight >= 88 && pickLayout.sampleHeight >= 44,
    'the drop area and the sample button are big enough to tap');
  ok(pickLayout.formats > 10, 'the format message is really filled in');
  ok(pickLayout.focusOnDrop === 'drop', 'keyboard focus lands on the drop area after Start over',
    pickLayout.focusOnDrop);

  // ---- [9] the single-file build ------------------------------------------ //
  section('[9] the offline single file (pagesnap-offline.html)');
  const offline = await openApp(browser, base, { file: 'pagesnap-offline.html', viewport: { width: 390, height: 844 } });
  ok(offline.errors.length === 0, 'it loads without JavaScript errors', offline.errors.join(' | '));
  const offlineBits = await offline.page.evaluate(() => ({
    sampleButton: !!document.getElementById('sample'),
    errorBox: !!document.getElementById('err'),
    reviewCard: !!document.getElementById('step-review'),
    note: document.body.innerText.includes('This is the offline copy'),
  }));
  ok(!offlineBits.sampleButton && offlineBits.note, 'the sample button is replaced by the offline note');
  ok(offlineBits.errorBox && offlineBits.reviewCard, 'the error box and review card are still there');
  const offlineInput = await offline.page.$('#file');
  await offlineInput.uploadFile(join(MEDIA, 'slides.webm'));
  await waitFor(offline.page, SETTLED, { timeout: 120000, label: 'the offline file to finish a job' });
  const offlineState = await offline.page.evaluate(STATE);
  ok(offlineState.step === 'review' && offlineState.pages === 2,
    'it finds the pages and shows them', JSON.stringify(offlineState).slice(0, 120));
  ok(offline.errors.length === 0, 'still no JavaScript errors after a job', offline.errors.join(' | '));
  await offline.page.close();

  // ---- [11] the PDF box, the capture box and the link box ---------------- //
  // Everything below is a control a person can actually reach, so each check clicks
  // the real button and reads what the page then says or does.
  section('[11] the PDF box: order, removal, and the paper');

  const backToColours = async () => {
    await backToPick(page);
    await chooseFile(page, join(MEDIA, 'colours.mp4'));
    await waitFor(page, SETTLED, { timeout: 120000, label: 'the colour video to finish' });
    return page.evaluate(STATE);
  };
  let colourState = await backToColours();
  if (colourState.step !== 'review') {
    note('skipping the PDF-box checks: the colour video did not decode here');
  } else {
    const count = () => page.$eval('#export-count', (el) => el.textContent);
    const rows = () => page.$$eval('#export-list .exrow', (els) => els.length);
    const orderOnScreen = () => page.$$eval('#pages .meta span:last-child',
      (els) => els.map((el) => el.textContent));

    ok(/3 of 3 pictures selected/.test(await count()),
      'the box counts the selected pictures', await count());
    ok((await rows()) === 3, 'and lists one row per picture', String(await rows()));

    // moving picture 1 later must change the gallery too - the gallery's numbers are
    // the PDF's page numbers
    const before = await orderOnScreen();
    await page.click('#export-list .exrow:nth-child(1) .exm[data-act="down"]');
    const after = await orderOnScreen();
    ok(before[0] === after[1] && before[1] === after[0],
      'moving a picture down reorders the gallery with it', `${before} -> ${after}`);
    ok(/3 of 3 pictures selected/.test(await count()),
      'and the count is unchanged by reordering', await count());
    await page.click('#export-list .exrow:nth-child(2) .exm[data-act="up"]');
    const restored = await orderOnScreen();
    ok(restored.join(',') === before.join(','), 'and moving it back restores the order',
      restored.join(','));

    // removal is explicit, counted, and the PDF box keeps up
    await page.click('#export-list .exrow:nth-child(3) .exm[data-act="remove"]');
    const removed = await page.evaluate(STATE);
    ok(removed.pages === 2, 'removing a picture takes it out of the gallery', String(removed.pages));
    ok(/2 of 2 pictures selected/.test(await count()),
      'and the box counts what is left', await count());
    ok(/Removed that picture/.test(await page.$eval('#export-note', (el) => el.textContent)),
      'and says so rather than doing it silently');

    // paper and orientation really reach the file
    await page.select('#pagesize', 'letter');
    await page.select('#orient', 'portrait');
    const letterDownload = await downloadOnce('letter-portrait');
    const letterProbe = probePdf(letterDownload.file);
    note(`letter/portrait read back with ${letterProbe.how}`);
    ok(letterProbe.pageCount === 2, 'the exported PDF has the pages that were selected',
      String(letterProbe.pageCount));
    const letterBoxes = (letterProbe.pages || []).map((pg) => pg.mediabox).filter(Boolean);
    ok(letterBoxes.length === 0 || letterBoxes.every(([w, h]) => Math.round(h) === 792
      && Math.round(w) === 612), 'every page is Letter portrait', JSON.stringify(letterBoxes));
    await page.select('#pagesize', 'a4');
    await page.select('#orient', 'auto');

    section('[12] capture a frame, and replace one picture');
    ok(await page.$eval('#capture-box', (el) => getComputedStyle(el).display !== 'none'),
      'the capture box is there after a job');

    await page.click('#cap-use-loaded');
    await page.$eval('#cap-time', (el) => {
      el.value = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    // a time past the end must be refused with the length in the message
    await page.$eval('#cap-time', (el) => {
      el.value = '99:00.000';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#cap-preview');
    await wait(page, 400);
    const pastEnd = await page.$eval('#cap-status', (el) => el.textContent);
    note(`past the end -> "${pastEnd}"`);
    ok(/past the end/.test(pastEnd), 'a time past the end is refused', pastEnd);

    // a real time: preview, then capture
    const beforeCapture = (await page.evaluate(STATE)).pages;
    // colours.mp4 runs 6 s, so the time has to be inside that or the app is right to
    // refuse it - which is exactly what the previous check just proved.
    await page.$eval('#cap-time', (el) => {
      el.value = '0:02.500';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#cap-preview');
    await waitFor(page, () => document.getElementById('cap-stage').style.display !== 'none',
      { timeout: 30000, label: 'the frame preview' });
    const preview = await page.$eval('#cap-img', (el) => ({ src: el.src, alt: el.alt }));
    ok(preview.src.startsWith('data:image/jpeg'),
      'the preview shows the frame that would be captured', preview.src.slice(0, 30));
    ok(/0:02\.500|\.500/.test(preview.alt), 'and says which moment it is', preview.alt);

    await page.click('#cap-go');
    // waitFor's condition cannot take an argument (it is evaluated in the page), so
    // this one waits on the real count with the expected number passed in.
    await page.waitForFunction(
      (n) => document.getElementById('pages').children.length === n,
      { timeout: 60000, polling: 250 }, beforeCapture + 1);
    const afterCapture = await page.evaluate(STATE);
    ok(afterCapture.pages === beforeCapture + 1,
      'capturing adds the frame to the gallery', `${beforeCapture} -> ${afterCapture.pages}`);
    ok(/Captured the frame/.test(await page.$eval('#cap-status', (el) => el.textContent)),
      'and the page says what it did');

    // replace: only the chosen picture changes, and the others keep their places
    const shotsBefore = await page.$$eval('#pages .page .shot img', (els) => els.map((e) => e.src));
    await page.click('#export-list .exrow:nth-child(1) .exm[data-act="choose"]');
    await page.$eval('#cap-time', (el) => {
      el.value = '0:05.000';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#cap-preview');
    await waitFor(page, () => document.getElementById('cap-stage').style.display !== 'none',
      { timeout: 30000, label: 'the replacement preview' });
    await page.click('#cap-replace');
    await waitFor(page, () => document.getElementById('cap-undo').style.display !== 'none',
      { timeout: 60000, label: 'the replacement to be applied' });
    const shotsAfter = await page.$$eval('#pages .page .shot img', (els) => els.map((e) => e.src));
    ok(shotsAfter.length === shotsBefore.length, 'replacing does not change how many pictures there are');
    ok(shotsAfter[0] !== shotsBefore[0], 'the chosen picture really changed');
    ok(shotsAfter.slice(1).join('|') === shotsBefore.slice(1).join('|'),
      'and every other picture is exactly as it was');
    ok(/is now the frame/.test(await page.$eval('#cap-status', (el) => el.textContent)),
      'the page says which picture it swapped');

    await page.click('#cap-undo');
    await wait(page, 600);
    const shotsUndone = await page.$$eval('#pages .page .shot img', (els) => els.map((e) => e.src));
    ok(shotsUndone[0] === shotsBefore[0], 'undo puts the original picture back');
    ok(shotsUndone.length === shotsBefore.length, 'and nothing else moved');

    section('[13] the link box tells the truth about a social link');
    await backToPick(page);
    await page.$eval('#link', (el) => {
      el.value = 'https://youtu.be/dQw4w9WgXcQ';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#link-check');
    const youTube = await page.$eval('#link-out', (el) => el.textContent);
    note(`a YouTube link -> "${youTube.slice(0, 90)}…"`);
    ok(/YouTube video/.test(youTube), 'it recognises a YouTube video', youTube.slice(0, 60));
    ok(/cannot fetch it/.test(youTube), 'and says this page cannot fetch it');
    ok(/frames, not a set of post photographs/.test(youTube),
      'and explains that a video yields frames, not post photos');

    await page.$eval('#link', (el) => {
      el.value = 'https://www.instagram.com/p/Cabcdefghij/';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#link-check');
    const insta = await page.$eval('#link-out', (el) => el.textContent);
    ok(/Instagram photo post or carousel/.test(insta), 'it recognises an Instagram post', insta.slice(0, 60));
    ok(/Save each picture|Save the post/.test(insta) || /Save/.test(insta),
      'and offers the alternative that works');

    await page.$eval('#link', (el) => {
      el.value = 'javascript:alert(1)';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('#link-check');
    const nasty = await page.$eval('#link-out', (el) => el.textContent);
    ok(/cannot be used/.test(nasty), 'a javascript: URL is refused', nasty.slice(0, 60));
    ok(!/alert\(1\)/.test(await page.$eval('#link-out', (el) => el.innerHTML)),
      'and nothing from it is injected into the page');
  }

  // ---- [10] overall ------------------------------------------------------- //
  section('[10] everything the app logged');
  if (errors.length) {
    note(`${errors.length} console/page error(s), all shown below so none is hidden:`);
    for (const line of errors) console.log(`         ${line}`);
  }
  ok(errors.length === 0, 'no JavaScript errors during the whole session', `${errors.length} total`);

  await browser.close();
  server.close();
  await rm(downloads, { recursive: true, force: true });

  console.log();
  if (failures) console.log(`${checks} checks, ${failures} failure(s).`);
  else console.log(`${checks} checks, 0 failures${skipped ? ` (${skipped} skipped)` : ''}.`);
  process.exit(failures ? 1 : 0);
}

main().catch(async (error) => {
  console.error(`\nThe suite could not finish: ${error.message}\n`);
  process.exit(1);
});
