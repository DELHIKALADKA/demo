/**
 * Check the *built* site the way GitHub Pages serves it.
 *
 *     node docs/test/site.mjs                       # builds _site/, serves it at /demo/
 *     node docs/test/site.mjs --base /demo/         # ...or any other base path
 *     node docs/test/site.mjs --no-build            # use the _site/ that is already there
 *
 * The other suites test the app you run from docs/ at the root of a server. This one
 * tests the thing that actually gets published, under the subpath it is published at,
 * in a real browser. Those are different situations, and the difference is invisible
 * until it is live:
 *
 *   * `./pagesnap-core.js` resolves to `/demo/pagesnap-core.js` on Pages and to
 *     `/pagesnap-core.js` locally - both fine, but only one of them is what visitors get;
 *   * `/pagesnap-core.js` works locally and 404s on Pages;
 *   * every request has to stay under `/demo/`, or it is not part of this site at all.
 *
 * So this checks the response of every request the page makes, not a list of files, and
 * it also runs the app end to end from the published folder: find pages in the sample
 * video, build a PDF, download it. A page whose scripts 404 looks exactly like a page
 * whose scripts have a bug, and this tells them apart.
 *
 * The same folder is then served at the domain root as well (`--base /`), because the
 * build claims to work in both places and that claim should be checked, not asserted.
 *
 * Needs a browser and puppeteer-core. With no browser it prints SKIPPED and exits 0.
 */

import { createServer } from 'node:http';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DOCS = dirname(HERE);
const ROOT = dirname(DOCS);

let pass = 0;
let fail = 0;
const failures = [];

function ok(condition, label, detail = '') {
  if (condition) {
    pass += 1;
    console.log(`    [PASS] ${label}`);
  } else {
    fail += 1;
    failures.push(label);
    console.log(`    [FAIL] ${label}${detail ? `\n           ${detail}` : ''}`);
  }
}

function info(message) {
  console.log(`    [info] ${message}`);
}

function section(title) {
  console.log(`\n  === ${title} ===`);
}

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.json': 'application/json', '.webmanifest': 'application/manifest+json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
  '.md': 'text/markdown; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
};

/**
 * Serve a folder under a base path, the way GitHub Pages does:
 * a directory serves its index.html, a missing path serves 404.html with a 404
 * status, and a folder is never listed.
 */
async function serveSite(root, base) {
  const prefix = base.endsWith('/') ? base : `${base}/`;
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    let pathname = decodeURIComponent(url.pathname);

    if (prefix !== '/' && pathname === prefix.slice(0, -1)) {
      res.writeHead(301, { location: prefix }).end();
      return;
    }
    if (prefix !== '/' && !pathname.startsWith(prefix)) {
      return send404(res, root);
    }
    pathname = prefix === '/' ? pathname : `/${pathname.slice(prefix.length)}`;

    let path = join(root, pathname);
    if (!path.startsWith(root)) { res.writeHead(403).end('no'); return; }
    try {
      const info_ = await stat(path);
      if (info_.isDirectory()) {
        if (!pathname.endsWith('/')) {
          res.writeHead(301, { location: pathname + '/' }).end();
          return;
        }
        path = join(path, 'index.html');
      }
      const body = await readFile(path);
      res.writeHead(200, {
        'content-type': MIME[extname(path)] || 'application/octet-stream',
        'content-length': body.length,
        'cache-control': 'no-store',
      });
      res.end(body);
    } catch (_) {
      send404(res, root);
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, port: server.address().port, prefix };
}

async function send404(res, root) {
  try {
    const body = await readFile(join(root, '404.html'));
    res.writeHead(404, { 'content-type': 'text/html; charset=utf-8' }).end(body);
  } catch (_) {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
  }
}

