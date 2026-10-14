/**
 * Verifies the JavaScript engine against the same ground-truth fixtures the Python
 * engine is tested with, so the browser build cannot quietly drift from the tested
 * one. Run: node test/run.mjs
 *
 * The frames are pulled with ffmpeg exactly the way the Python sampler pulls them
 * (3 per second, 160x90, grayscale, one sequential decode), then handed to the
 * very same `signature()` and `detect()` the browser calls.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { detect, signature, distance } from '../pagesnap-core.js';

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(here, '..', '..', 'engine', 'tests', 'fixtures');
const FPS = 3, W = 160, H = 90;

function ffmpegPath() {
  const candidates = [
    process.env.PAGESNAP_FFMPEG,
    execFileSync('python3', ['-c', 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())'], {
      encoding: 'utf8',
    }).trim(),
  ].filter(Boolean);
  for (const c of candidates) if (existsSync(c)) return c;
  return 'ffmpeg';
}

/** Decode a fixture into 160x90 grayscale frames, exactly like the Python sampler. */
function framesOf(videoPath) {
  const raw = execFileSync(ffmpegPath(), [
    '-v', 'error', '-nostdin', '-i', videoPath,
    '-vf', `fps=${FPS},scale=${W}:${H}:flags=area,format=gray`,
    '-f', 'rawvideo', '-pix_fmt', 'gray', '-',
  ], { maxBuffer: 1024 * 1024 * 256 });

  const frameBytes = W * H;
  const frames = [];
  for (let off = 0; off + frameBytes <= raw.length; off += frameBytes) {
    frames.push(new Uint8Array(raw.buffer, raw.byteOffset + off, frameBytes));
  }
  return frames;
}

function checkFixture(truth, preset) {
  const videoPath = join(FIXTURES, truth.video);
  if (!existsSync(videoPath)) return { ok: false, problems: [`missing fixture ${truth.video}`] };

  const started = Date.now();
  const frames = framesOf(videoPath);
  const sigs = frames.map((gray, i) => signature(i, i / FPS, gray, W, H));
  const report = detect(sigs, preset);
  const elapsed = (Date.now() - started) / 1000;

  const problems = [];
  const pages = report.pages;
  const expected = truth.pages;

  if (preset === 'balanced' && pages.length !== expected.length) {
    problems.push(`expected exactly ${expected.length} page(s), got ${pages.length}`);
  }
  if (preset === 'strict' && pages.length > expected.length) {
    problems.push(`strict invented ${pages.length - expected.length} extra page(s)`);
  }
  if (preset === 'eager' && pages.length < expected.length) {
    problems.push(`eager missed ${expected.length - pages.length} page(s)`);
  }

  for (const exp of expected) {
    const [lo, hi] = exp.acceptable;
    const inside = pages.find((p) => p.time >= lo - 0.35 && p.time <= hi + 0.35);
    if (!inside) {
      const nearest = pages.reduce((best, p) => {
        const gap = Math.min(Math.abs(p.time - lo), Math.abs(p.time - hi));
        return !best || gap < best.gap ? { p, gap } : best;
      }, null);
      problems.push(`page '${exp.id}' (wanted ${lo}-${hi}s) was never captured`
        + (nearest ? `; nearest attempt t=${nearest.p.time.toFixed(2)}s` : '; nothing captured'));
    }
  }

  for (const t of (truth.forbidden || [])) {
    for (const p of pages) {
      if (Math.abs(p.time - t) < 0.34) {
        problems.push(`captured t=${p.time.toFixed(2)}s, inside a transition/stroke/scroll window`);
        break;
      }
    }
  }

  return {
    ok: problems.length === 0, problems, elapsed,
    found: pages.map((p) => `${p.time.toFixed(2)}s/${p.reason}`).join(' '),
  };
}


function main() {
  const truthFile = join(FIXTURES, 'ground_truth.json');
  if (!existsSync(truthFile)) {
    console.error('No fixtures. Build them first:  cd engine && python3 tests/make_fixtures.py');
    process.exit(2);
  }
  const data = JSON.parse(readFileSync(truthFile, 'utf8'));

  let failures = 0;
  for (const preset of ['balanced', 'strict', 'eager']) {
    console.log(`\n=== sensitivity: ${preset} ===`);
    for (const truth of data.fixtures) {
      const r = checkFixture(truth, preset);
      console.log(`  [${r.ok ? 'PASS' : 'FAIL'}] ${truth.video.padEnd(15)} `
        + `${String(truth.pages.length).padStart(2)} expected  ->  ${r.found || '(nothing)'}`);
      for (const p of r.problems) { failures++; console.log(`         ! ${p}`); }
    }
  }

  console.log();
  if (failures) {
    console.log(`${failures} problem(s) - the browser engine is out of step with the tested one.`);
    process.exit(1);
  }
  console.log('The browser engine matches the Python engine on every fixture.');
}

main();
