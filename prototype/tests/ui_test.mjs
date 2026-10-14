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

import { readFileSync, openSync } from 'node:fs';
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
  const child = spawn('python3', [join(ROOT, 'prototype', 'server.py'),
                                  '--port', String(port), '--host', '127.0.0.1'],
                      { stdio: ['ignore', log, log] });
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
    Object.defineProperty(input, 'files', { value: [file], writable: false });
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

    // ── 11. failures of the download path ──────────────────────────────────
    console.log('\n[11] download failures are reported, not saved');
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

    // ── 12. the browser layer stayed clean ─────────────────────────────────
    console.log('\n[12] console + network hygiene');
    check('no console errors', consoleErrors.length === 0,
      consoleErrors.slice(0, 4).join(' | '));
    const unexpected = failedRequests.filter((line) => !/-> (400|409|422|404)/.test(line));
    check('no unexpected failed requests', unexpected.length === 0,
      unexpected.slice(0, 5).join(' | '));
    const rejections = failedRequests.filter((line) => /-> 4\d\d/.test(line));
    check('the only rejections are the two negative tests the page provoked',
      rejections.length === 2, rejections.join(' | '));
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