function findChrome() {
  const candidates = [
    process.env.PAGESNAP_CHROME,
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium', '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((path) => existsSync(path)) || null;
}

async function loadPuppeteer() {
  try {
    const module = await import('puppeteer-core');
    return module.default ?? module;
  } catch (_) {
    return null;
  }
}

function args(argv) {
  const out = { base: '/demo/', build: true, keep: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--base') out.base = argv[++i];
    else if (argv[i] === '--no-build') out.build = false;
    else if (argv[i] === '--keep') out.keep = true;
  }
  out.base = out.base.startsWith('/') ? out.base : `/${out.base}`;
  return out;
}

/** Every request the page made, so a silent 404 cannot hide. */
function watchRequests(page, prefix, record) {
  page.on('response', (response) => {
    const url = response.url();
    if (!url.startsWith('http')) return;             // data:, blob: - not a request
    const path = new URL(url).pathname;
    record.push({ url, path, status: response.status() });
  });
}

function requestProblems(record, prefix) {
  const problems = [];
  for (const entry of record) {
    if (entry.status >= 400) problems.push(`${entry.status} ${entry.path}`);
    else if (!entry.path.startsWith(prefix)) {
      problems.push(`outside ${prefix}: ${entry.path}`);
    }
  }
  return problems;
}

async function runApp(browser, url, label) {
  const page = await browser.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  watchRequests(page, new URL(url).pathname, requests);

  // One navigation. networkidle2 waits for the modules and the icon requests too, so the
  // request audit below sees everything the page asks for.
  const response = await page.goto(url, { waitUntil: 'networkidle2' });

  section(`${label}: the page as published at ${new URL(url).pathname}`);
  ok(response.status() === 200, 'the site answers 200 under its base path',
     `got ${response.status()}`);
  ok(await page.$('#sample') !== null, 'the page rendered (the sample button is there)');
  ok(await page.$('#export-box') !== null, 'the PDF box is in the published page');
  ok(await page.$('#capture-box') !== null, 'the capture box is in the published page');
  ok(await page.$('#link') !== null, 'the link box is in the published page');

  // the links in the head and the footer must resolve inside the site, not at the root
  const resolved = await page.evaluate(() => {
    const pick = (selector) => {
      const element = document.querySelector(selector);
      return element ? element.href : null;
    };
    return {
      manifest: pick('link[rel="manifest"]'),
      icon: pick('link[rel="icon"]'),
      touch: pick('link[rel="apple-touch-icon"]'),
      offline: pick('p.foot a'),
    };
  });
  const base = new URL(url).pathname;
  for (const [what, href] of Object.entries(resolved)) {
    ok(href && new URL(href).pathname.startsWith(base),
       `the ${what} link stays inside the site`, `${href}`);
  }

  // the app itself: find the pages in the sample video and make a PDF from this folder
  await page.click('#sample');
  const started = Date.now();
  await page.waitForFunction(
    () => document.getElementById('step-review').style.display !== 'none'
       || (document.getElementById('err').textContent || '').length > 0,
    { timeout: 180000, polling: 300 },
  );
  const state = await page.evaluate(() => ({
    pages: document.getElementById('pages').children.length,
    count: document.getElementById('export-count').textContent,
    note: document.getElementById('save-note').textContent,
    error: document.getElementById('err').textContent,
  }));
  info(`the sample found ${state.pages} page(s) in ${((Date.now() - started) / 1000).toFixed(1)}s`);
  ok(state.pages >= 3 && !state.error, 'the app works from the published folder',
     state.error || `only ${state.pages} pages`);
  ok(/of \d+ pictures? selected/.test(state.count), 'the PDF box counted them',
     state.count);

  // The manifest and the icons are part of the site, but a browser only goes and gets
  // them when it has a reason to. Ask for each one by name, from the page, so the answer
  // comes from the server rather than from a cache and a path typo cannot hide.
  const assets = [
    'manifest.webmanifest', 'pagesnap-core.js', 'pagenotes-pdf.js', 'pagesnap-media.js',
    'favicon.svg', 'apple-touch-icon.png', 'icon-192.png', 'icon-512.png',
  ];
  const fetched = await page.evaluate(async (names) => {
    const out = {};
    for (const name of names) {
      try {
        const response = await fetch(name);
        out[name] = `${response.status} ${response.headers.get('content-type')}`;
      } catch (error) {
        out[name] = `threw ${error.message}`;
      }
    }
    return out;
  }, assets);
  const notServed = Object.entries(fetched).filter(([, value]) => !value.startsWith('200'));
  ok(notServed.length === 0, `all ${assets.length} asset(s) load from ${base} by name`,
     notServed.map(([name, value]) => `${name}: ${value}`).join('; '));
  const manifestType = fetched['manifest.webmanifest'] || '';
  ok(/manifest\+json|application\/json/.test(manifestType),
     'the manifest is served as a manifest, not as a download',
     manifestType);

  const parsed = await page.evaluate(async () => {
    const text = await (await fetch('manifest.webmanifest')).text();
    try { return JSON.parse(text); } catch (error) { return { error: error.message }; }
  });
  ok(!parsed.error && parsed.start_url === './',
     'the manifest parses and its paths are relative (so scope follows the base path)',
     JSON.stringify(parsed).slice(0, 120));

  const problems = requestProblems(requests, base);
  ok(problems.length === 0,
     `all ${requests.length} request(s) stayed under ${base} and none failed`,
     problems.slice(0, 4).join('; '));
  ok(errors.length === 0, 'no JavaScript errors from the published page',
     errors.slice(0, 3).join('; '));

  return { page, errors };
}

async function main() {
  const options = args(process.argv.slice(2));
  const site = join(ROOT, '_site');

  if (options.build) {
    section('building the site the way the deploy does');
    const built = spawnSync('python3', ['docs/build_site.py', '--base-path', options.base],
                            { cwd: ROOT, encoding: 'utf8' });
    process.stdout.write(built.stdout.split('\n').map((l) => (l ? `    ${l}` : l)).join('\n'));
    if (built.stderr) process.stderr.write(built.stderr);
    ok(built.status === 0, 'the production build succeeded');
    if (built.status !== 0) return finish();
  }

  if (!existsSync(join(site, 'index.html'))) {
    console.log('\n  No _site/index.html. Run: python3 docs/build_site.py');
    return finish();
  }

  const chrome = findChrome();
  const puppeteer = await loadPuppeteer();
  if (!chrome || !puppeteer) {
    console.log(`\n  SKIPPED: ${!puppeteer ? 'puppeteer-core is not installed' :
      'no Chrome or Chromium found'}.`);
    console.log('  The published site was NOT checked in a browser.');
    console.log('  Install it (npm ci --prefix docs) or set PAGESNAP_CHROME=/path/to/chrome.\n');
    return 0;
  }

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
    env: { ...process.env, LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8' },
  });

  try {
    // 1. the real deployment: the built folder under /demo/
    const production = await serveSite(site, options.base);
    try {
      await runApp(browser, `http://127.0.0.1:${production.port}${production.prefix}`, 'GitHub Pages layout');

      section('the single-file copy, under the base path');
      const offline = await browser.newPage();
      const offlineErrors = [];
      const offlineRequests = [];
      offline.on('pageerror', (error) => offlineErrors.push(error.message));
      offline.on('console', (message) => {
        if (message.type() === 'error') offlineErrors.push(message.text());
      });
      offline.on('request', (request) => offlineRequests.push(request.url()));
      const offlineUrl = `http://127.0.0.1:${production.port}${production.prefix}pagesnap-offline.html`;
      const offlineResponse = await offline.goto(offlineUrl, { waitUntil: 'load' });
      ok(offlineResponse.status() === 200, 'the footer link really opens the offline copy');
      const offlineParts = await offline.evaluate(() => ({
        file: !!document.getElementById('file'),
        link: !!document.getElementById('link'),
        exportBox: !!document.getElementById('export-box'),
        captureBox: !!document.getElementById('capture-box'),
        sample: !!document.getElementById('sample'),
      }));
      // The sample button is removed on purpose: that file travels with no video beside
      // it. Everything the workflow needs must still be there.
      ok(offlineParts.file && offlineParts.link && offlineParts.exportBox
         && offlineParts.captureBox && !offlineParts.sample,
         'the offline copy is the whole workflow (and drops the sample button by design)',
         JSON.stringify(offlineParts));
      const external = offlineRequests.filter((u) => u.startsWith('http') && u !== offlineUrl);
      ok(external.length === 0, 'the offline copy asks the network for nothing',
         external.slice(0, 3).join('; '));
      ok(offlineErrors.length === 0, 'the offline copy logs no errors',
         offlineErrors.slice(0, 2).join('; '));
      await offline.close();

      section('a wrong address inside the site');
      const missing = await browser.newPage();
      const missingResponse = await missing.goto(
        `http://127.0.0.1:${production.port}${production.prefix}nope-does-not-exist`,
        { waitUntil: 'load' });
      ok(missingResponse.status() === 404, 'a missing page answers 404, not a directory listing');
      const body = await missing.content();
      ok(body.includes(`${options.base}`), 'the 404 page points back into the site, not at the root');
      ok(!/<pre>|<ul><li><a href=/.test(body), 'and it is not a file listing');
      await missing.close();

      // 2. the same folder at the domain root: the claim is that it works in both places
      const rooted = await serveSite(site, '/');
      try {
        await runApp(browser, `http://127.0.0.1:${rooted.port}/`, 'domain-root layout (other hosts)');
      } finally {
        rooted.server.close();
      }
    } finally {
      production.server.close();
    }
  } finally {
    await browser.close();
  }

  return finish();
}

function finish() {
  console.log(`\n  ${pass} checks, ${fail} failures.`);
  if (fail) {
    console.log('  Failing checks:');
    for (const label of failures) console.log(`    - ${label}`);
  }
  process.exitCode = fail ? 1 : 0;
  return fail ? 1 : 0;
}

await main();
