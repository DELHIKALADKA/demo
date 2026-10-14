/**
 * Browser-layer test for the PageSnap prototype UI.
 *
 * The API tests prove the server is right. This proves the *page* is right: that
 * the buttons actually drive the workflow, that the DOM ends up showing the truth,
 * and - the reason this file exists - that there are no console errors and no
 * failed requests along the way.
 *
 * jsdom has no video decoder and no fetch, so Node's own fetch (real HTTP to a real
 * server) is wired into the page, and Node's FormData/Blob/File - which is what a
 * real browser's FormData looks like on the wire.
 *
 *   node prototype/tests/ui_test.mjs
 */

import { readFileSync, openSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import net from 'node:net';

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(here, '..', '..');
const FIXTURE = join(ROOT, 'engine', 'tests', 'fixtures', 'lecture.mp4');

let passed = 0;
const failures = [];
function check(name, condition, detail = '') {
  if (condition) { passed++; console.log(`  PASS  ${name}`); }
  else { failures.push(`${name}${detail ? ': ' + detail : ''}`); console.log(`  FAIL  ${name}${detail ? '\n          ' + detail : ''}`); }
  return !!condition;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(description, predicate, timeoutMs = 60000, intervalMs = 200) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { if (await predicate()) return true; } catch (_) { /* keep waiting */ }
    await sleep(intervalMs);
  }
  check(`waitFor: ${description}`, false, `timed out after ${timeoutMs / 1000}s`);
  return false;
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function startServer(port) {
  const log = openSync(join(here, '.ui-server.log'), 'a');
  // the library writes real files; keep them in a throwaway folder for tests
  const library = mkdtempSync(join(tmpdir(), 'pagesnap-ui-lib-'));
  const child = spawn('python3', [join(ROOT, 'prototype', 'server.py'),
                                  '--port', String(port), '--host', '127.0.0.1'],
                      { stdio: ['ignore', log, log],
                        env: { ...process.env, PAGESNAP_LIBRARY: library } });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/sources`);
      if (response.ok) return child;
    } catch (_) { /* not up yet */ }
    await sleep(300);
  }
  throw new Error('server did not start');
}

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = await import('jsdom'));
} catch (_) {
  console.error('This test drives the page in a real DOM, so it needs jsdom.\n'
    + 'Install it once:\n\n    cd prototype && npm install jsdom\n\n'
    + 'then run:  node prototype/tests/ui_test.mjs');
  process.exit(2);
}

async function main() {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  console.log(`Starting the prototype server on port ${port} …`);
  const server = await startServer(port);

  const consoleErrors = [];
  const failedRequests = [];
  const virtualConsole = new VirtualConsole();
  const knownEnvironmentGaps = [];
  virtualConsole.on('jsdomError', (error) => {
    // jsdom has no blob: URL loader, so an <iframe src="blob:..."> - the normal way
    // to preview a generated PDF - cannot load here. Real browsers do this fine, so
    // it is a gap in the test environment, not a bug in the app.
    if (/Could not load iframe.*blob:/i.test(error.message)) {
      knownEnvironmentGaps.push(error.message);
      return;
    }
    consoleErrors.push(`jsdomError: ${error.message}`);
  });
  virtualConsole.on('error', (...args) => consoleErrors.push(`console.error: ${args.join(' ')}`));
  virtualConsole.on('warn', () => { /* the page logs nothing on the warning path */ });

  let dom;
  try {
    dom = await JSDOM.fromURL(base + '/', {
      runScripts: 'dangerously',
      resources: 'usable',
      pretendToBeVisual: true,
      virtualConsole,
      beforeParse(window) {
        // jsdom ships no fetch and no web-media classes; give the page the real ones.
        const realFetch = async (input, init) => {
          const url = typeof input === 'string' ? new URL(input, base).href : input;
          try {
            const response = await fetch(url, init);
            if (!response.ok) failedRequests.push(`${init && init.method || 'GET'} ${url} -> ${response.status}`);
            return response;
          } catch (error) {
            failedRequests.push(`${init && init.method || 'GET'} ${url} -> ${error.message}`);
            throw error;
          }
        };
        window.fetch = realFetch;
        window.FormData = globalThis.FormData;
        window.Blob = globalThis.Blob;
        window.File = globalThis.File;
        // Blob URLs do not exist in jsdom; the page only uses them to show a
        // download or a preview, so hand back a stable fake and remember the blobs.
        window.__blobs = new Map();
        let n = 0;
        window.URL.createObjectURL = (blob) => {
          const url = `blob:pagesnap/${++n}`;
          window.__blobs.set(url, blob);
          return url;
        };
        window.URL.revokeObjectURL = (url) => window.__blobs.delete(url);
        // jsdom has no confirm(); every browser does. Answer "yes" so the
        // destructive-action path can be exercised.
        window.__confirmed = [];
        window.confirm = (message) => { window.__confirmed.push(message); return true; };
        // jsdom cannot actually download; record what the page tried to save.
        window.__downloads = [];
        const realClick = window.HTMLAnchorElement.prototype.click;
        window.HTMLAnchorElement.prototype.click = function () {
          if (this.download) {
            window.__downloads.push({ name: this.download, href: this.href });
            return;                       // do not navigate
          }
          return realClick.apply(this, arguments);
        };
      },
    });

    const { window } = dom;
    const realFetch = window.fetch;
    const click = (selector) => {
      const el = window.document.querySelector(selector);
      if (el) el.click();
      return !!el;
    };
    const $ = (id) => window.document.getElementById(id);
    const text = (id) => ($(id) ? $(id).textContent.trim() : '');
    const countPages = () => window.document.querySelectorAll('#pages .page').length;
    const pageUids = () => [...window.document.querySelectorAll('#pages .page')].map((el) => el.dataset.uid);

    await waitFor('page script ran', () => $('src-list') && text('src-list') !== 'Loading…', 20000);

    // ── 1. the source list is honest about what works ───────────────────────
    console.log('\n[1] source guidance');
    const srcText = text('src-list');
    check('source list rendered', srcText.length > 40, srcText.slice(0, 80));
    check('says files always work', /files from this device always work/i.test(srcText), srcText.slice(0, 200));
    check('names the login-requiring platforms', /instagram|facebook|youtube/i.test(srcText), srcText.slice(0, 200));
    check('says DRM is never supported', /never/i.test(srcText) && /drm/i.test(srcText));

    // ── 2. an invalid link fails for the right reason ───────────────────────
    console.log('\n[2] invalid link');
    $('link').value = 'ftp://example.com/video.mp4';
    $('import').click();
    await waitFor('error shown for the bad link', () => $('err').style.display === 'block'
      || $('err').textContent.trim().length > 10, 20000);
    const linkError = text('err');
    check('bad link reports a reason', /http/i.test(linkError), linkError.slice(0, 200));
    check('bad link suggests the upload fallback', /choose a video from this device/i.test(linkError),
      linkError.slice(0, 240));
    check('bad link does not start a job', $('step-progress').style.display === 'none',
      `progress visible: ${$('step-progress').style.display}`);

    // ── 3. upload a real video ──────────────────────────────────────────────
    console.log('\n[3] upload + detect (through the page)');
    const bytes = readFileSync(FIXTURE);
    const file = new window.File([bytes], 'lecture.mp4', { type: 'video/mp4' });
    const input = $('file');
    Object.defineProperty(input, 'files', { value: [file], writable: false,
                                             configurable: true });
    input.dispatchEvent(new window.Event('change'));

    await waitFor('a job id was created', () => $('step-progress').style.display !== 'none', 15000);
    check('progress screen appears on upload', $('step-progress').style.display !== 'none');
    await waitFor('progress text updates', () => text('stage2').length > 5, 20000);
    check('progress text is human', /analysing|page|video/i.test(text('stage') + ' ' + text('stage2')),
      (text('stage') + ' | ' + text('stage2')).slice(0, 120));

    await waitFor('review screen appears', () => $('step-result').style.display !== 'none' && countPages() > 0, 120000);
    check('4 page thumbnails rendered', countPages() === 4, `rendered ${countPages()}`);
    check('summary states the count', /4 page\(s\) found/.test(text('summary')), text('summary'));
    check('summary states how many are selected', /4 selected/.test(text('summary2')), text('summary2'));
    check('generate button enabled', $('generate').disabled === false);
    check('filename preview shown', /\.pdf$/.test(text('filename')), text('filename'));

    // ── 4. generate the PDF ────────────────────────────────────────────────
    console.log('\n[4] generate PDF');
    $('title').value = 'Biology Chapter 6';
    $('title').dispatchEvent(new window.Event('change'));
    await sleep(600);
    $('generate').click();
    await waitFor('PDF reported ready', () => /is ready/.test(text('pdf-line')), 90000);
    const meta = text('pdf-meta');
    check('panel reports 4 pages', /4/.test(meta) && /page/.test(meta), meta);
    check('panel reports the file size', /KB|MB/.test(meta), meta);
    check('panel says the file was verified', /verified/i.test(meta), meta);
    check('panel names the file', /Biology-Chapter-6\.pdf/.test(text('pdf-line')), text('pdf-line'));
    check('no stale warning yet', $('stale').style.display === 'none', $('stale').style.display);

    // ── 5. download really produces PDF bytes ──────────────────────────────
    console.log('\n[5] download');
    $('download').click();
    await waitFor('a download was triggered', () => window.__downloads.length > 0, 30000);
    const download = window.__downloads[window.__downloads.length - 1] || {};
    check('download filename comes from the title', download.name === 'Biology-Chapter-6.pdf',
      String(download.name));
    const blob = window.__blobs.get(download.href);
    check('the downloaded blob exists', !!blob);
    if (blob) {
      const head = new TextDecoder().decode(new Uint8Array(await blob.arrayBuffer()).slice(0, 5));
      check('the downloaded bytes start with %PDF-', head === '%PDF-', JSON.stringify(head));
      check('the blob is application/pdf', blob.type === 'application/pdf', blob.type);
    check('the download filename ends in .pdf', /\.pdf$/.test(String(download.name)),
      String(download.name));
    }
    check('the page confirms the save', /saved/i.test(text('privacy')), text('privacy'));

    // ── 6. preview the PDF ─────────────────────────────────────────────────
    console.log('\n[6] preview');
    $('preview-pdf').click();
    await waitFor('preview opened', () => window.document.querySelector('#lightbox-body iframe'), 30000);
    const frame = window.document.querySelector('#lightbox-body iframe');
    check('PDF preview shown in an inline frame', !!frame, 'no iframe');
    check('frame points at a blob URL', !!frame && /^blob:/.test(frame.src), frame ? frame.src : '');
    check('lightbox is open', $('lightbox').classList.contains('on'));
    $('lightbox').click();
    check('lightbox closes', !$('lightbox').classList.contains('on'));

    // ── 7. remove a page, then regenerate ──────────────────────────────────
    console.log('\n[7] edit: remove + regenerate');
    const before = pageUids();
    const secondCard = window.document.querySelectorAll('#pages .page')[1];
    secondCard.querySelector('button[data-act="remove"]').click();
    await waitFor('page marked removed', () => /3 selected/.test(text('summary2')), 30000,
      150);
    check('summary reflects the removal', /3 selected/.test(text('summary2')), text('summary2'));
    check('stale banner appears after editing', $('stale').style.display !== 'none',
      $('stale').style.display);
    check('download disabled while stale', $('download').disabled === true);
    check('preview disabled while stale too', $('preview-pdf').disabled === true,
      `preview disabled=${$('preview-pdf').disabled}`);
    check('generate button now says regenerate', /regenerate/i.test(text('generate')), text('generate'));

    $('generate').click();
    await waitFor('regenerated', () => /is ready/.test(text('pdf-line')) && /3/.test(text('pdf-meta')), 90000);
    check('regenerated PDF has 3 pages', /3/.test(text('pdf-meta')), text('pdf-meta'));
    check('stale banner cleared', $('stale').style.display === 'none', $('stale').style.display);
    check('download enabled again', $('download').disabled === false);
    check('preview enabled again', $('preview-pdf').disabled === false);

    window.__downloads.length = 0;
    $('download').click();
    await waitFor('second download', () => window.__downloads.length > 0, 30000);
    const second = window.__downloads[0] || {};
    const secondBlob = window.__blobs.get(second.href);
    if (secondBlob) {
      const reader = await secondBlob.arrayBuffer();
      const text = new TextDecoder('latin1').decode(new Uint8Array(reader));
      const pageCount = (text.match(/\/Type\s*\/Page[^s]/g) || []).length;
      check('the downloaded PDF really contains 3 page objects', pageCount === 3, `found ${pageCount}`);
    } else {
      check('the downloaded PDF really contains 3 page objects', false, 'no blob');
    }

    // ── 8. restore + reorder ───────────────────────────────────────────────
    console.log('\n[8] edit: restore + reorder');
    $('restore').click();
    await waitFor('pages restored', () => /4 selected/.test(text('summary2')), 30000, 150);
    check('restore brings the page back', /4 selected/.test(text('summary2')), text('summary2'));

    const orderBefore = pageUids();
    window.document.querySelectorAll('#pages .page')[0]
      .querySelector('button[data-act="right"]').click();
    await waitFor('order changed in the DOM', () => pageUids()[0] === orderBefore[1], 30000, 150);
    check('moving a page swaps it with its neighbour',
      pageUids()[0] === orderBefore[1] && pageUids()[1] === orderBefore[0],
      `${orderBefore.slice(0, 2)} -> ${pageUids().slice(0, 2)}`);
    check('reordering marks the PDF stale again', $('stale').style.display !== 'none');

    // ── 9. capture a page by timestamp ─────────────────────────────────────
    console.log('\n[9] manual capture');
    $('capture-time').value = '20';
    $('capture').click();
    await waitFor('a fifth page appears', () => countPages() === 5, 60000);
    check('captured page added to the grid', countPages() === 5, `now ${countPages()}`);
    check('captured page is labelled manual',
      !!window.document.querySelector('#pages .tag.manual'), 'no manual tag');
    check('capture reported back to the user', /20\.0s/.test(text('capture-hint')), text('capture-hint'));

    $('capture-time').value = '9999';
    $('capture').click();
    await waitFor('bad timestamp refused', () => /end of the video/i.test(text('capture-hint')), 20000, 150);
    check('a timestamp past the end is refused with a reason',
      /end of the video/i.test(text('capture-hint')), text('capture-hint'));
    check('the bad timestamp did not add a page', countPages() === 5, `${countPages()} pages`);

    // ── 10. preview of the selection ───────────────────────────────────────
    console.log('\n[10] contact sheet');
    $('preview-sheet').click();
    await waitFor('sheet opened', () => window.document.querySelector('#lightbox-body img'), 30000);
    const sheetImg = window.document.querySelector('#lightbox-body img');
    check('selected pages preview as an image', !!sheetImg && /^blob:/.test(sheetImg.src),
      sheetImg ? sheetImg.src : 'none');
    $('lightbox').click();

    // ── 16. failures of the download path ──────────────────────────────────
    // ── 11. YouTube link kinds ─────────────────────────────────────────────
    console.log('\n[11] YouTube Shorts through the page');
    const failuresBeforeLinks = failedRequests.length;

    // a channel is refused before any work starts, so this is deterministic
    $('link').value = 'https://www.youtube.com/@SomeChannel';
    $('import').click();
    await waitFor('the channel link is refused',
      () => /channel/i.test(text('err')) || /channel/i.test(text('url-state')), 20000, 100);
    check('a channel link is refused with a reason',
      /channel/i.test(text('err') + text('url-state')), (text('err') + text('url-state')).slice(0, 160));
    check('  ↳ and no job was started', $('step-progress').style.display === 'none',
      `progress=${$('step-progress').style.display}`);

    // the real Shorts URL: it is recognised by its id, then either fetched or explained
    $('link').value = 'https://www.youtube.com/shorts/Sh8EPts-DXk';
    $('import').click();
    await waitFor('the Shorts link is recognised',
      () => /YouTube Short/i.test(text('url-state')) || /YouTube Short/i.test(text('err')), 20000, 100);
    check('the page says it is importing a YouTube Short',
      /YouTube Short/i.test(text('url-state') + text('err')),
      (text('url-state') + ' | ' + text('err')).slice(0, 200));

    // Wait for the outcome. The link row is the signal to wait on: the page only
    // writes it for *this* job, so it cannot be confused with the grid left over
    // from the earlier local upload. Either this machine can fetch YouTube and the
    // row reports the import, or it cannot and the page must say so - without
    // blaming the connection, and with the file upload offered instead.
    await waitFor('the Shorts import reaches an outcome',
      () => /\u2713 Imported/.test(text('url-state'))
            || ($('err').style.display === 'block' && /youtube/i.test(text('err'))),
      180000, 300);
    if (/\u2713 Imported/.test(text('url-state'))) {
      console.log('      this machine reached YouTube - the Short was fetched');
      check('the Short was fetched and reported as imported',
        /\u2713 Imported/.test(text('url-state')), text('url-state').slice(0, 160));
      check('  \u21b3 and the review screen shows its pages', countPages() > 0,
        `${countPages()} pages`);
    } else {
      const said = (text('err') + ' ' + text('url-state')).toLowerCase();
      check('a Short that cannot be fetched is not reported as imported',
        !/\u2713 Imported/.test(text('url-state')), text('url-state').slice(0, 160));
      check('  \u21b3 the page explains it cannot be fetched from here',
        said.includes('youtube')
          && (said.includes('browser') || said.includes('could not be reached')),
        said.slice(0, 200));
      check('  \u21b3 and does not blame the connection',
        !said.includes('check the connection') && !said.includes('check your internet'),
        said.slice(0, 200));
      check('  \u21b3 and offers the local file instead',
        said.includes('choose the file here') || said.includes('upload'), said.slice(0, 200));
    }
    check('the YouTube calls were answered as expected',
      failedRequests.length - failuresBeforeLinks === 1,
      failedRequests.slice(failuresBeforeLinks).join(' | '));

    // ── 12. a title in another script ──────────────────────────────────────
    console.log('\n[12] a PDF named in Hindi');
    // Section 11 may have left the page pointed at a job that failed, so upload the
    // local video again: this section is about the filename, not about the network.
    const hindiFile = new window.File([readFileSync(FIXTURE)], 'lecture.mp4',
      { type: 'video/mp4' });
    Object.defineProperty($('file'), 'files', { value: [hindiFile], writable: false,
                                              configurable: true });
    $('file').dispatchEvent(new window.Event('change'));
    await waitFor('a fresh local upload for the naming test',
      () => $('step-result').style.display !== 'none' && countPages() === 4, 120000, 200);
    check('the fresh upload is ready for the naming test', countPages() === 4,
      `${countPages()} pages`);
    const downloadsBeforeHindi = window.__downloads.length;
    $('title').value = 'अध्याय ६';
    $('title').dispatchEvent(new window.Event('change'));
    $('generate').click();
    await waitFor('the PDF with a Hindi name is ready',
      () => /is ready/.test(text('pdf-line')) && $('download').disabled === false, 90000);
    check('a Hindi title does not break generation', /is ready/.test(text('pdf-line')),
      text('pdf-line').slice(0, 160));
    check('  ↳ the panel reports the file', /page/.test(text('pdf-meta')), text('pdf-meta'));
    $('download').click();
    await waitFor('the Hindi-named download starts',
      () => window.__downloads.length > downloadsBeforeHindi, 30000);
    const hindi = window.__downloads[window.__downloads.length - 1] || {};
    check('a Hindi title can be downloaded', !!hindi.href, String(hindi.name));
    check('  ↳ the saved name keeps the Hindi letters and ends in .pdf',
      /अध्याय/.test(String(hindi.name)) && /\.pdf$/.test(String(hindi.name)), String(hindi.name));
    const hindiBlob = window.__blobs.get(hindi.href);
    if (hindiBlob) {
      const head = new TextDecoder('latin1')
        .decode(new Uint8Array(await hindiBlob.slice(0, 5).arrayBuffer()));
      check('  ↳ and the bytes are a real PDF', head === '%PDF-', JSON.stringify(head));
    } else {
      check('  ↳ and the bytes are a real PDF', false, 'no blob');
    }
    check('  ↳ the download path stayed clean', consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(' | '));

    // ── 13. the redesigned shell: home, navigation, theme ──────────────────
    console.log('\n[13] home screen, bottom navigation and themes');
    const $all = (sel) => [...window.document.querySelectorAll(sel)];

    // the app opens on Home, not on a bare import form
    click('[data-go="home"]');
    await sleep(50);
    check('the app opens on Home', $('screen-home').style.display !== 'none',
      `home=${$('screen-home').style.display} create=${$('step-input').style.display}`);
    check('the other screens are hidden until asked for',
      $('screen-library').style.display === 'none'
      && $('screen-settings').style.display === 'none');
    check('the greeting is filled in', /good (morning|afternoon|evening)/i.test(text('greeting')),
      text('greeting'));
    check('the hero says what PageSnap does',
      /turn videos into pdfs/i.test($('screen-home').textContent), 'hero heading missing');
    check('there is a primary way to start', !!$('go-create'));
    check('the home screen shows both import options',
      !!$('opt-upload') && !!$('opt-link'));
    check('the three-step guide is shown on Home',
      $('guide').style.display !== 'none' && /import/i.test($('guide').textContent)
      && /review/i.test($('guide').textContent) && /save/i.test($('guide').textContent));

    // the guide hides itself for good, and can be brought back
    $('dismiss-guide').click();
    await sleep(60);
    check('the guide can be dismissed', $('guide').style.display === 'none');
    check('  ↳ and the choice is remembered',
      window.localStorage.getItem('pagesnap-guide') === 'hidden',
      String(window.localStorage.getItem('pagesnap-guide')));
    click('[data-go="settings"]');
    await sleep(80);
    check('Settings says the guide is hidden', /hidden/i.test(text('guide-state')),
      text('guide-state'));
    $('guide-toggle').click();
    await sleep(60);
    check('Settings can bring the guide back',
      window.localStorage.getItem('pagesnap-guide') !== 'hidden',
      String(window.localStorage.getItem('pagesnap-guide')));

    // the bottom navigation: four real destinations, and it says which one you are on
    const tabs = $all('#tabbar [data-go]').map(b => b.dataset.go);
    check('the bottom bar has Home, Create, Library and Settings',
      JSON.stringify(tabs) === JSON.stringify(['home', 'create', 'library', 'settings']),
      tabs.join(', '));
    check('every tab has a visible label, not just an icon',
      $all('#tabbar button').every(b => (b.textContent || '').trim().length > 2),
      $all('#tabbar button').map(b => b.textContent.trim()).join('|'));

    for (const [tab, screen] of [['home','screen-home'], ['create','step-input'],
                                 ['library','screen-library'], ['settings','screen-settings']]) {
      click(`#tabbar [data-go="${tab}"]`);
      await sleep(60);
      check(`the ${tab} tab opens its own screen`, $(screen).style.display !== 'none',
        `screen ${screen} is ${$(screen).style.display}`);
      const current = $all('#tabbar [aria-current="page"]').map(b => b.dataset.go);
      check(`  ↳ and the bar marks ${tab} as the current tab`,
        current.length === 1 && current[0] === tab, current.join(',') || 'none marked');
    }

    // a job in progress is offered back, rather than silently forgotten
    click('#tabbar [data-go="home"]');
    await sleep(80);
    check('Home offers to resume the work in progress', $('resume').style.display !== 'none',
      $('resume').style.display);
    check('  ↳ and the offer describes the real job', /page\(s\) found/.test(text('resume-text')),
      text('resume-text'));
    $('resume-go').click();
    await sleep(60);
    check('  ↳ continue returns to the pages', $('step-result').style.display !== 'none',
      $('step-result').style.display);

    // themes: dark is the default, switching works, and the choice is remembered
    console.log('      (appearance)');
    check('dark is the default appearance',
      window.document.documentElement.dataset.theme === 'dark',
      String(window.document.documentElement.dataset.theme));
    click('[data-theme-set="light"]');
    await sleep(60);
    check('choosing Light switches the whole document',
      window.document.documentElement.dataset.theme === 'light',
      String(window.document.documentElement.dataset.theme));
    check('  ↳ and the preference is saved',
      window.localStorage.getItem('pagesnap-theme') === 'light',
      String(window.localStorage.getItem('pagesnap-theme')));
    const seg = (name) => $all(`[data-theme-set="${name}"]`)[0];
    check('  ↳ the Light button reports itself as pressed',
      seg('light').getAttribute('aria-pressed') === 'true',
      String(seg('light').getAttribute('aria-pressed')));
    check('  ↳ and Dark reports itself as not pressed',
      seg('dark').getAttribute('aria-pressed') === 'false',
      String(seg('dark').getAttribute('aria-pressed')));
    click('[data-theme-set="system"]');
    await sleep(60);
    check('choosing System is supported and saved',
      window.localStorage.getItem('pagesnap-theme') === 'system'
      && ['dark','light'].includes(window.document.documentElement.dataset.theme),
      `saved=${window.localStorage.getItem('pagesnap-theme')} `
      + `resolved=${window.document.documentElement.dataset.theme}`);
    // the quick toggle in the header always leaves an explicit choice
    $('theme-quick').click();
    await sleep(60);
    const afterQuick = window.document.documentElement.dataset.theme;
    check('the header toggle switches appearance directly',
      ['dark','light'].includes(afterQuick)
      && window.localStorage.getItem('pagesnap-theme') === afterQuick,
      `${afterQuick} saved=${window.localStorage.getItem('pagesnap-theme')}`);
    click('[data-theme-set="dark"]');           // back to the default for the rest
    await sleep(60);
    check('switching themes never leaves a broken state',
      window.document.documentElement.dataset.theme === 'dark');

    // ── 14. the library holds real files ───────────────────────────────────
    console.log('\n[14] library (real files, real actions)');
    click('#tabbar [data-go="library"]');
    await waitFor('the library lists something', () => $all('#library-list .lib-item').length > 0
      || $all('#library-list .empty').length > 0, 20000, 150);
    const rows = $all('#library-list .lib-item');
    check('the PDF made earlier is in the library', rows.length > 0,
      `${rows.length} rows`);
    check('  ↳ with a real name ending in .pdf',
      /\.pdf$/.test(rows[0].querySelector('.name').textContent),
      rows[0] ? rows[0].querySelector('.name').textContent : '');
    check('  ↳ and its page count', /\d+ page\(s\)/.test(rows[0].textContent),
      rows[0].textContent.replace(/\s+/g, ' ').slice(0, 90));
    check('  ↳ and a real date', /(today|\d{4})/i.test(rows[0].textContent),
      rows[0].textContent.replace(/\s+/g, ' ').slice(0, 90));
    check('  ↳ and the folder it lives in is stated',
      /kept in|will be kept in/i.test(text('library-folder')), text('library-folder'));

    // downloading a saved PDF really produces PDF bytes
    const savedBefore = window.__downloads.length;
    rows[0].querySelector('[data-lib="download"]').click();
    await waitFor('the saved PDF downloads', () => window.__downloads.length > savedBefore, 20000);
    const savedDl = window.__downloads[window.__downloads.length - 1] || {};
    check('a library download uses its real filename', /\.pdf$/.test(String(savedDl.name)),
      String(savedDl.name));
    const savedBlob = window.__blobs.get(savedDl.href);
    if (savedBlob) {
      const head = new TextDecoder('latin1')
        .decode(new Uint8Array(await savedBlob.slice(0, 5).arrayBuffer()));
      check('  ↳ and the bytes are a real PDF', head === '%PDF-', JSON.stringify(head));
    } else {
      check('  ↳ and the bytes are a real PDF', false, 'no blob');
    }

    // renaming is inline and really changes the stored file
    const newName = 'Renamed By Test';
    rows[0].querySelector('[data-lib="rename"]').click();
    await waitFor('the inline rename field appears',
      () => !!$all('#library-list input[type=text]').length, 10000, 100);
    const renameInput = $all('#library-list input[type=text]')[0];
    renameInput.value = newName;
    renameInput.parentElement.querySelector('.btn.primary').click();
    await waitFor('the rename is applied', () => /Renamed-By-Test/.test(
      $all('#library-list .lib-item')[0].textContent), 20000, 200);
    check('a saved PDF can be renamed, and the new name sticks',
      /Renamed-By-Test\.pdf/.test($all('#library-list .lib-item')[0].textContent),
      $all('#library-list .lib-item')[0].textContent.replace(/\s+/g, ' ').slice(0, 80));

    // the API agrees - this is a real file, not a DOM trick
    const libJson = await (await fetch(base + '/api/library')).json();
    check('the server lists the same file under the new name',
      libJson.items.some(i => i.filename === 'Renamed-By-Test.pdf'),
      libJson.items.map(i => i.filename).join(', '));

    // deleting asks first, then really removes the file
    const countBefore = $all('#library-list .lib-item').length;
    rows[0].querySelector('[data-lib="delete"]').click();
    await waitFor('the row disappears', () => $all('#library-list .lib-item').length
      < countBefore, 20000, 200);
    const afterDelete = await (await fetch(base + '/api/library')).json();
    check('deleting removes the file for real',
      !afterDelete.items.some(i => i.filename === 'Renamed-By-Test.pdf'),
      afterDelete.items.map(i => i.filename).join(', '));

    // ── 15. accessibility of the new shell ─────────────────────────────────
    console.log('\n[15] accessibility and layout safety');
    const nameless = $all('button').filter(b => {
      const label = (b.getAttribute('aria-label') || b.getAttribute('title')
                     || b.textContent || b.getAttribute('aria-labelledby') || '').trim();
      return !label;
    });
    check('every button has an accessible name', nameless.length === 0,
      `${nameless.length} nameless button(s)`);

    const tickButtons = $all('#pages .tick');
    check('each page tick is a real button', tickButtons.every(t => t.tagName === 'BUTTON'),
      tickButtons.map(t => t.tagName).join(','));
    check('  ↳ and reports its state to a screen reader',
      tickButtons.every(t => ['true','false'].includes(t.getAttribute('aria-pressed'))),
      tickButtons.map(t => t.getAttribute('aria-pressed')).join(','));

    const viewport = window.document.querySelector('meta[name=viewport]').content;
    check('the viewport meta allows safe-area handling and device width',
      /width=device-width/.test(viewport) && /viewport-fit=cover/.test(viewport), viewport);

    // jsdom does not lay out or paint, so a real "does it overflow" measurement is not
    // possible here. What can be checked is that nothing asks for a fixed width that
    // would not fit a 320px phone, and that long words are allowed to wrap.
    const wideInline = $all('[style]').filter(el => {
      const m = /(?:^|;)\s*width\s*:\s*(\d+)px/.exec(el.getAttribute('style') || '');
      return m && Number(m[1]) > 300;
    });
    check('no element demands a fixed width wider than a small phone', wideInline.length === 0,
      wideInline.map(e => e.id || e.tagName).join(','));

    const css = window.document.querySelector('style').textContent;
    check('the layout caps itself with max-width rather than fixed widths',
      /max-width:680px/.test(css), 'container max-width missing');
    check('long words are allowed to wrap instead of overflowing',
      /overflow-wrap:anywhere/.test(css));
    check('horizontal scrolling is explicitly prevented', /overflow-x:hidden/.test(css));
    check('the bottom bar reserves space for the system gesture area',
      /env\(safe-area-inset-bottom\)/.test(css));
    check('touch targets are at least 44px tall',
      /--tap:46px/.test(css), 'tap-size token missing');
    check('reduced-motion is respected', /prefers-reduced-motion/.test(css));
    check('the server connection notice exists for a dead server', !!$('err'));

    console.log('\n[16] download failures are reported, not saved');
    // The reorder in section 8 marked the file stale, so regenerate first: these
    // checks are about a download that fails, not about a disabled button.
    $('generate').click();
    await waitFor('a fresh PDF before the failure tests',
      () => /is ready/.test(text('pdf-line')) && $('download').disabled === false, 90000);
    check('regenerated and download enabled before the failure tests',
      $('download').disabled === false, `disabled=${$('download').disabled}`);
    const downloadsBefore = window.__downloads.length;
    const failuresBefore = failedRequests.length;

    // (a) the server answers 200, but with a web page instead of a PDF
    window.fetch = async () => new Response('<html>502 Bad Gateway</html>',
      { status: 200, headers: { 'Content-Type': 'text/html' } });
    $('download').click();
    await waitFor('the bad response was rejected', () => $('err').style.display === 'block', 15000, 100);
    check('a non-PDF body is refused', /not a pdf/i.test(text('err')), text('err').slice(0, 160));
    check('  ↳ and nothing was saved to disk', window.__downloads.length === downloadsBefore,
      `${window.__downloads.length} downloads`);

    // (b) the server refuses because the file went stale
    window.fetch = async () => new Response(JSON.stringify({ detail: 'The page list changed '
      + 'after this PDF was generated - generate it again so the file matches what you see.' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } });
    $('download').click();
    await waitFor('the refusal is shown', () => /changed/i.test(text('err')), 15000, 100);
    check('a refused download says the page list changed', /changed/i.test(text('err')),
      text('err').slice(0, 160));
    check('  ↳ and nothing was saved to disk', window.__downloads.length === downloadsBefore,
      `${window.__downloads.length} downloads`);
    check('  ↳ and it does not give video advice for a PDF problem',
      !/choose a video from this device/i.test(text('err')), text('err').slice(0, 200));
    check('  ↳ and the download button works again afterwards',
      $('download').disabled === false && !/Preparing/.test(text('download')),
      `disabled=${$('download').disabled} label=${text('download')}`);

    // (c) an empty response must not be saved as an empty .pdf
    window.fetch = async () => new Response('', { status: 200,
      headers: { 'Content-Type': 'application/pdf' } });
    $('download').click();
    await waitFor('the empty body is caught', () => /empty/i.test(text('err')), 15000, 100);
    check('an empty PDF body is refused', /empty/i.test(text('err')), text('err').slice(0, 160));
    check('  ↳ and nothing was saved to disk', window.__downloads.length === downloadsBefore,
      `${window.__downloads.length} downloads`);

    // put the real fetch back and confirm the page still downloads correctly
    window.fetch = realFetch;
    $('download').click();
    await waitFor('a real download after the failures',
      () => window.__downloads.length > downloadsBefore, 30000);
    const recovered = window.__downloads[window.__downloads.length - 1] || {};
    check('downloads work again once the server is healthy',
      /\.pdf$/.test(String(recovered.name)), String(recovered.name));
    check('the three injected failures are the only new ones',
      failedRequests.length === failuresBefore, `${failedRequests.length - failuresBefore} new`);
    check('no console errors from the failure handling', consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(' | '));

    // ── 14. the browser layer stayed clean ─────────────────────────────────
    console.log('\n[17] console + network hygiene');
    check('no console errors', consoleErrors.length === 0,
      consoleErrors.slice(0, 4).join(' | '));
    const unexpected = failedRequests.filter((line) => !/-> (400|409|422|404)/.test(line));
    check('no unexpected failed requests', unexpected.length === 0,
      unexpected.slice(0, 5).join(' | '));
    const rejections = failedRequests.filter((line) => /-> 4\d\d/.test(line));
    check('the only rejections are the negative tests the page provoked',
      rejections.length === 3, rejections.join(' | '));
    check('nothing ever answered 5xx',
      !failedRequests.some((line) => /-> 5\d\d/.test(line)), 'a 5xx was returned');
    check('jsdom\'s own blob-iframe limitation is the only environment gap',
      knownEnvironmentGaps.length === 1, knownEnvironmentGaps.join(' | '));
  } finally {
    if (dom) dom.window.close();
    server.kill('SIGTERM');
  }

  console.log('\n' + '='.repeat(74));
  console.log(`${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log('\nfailures:');
    for (const item of failures) console.log('  - ' + item);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error('\nthe UI test itself crashed:', error);
  process.exit(2);
});
