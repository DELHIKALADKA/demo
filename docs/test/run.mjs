/**
 * Verifies the JavaScript engine against the same ground-truth fixtures the Python
 * engine is tested with, so the browser build cannot quietly drift from the tested
 * one. Run: node test/run.mjs
 *
 * The frames are pulled with ffmpeg exactly the way the Python sampler pulls them
 * (3 per second, 160x90, grayscale, one sequential decode), then handed to the
 * very same `signature()` and `detect()` the browser calls.
 *
 * On top of that it checks the two things a visitor actually receives: the sample
 * video the page offers ("33 seconds, 4 pages of notes") must really produce those
 * four pages, and the PDF writer must put the pages in the order it was given.
 *
 * Needs nothing but node and ffmpeg (imageio-ffmpeg supplies one). The videos in
 * test/media are generated: python3 test/make_media.py
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { detect, signature, distance } from '../pagesnap-core.js';
import { buildPdf, safeFilename, encodeLossless } from '../pagenotes-pdf.js';
import {
  classifyUrl, parseTimecode, checkTimecode, formatTimecode,
  describeResolution, qualityNote, parseLink,
} from '../pagesnap-media.js';

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(here, '..', '..');
const FIXTURES = join(ROOT, 'engine', 'tests', 'fixtures');
const MEDIA = join(here, 'media');
const SAMPLE = join(here, '..', 'sample', 'lecture.mp4');
const FPS = 3, W = 160, H = 90;

let failures = 0;
let checks = 0;
function ok(condition, label, detail = '') {
  checks++;
  if (condition) {
    console.log(`  [PASS] ${label}`);
  } else {
    failures++;
    console.log(`  [FAIL] ${label}${detail ? ` — ${detail}` : ''}`);
  }
}
function section(title) {
  console.log(`\n=== ${title} ===`);
}

let cachedFfmpeg = null;

/**
 * Find an ffmpeg. In order: whatever PAGESNAP_FFMPEG says, the ffmpeg that
 * imageio-ffmpeg ships (from this repo's .venv, then from the system python, then
 * `py` - the only launcher Windows has), and finally a plain ffmpeg on PATH.
 * A python without the package is skipped quietly instead of crashing the suite.
 */
function ffmpegPath() {
  if (cachedFfmpeg) return cachedFfmpeg;
  const ask = 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())';
  const pythons = [
    process.env.PAGESNAP_PYTHON,
    join(ROOT, '.venv', 'bin', 'python'),
    join(ROOT, '.venv', 'Scripts', 'python.exe'),
    'python3', 'python', 'py',
  ].filter(Boolean);

  const candidates = [process.env.PAGESNAP_FFMPEG];
  for (const python of pythons) {
    try {
      const exe = execFileSync(python, ['-c', ask], {
        encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (exe && existsSync(exe)) candidates.push(exe);
    } catch (_) { /* this interpreter has no imageio-ffmpeg; try the next one */ }
  }
  candidates.push('ffmpeg');

  for (const candidate of candidates) {
    if (!candidate) continue;
    if (candidate === 'ffmpeg') {
      try {
        execFileSync('ffmpeg', ['-version'], { stdio: 'ignore' });
      } catch (_) { continue; }
    }
    cachedFfmpeg = candidate;
    return candidate;
  }
  throw new Error('no ffmpeg: pip install imageio-ffmpeg, or set PAGESNAP_FFMPEG to one');
}

/** Decode a video into 160x90 grayscale frames, exactly like the Python sampler. */
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

/** Run the engine over a video file and hand back the report. */
function detectFile(videoPath, preset = 'balanced') {
  const frames = framesOf(videoPath);
  const sigs = frames.map((gray, i) => signature(i, i / FPS, gray, W, H));
  return { frames: frames.length, sigs, report: detect(sigs, preset) };
}

// --------------------------------------------------------------------------- //
// the ground-truth fixtures
// --------------------------------------------------------------------------- //

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

function fixtureSection() {
  const truthFile = join(FIXTURES, 'ground_truth.json');
  if (!existsSync(truthFile)) {
    console.error('No fixtures. Build them first:  cd engine && python3 tests/make_fixtures.py');
    process.exit(2);
  }
  const data = JSON.parse(readFileSync(truthFile, 'utf8'));

  for (const preset of ['balanced', 'strict', 'eager']) {
    section(`truth fixtures, sensitivity: ${preset}`);
    for (const truth of data.fixtures) {
      const r = checkFixture(truth, preset);
      console.log(`  [${r.ok ? 'PASS' : 'FAIL'}] ${truth.video.padEnd(15)} `
        + `${String(truth.pages.length).padStart(2)} expected  ->  ${r.found || '(nothing)'}`);
      if (!r.ok) { failures++; for (const p of r.problems) console.log(`         ! ${p}`); }
      checks++;
    }
  }
}

// --------------------------------------------------------------------------- //
// the sample the page offers to visitors
// --------------------------------------------------------------------------- //

function sampleSection() {
  section('the sample video the page offers ("33 seconds, 4 pages of notes")');
  if (!existsSync(SAMPLE)) {
    ok(false, 'docs/sample/lecture.mp4 exists');
    return;
  }
  const size = readFileSync(SAMPLE).length;
  ok(size > 50000, 'the sample is a real file', `${size} bytes`);

  const { sigs, report } = detectFile(SAMPLE);
  const pages = report.pages;
  const frameAt = (t) => sigs[Math.max(0, Math.min(sigs.length - 1, Math.round(t * FPS)))];

  ok(report.duration > 33 && report.duration < 34,
    'the sample is the 33s lecture the button promises', `got ${report.duration.toFixed(1)}s`);
  ok(pages.length === 4, 'the sample yields exactly 4 pages',
    `got ${pages.length}: ${pages.map((p) => p.time.toFixed(2)).join(', ')}`);

  const near = (page, t, tolerance = 0.4) => page && Math.abs(page.time - t) <= tolerance;
  ok(near(pages[0], 1.0), 'page 1 is the opening page (1s)',
    pages[0] ? `got ${pages[0].time.toFixed(2)}s` : 'missing');
  ok(near(pages[1], 13.0), 'page 2 is the page that finishes being written at 13s',
    pages[1] ? `got ${pages[1].time.toFixed(2)}s` : 'missing');
  ok(near(pages[2], 16.0), 'page 3 arrives with the cut at 16s',
    pages[2] ? `got ${pages[2].time.toFixed(2)}s` : 'missing');

  // The last page of this video is on screen, unchanged, from 25s to the end, so a
  // screenshot taken anywhere in that stretch is the same page. (Measured: the frames
  // at 25s and 33s are identical.) What matters is that the page was captured at all
  // and that it is not one of the earlier three.
  const last = pages[3];
  ok(!!last && last.time >= 24.9 && last.time <= 33.7,
    'page 4 is captured from the final page (on screen 25s to the end)',
    last ? `got ${last.time.toFixed(2)}s` : 'missing');
  if (last) {
    ok(distance(frameAt(last.time), frameAt(25.0)) < 0.01,
      'page 4 really is the picture that is on screen from 25s');
  }

  // four captures, four different pictures - this is what would catch a regression
  // that returned the same frame four times
  let closest = { gap: Infinity, pair: '' };
  for (let i = 0; i < pages.length; i++) {
    for (let j = i + 1; j < pages.length; j++) {
      const gap = distance(frameAt(pages[i].time), frameAt(pages[j].time));
      if (gap < closest.gap) closest = { gap, pair: `${i + 1} and ${j + 1}` };
    }
  }
  ok(pages.length === 4 && closest.gap > 0.03,
    'the four pages are four different pictures',
    `closest pair (pages ${closest.pair}) differ by only ${closest.gap.toFixed(3)}`);
}

// --------------------------------------------------------------------------- //
// containers: does the engine cope with the files people actually have?
// --------------------------------------------------------------------------- //

function mediaSection() {
  section('containers (generated: python3 test/make_media.py)');
  const wanted = [
    ['colours.mp4', 3, [0, 2, 4]],       // also the order test in the browser suite
    ['phone.mov', 2, [0, 2]],            // what an iPhone gives you
    ['slides.webm', 2, [0, 2]],
    ['slides.mkv', 2, [0, 2]],
    ['hevc.mp4', 2, [0, 2]],             // H.265 inside MP4
  ];
  const missing = wanted.filter(([name]) => !existsSync(join(MEDIA, name)));
  if (missing.length) {
    console.log(`  [SKIP] media not generated (${missing.map((m) => m[0]).join(', ')})`);
    console.log('         run: python3 docs/test/make_media.py');
    return;
  }
  for (const [name, count, times] of wanted) {
    let report;
    try {
      ({ report } = detectFile(join(MEDIA, name)));
    } catch (error) {
      ok(false, `${name} decodes`, String(error.message).split('\n')[0]);
      continue;
    }
    ok(report.pages.length === count, `${name} yields ${count} page(s)`,
      `got ${report.pages.length}: ${report.pages.map((p) => p.time.toFixed(2)).join(', ')}`);
    for (const [i, t] of times.entries()) {
      const page = report.pages[i];
      ok(page && Math.abs(page.time - t) <= 0.4, `${name} page ${i + 1} at ${t}s`,
        page ? `got ${page.time.toFixed(2)}s` : 'missing');
    }
  }
}

// --------------------------------------------------------------------------- //
// the PDF writer
// --------------------------------------------------------------------------- //

/** Find a byte sequence, so the tests can prove the pages are in order. */
function indexOfBytes(haystack, needle) {
  outer: for (let i = 0; i + needle.length <= haystack.length; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

function fakeJpeg(marker, size = 96) {
  const bytes = new Uint8Array(size);
  bytes.set([0xff, 0xd8, 0xff, 0xe0]);
  for (let i = 4; i < size; i++) bytes[i] = (marker * 7 + i) & 0xff;
  return bytes;
}

/**
 * Read a finished PDF the way the other checks read one: from its own bytes.
 * Returns the page count and each page's MediaBox, which is all the paper and
 * orientation questions need.
 */
function probePdf(bytes) {
  const text = Buffer.from(bytes).toString('latin1');
  const boxes = [...text.matchAll(/\/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]/g)]
    .map((m) => [Number(m[1]), Number(m[2])]);
  const declared = text.match(/\/Count (\d+)/);
  return {
    pageCount: boxes.length,
    declared: declared ? Number(declared[1]) : null,
    boxes,
    text,
  };
}

/** A tiny but genuinely valid JPEG, so the writer is fed real bytes. */
const JPEG_2x2 = Uint8Array.from(Buffer.from(
  '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a'
  + 'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAIBAREA/8QAFAABAAAAAAAA'
  + 'AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==', 'base64'));

function pdfSection() {
  section('the PDF writer');

  const jpegs = [fakeJpeg(1), fakeJpeg(2), fakeJpeg(3)];
  const pdf = buildPdf(jpegs.map((jpeg) => ({ jpeg, width: 1280, height: 720 })),
    { pageSize: 'a4', title: 'Unit test' });

  ok(pdf[0] === 0x25 && pdf[1] === 0x50 && pdf[2] === 0x44 && pdf[3] === 0x46, 'the file starts with %PDF');
  const text = Buffer.from(pdf).toString('latin1');
  ok(text.includes('%%EOF'), 'the file ends with %%EOF');
  ok(text.includes('/Count 3'), 'the page tree says there are 3 pages');
  ok((text.match(/\/Type \/Page[^s]/g) || []).length === 3, 'there are 3 page objects');
  ok((text.match(/\/Subtype \/Image/g) || []).length === 3, 'there are 3 images');

  // order: the three payloads must appear in the file in the order they were given
  const offsets = jpegs.map((jpeg) => indexOfBytes(pdf, jpeg));
  ok(offsets.every((o) => o > 0), 'every picture is embedded');
  ok(offsets[0] < offsets[1] && offsets[1] < offsets[2],
    'the pages are written in the order they were handed over', `offsets ${offsets.join(', ')}`);

  // landscape pictures get landscape paper
  ok(text.includes('/MediaBox [0 0 841.89 595.28]'), 'a wide picture gets A4 landscape');
  const portrait = buildPdf([{ jpeg: fakeJpeg(9), width: 720, height: 1280 }], {});
  ok(Buffer.from(portrait).toString('latin1').includes('/MediaBox [0 0 595.28 841.89]'),
    'a tall picture gets A4 portrait');

  // the cross-reference table has to point at real objects, or readers refuse the file
  const startxref = Number(/(\d+)\s*%%EOF/.exec(text)[1]);
  ok(text.slice(startxref, startxref + 4) === 'xref', 'startxref points at the cross-reference table');
  const declared = Number(/xref\n0 (\d+)\n/.exec(text)[1]);
  const entries = [...text.slice(startxref).matchAll(/^(\d{10}) 00000 n $/gm)].map((m) => Number(m[1]));
  ok(entries.length === declared - 1,
    `the table lists every object it declares (${declared - 1} + 1 free)`,
    `declared ${declared}, listed ${entries.length}`);
  ok(entries.every((offset, i) => text.slice(offset, offset + 8).startsWith(`${i + 1} 0 obj`)),
    'every entry points at the object it claims');

  let threw = false;
  try { buildPdf([]); } catch (_) { threw = true; }
  ok(threw, 'writing an empty PDF is refused instead of producing a broken file');

  const numbered = Buffer.from(buildPdf(
    [{ jpeg: fakeJpeg(4), width: 720, height: 1280 }],
    { pageNumbers: true, title: 'Numbered' })).toString('latin1');
  ok(numbered.includes('/Font << /F1') && numbered.includes('/BaseFont /Helvetica'),
    'page numbers bring a font with them');
  ok(numbered.includes('BT /F1 8 Tf 0.55 g') && numbered.includes('(1 / 1) Tj'),
    'the page number is written on the page');
  const plain = Buffer.from(buildPdf([{ jpeg: fakeJpeg(4), width: 100, height: 50 }], {})).toString('latin1');
  ok(!plain.includes('/Font'), 'no font is added when page numbers are not asked for');
}

// --------------------------------------------------------------------------- //
// the filename on the downloaded file
// --------------------------------------------------------------------------- //

function filenameSection() {
  section('the filename the file is saved under');
  const cases = [
    ['Biology Chapter 6', 'Biology Chapter 6.pdf'],
    ['Chapter 6.pdf', 'Chapter 6.pdf'],
    ['Report.PDF', 'Report.PDF'],
    ['अध्याय ६', 'अध्याय ६.pdf'],                 // Hindi survives
    ['தமிழ் குறிப்புகள்', 'தமிழ் குறிப்புகள்.pdf'],
    ['Notes 📚', 'Notes.pdf'],                    // emoji have no place in a filename
    ['a/b:c*?"<>|d', 'a-b-c-d.pdf'],              // characters no filesystem accepts
    ['../../etc/passwd', 'etc-passwd.pdf'],       // no path escapes into the name
    ['   ', 'notes.pdf'],
    ['', 'notes.pdf'],
    ['!!!', 'notes.pdf'],
    ['.hidden', 'hidden.pdf'],
    ['CON', '_CON.pdf'],                          // reserved on Windows
    ['', 'notes.pdf'],
  ];
  for (const [input, expected] of cases) {
    const got = safeFilename(input);
    ok(got === expected, `"${input}" -> "${expected}"`, `got "${got}"`);
  }
  ok(safeFilename('x'.repeat(200)).length <= 84, 'a very long title is shortened');
  ok(safeFilename('  trailing dots...  ') === 'trailing dots.pdf', 'trailing dots are dropped',
    `got "${safeFilename('  trailing dots...  ')}"`);
}


// --------------------------------------------------------------------------- //

/**
 * The link box: what it recognises, and - just as important - what it refuses to
 * claim. Every case below is a string a real person might paste.
 */
function linkSection() {
  section('which platform is this link from');
  const cases = [
    // YouTube, in every shape a share sheet produces
    ['https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'youtube', 'video', 'frames'],
    ['https://youtu.be/dQw4w9WgXcQ', 'youtube', 'video', 'frames'],
    ['https://m.youtube.com/watch?v=dQw4w9WgXcQ', 'youtube', 'video', 'frames'],
    ['https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s', 'youtube', 'video', 'frames'],
    ['https://www.youtube.com/shorts/abc123XYZ', 'youtube', 'video', 'frames'],
    ['https://www.youtube.com/live/abc123XYZ', 'youtube', 'video', 'frames'],
    ['https://www.youtube.com/embed/abc123XYZ', 'youtube', 'video', 'frames'],
    ['https://www.youtube.com/post/UgkxABCDEF', 'youtube', 'community_post', 'photos'],
    ['https://www.youtube.com/@somebody', 'youtube', 'listing', 'unknown'],
    // Instagram
    ['https://www.instagram.com/p/Cabcdefghij/', 'instagram', 'photo_post', 'photos'],
    ['https://instagram.com/reel/Cabcdefghij/?igsh=xyz', 'instagram', 'reel', 'frames'],
    ['https://m.instagram.com/reels/Cabcdefghij/', 'instagram', 'reel', 'frames'],
    ['https://instagr.am/p/Cabcdefghij/', 'instagram', 'photo_post', 'photos'],
    // Facebook
    ['https://www.facebook.com/reel/123456789', 'facebook', 'reel', 'frames'],
    ['https://m.facebook.com/watch/?v=123456789', 'facebook', 'video', 'frames'],
    ['https://www.facebook.com/photo?fbid=123&set=a.456', 'facebook', 'photo_post', 'photos'],
    ['https://www.facebook.com/media/set/?set=a.123', 'facebook', 'photo_post', 'photos'],
    ['https://fb.watch/abcdef/', 'facebook', 'short_link', 'frames'],
    ['https://fb.me/abcdef', 'facebook', 'page', 'unknown'],
    // Everything else, told honestly
    ['https://example.com/clip.mp4', 'direct', 'video_file', 'frames'],
    ['https://example.com/page.jpg', 'direct', 'image_file', 'photos'],
    ['https://example.com/some/article', 'web', 'web_page', 'unknown'],
  ];
  for (const [url, platform, kind, yields] of cases) {
    const info = classifyUrl(url);
    ok(info.ok && info.platform === platform && info.kind === kind && info.yields === yields,
      `${url.slice(0, 52)} -> ${platform}/${kind}`,
      `got ${info.platform}/${info.kind}/${info.yields}`);
  }

  section('what it says can and cannot be done');
  ok(classifyUrl('https://youtu.be/dQw4w9WgXcQ').extractable === false,
    'a YouTube video is not claimed to be fetchable from a static page');
  ok(/frames, not a set of post photographs/.test(classifyUrl('https://youtu.be/x').limit),
    'and it explains that a video gives frames, not post photos');
  ok(/signed-in/.test(classifyUrl('https://www.instagram.com/p/Cabc/').limit),
    'an Instagram post says why it cannot be read');
  ok(/Save each picture|Save the/.test(classifyUrl('https://www.instagram.com/p/Cabc/').alternative),
    'and offers the thing that does work');
  ok(classifyUrl('https://www.youtube.com/watch?v=x').normalized
     === 'https://www.youtube.com/watch?v=x', 'a watch link normalises to itself');
  ok(classifyUrl('https://youtu.be/x').normalized === 'https://www.youtube.com/watch?v=x',
    'a shortened youtu.be link normalises to the full watch URL',
    classifyUrl('https://youtu.be/x').normalized);
  ok(!/t=42s|igsh/.test(classifyUrl('https://www.youtube.com/watch?v=x&t=42s').normalized
     + classifyUrl('https://instagram.com/reel/y/?igsh=abc').normalized),
    'tracking parameters are dropped from the normalised address');

  section('links that must be refused outright');
  const bad = [
    ['', 'empty'],
    ['   ', 'blank'],
    ['not a link at all', 'spaces'],
    ['javascript:alert(1)', 'a javascript: URL'],
    ['data:text/html,<script>alert(1)</script>', 'a data: URL'],
    ['file:///etc/passwd', 'a file: URL'],
    ['https://user:secret@example.com/x', 'credentials in the address'],
    ['https://example.com:8080/x', 'an odd port'],
    ['http://localhost/x', 'a hostname with no dot'],
  ];
  for (const [url, why] of bad) {
    const info = classifyUrl(url);
    ok(info.ok === false && info.error.length > 0, `${why} is refused with a reason`,
      `got ok=${info.ok}`);
  }
  ok(parseLink('https://user:secret@example.com/x').error.includes('username and password'),
    'the credentials message explains what it saw');
  ok(parseLink('youtube.com/watch?v=x').ok === true, 'a link without https:// is still understood');
  ok(parseLink('example.com:8080/x').ok === false, 'a bare host with a port is refused, not guessed');

  section('ever format the page is told about is either supported or named as not');
  for (const url of ['https://youtu.be/x', 'https://instagram.com/p/y/',
    'https://facebook.com/reel/z', 'https://example.com/a']) {
    const info = classifyUrl(url);
    ok(info.label.length > 0 && info.alternative.length > 0 && info.limit.length > 0,
      `${info.platform} explains itself in plain words`, info.label);
  }
}

/** The timecode box: HH:MM.mmm and the shapes people actually type. */
function timecodeSection() {
  section('reading a time out of the box');
  const good = [
    ['1:23.500', 83.5],
    ['0:00.000', 0],
    ['01:23:45.678', 5025.678],
    ['1:23', 83],
    ['83.5', 83.5],
    ['1:23,500', 83.5],          // a decimal comma, which is normal on a French keyboard
    ['  2:00.250  ', 120.25],
    ['0:59.999', 59.999],
  ];
  for (const [text, seconds] of good) {
    const parsed = parseTimecode(text);
    ok(parsed.ok && Math.abs(parsed.seconds - seconds) < 1e-9,
      `"${text}" -> ${seconds}s`, parsed.ok ? `got ${parsed.seconds}` : parsed.error);
  }

  section('times that are not times, each with its own message');
  const bad = [
    ['', 'empty'],
    ['abc', 'letters'],
    ['1:2:3:4', 'too many colons'],
    ['1::2', 'an empty part'],
    ['1:75', 'seconds of 75'],
    ['1:2:75', 'seconds of 75 in a long form'],
    ['1:70:00', 'minutes of 70'],
    ['-5', 'a negative time'],
    ['1.5.5', 'two decimal points'],
  ];
  for (const [text, why] of bad) {
    const parsed = parseTimecode(text);
    ok(parsed.ok === false && parsed.error.length > 0, `${why} is refused with a reason`,
      `got ${JSON.stringify(parsed)}`);
  }

  section('the time is checked against the video, not just parsed');
  ok(checkTimecode('0:10.000', 33.6).ok, 'a time inside the video is accepted');
  const past = checkTimecode('1:00.000', 33.6);
  ok(past.ok === false && /past the end/.test(past.error), 'a time past the end is refused', past.error);
  ok(/33\.60s/.test(past.error), 'and the message says how long the video actually is');
  ok(checkTimecode('0:33.600', 33.6).ok === false,
    'the exact final instant is refused, because no frame is left to show');
  ok(checkTimecode('0:33.599', 33.6).ok, 'the last frame just before it is accepted');
  ok(checkTimecode('1:00', 0).ok === false, 'an unknown duration cannot vouch for a time');

  section('turning seconds back into a readable time');
  ok(formatTimecode(0) === '0:00.000', 'zero reads as 0:00.000', formatTimecode(0));
  ok(formatTimecode(83.5) === '1:23.500', '83.5s reads as 1:23.500', formatTimecode(83.5));
  ok(formatTimecode(5025.678) === '1:23:45.678', 'over an hour keeps the hours', formatTimecode(5025.678));
}

/** The quality story: say what the resolution really is. */
function qualitySection() {
  section('saying what the resolution is, and what it means');
  ok(describeResolution(1920, 1080) === '1920 × 1080', describeResolution(1920, 1080));
  ok(describeResolution(0, 0) === 'size unknown', 'a picture with no size says so');
  ok(qualityNote(1920, 1080).level === 'high', '1080p counts as full detail');
  ok(qualityNote(1280, 720).level === 'medium', '720p is moderate, and says so');
  ok(qualityNote(640, 480).level === 'low', 'a small source is called small');
  ok(/cannot be sharper/.test(qualityNote(640, 480).text),
    'and it explains that the PDF cannot add detail the source never had');
  ok(!/original quality/i.test(qualityNote(640, 480).text + qualityNote(1920, 1080).text),
    'nothing anywhere claims "original quality"');
}

/**
 * The PDF box: paper, orientation, fit - and the promise that a selected picture is
 * never quietly dropped. The writer is the one already tested above; these are the
 * combinations the box can produce.
 */
function pdfOptionsSection() {
  section('the paper and the orientation the box offers');
  const jpeg = JPEG_2x2;
  const wide = { jpeg, width: 1280, height: 720 };
  const tall = { jpeg, width: 800, height: 1400 };

  const auto = probePdf(buildPdf([wide, tall], {}));
  ok(auto.boxes[0][0] > auto.boxes[0][1] && auto.boxes[1][1] > auto.boxes[1][0],
    'by default each page matches its picture', JSON.stringify(auto.boxes));

  const portrait = probePdf(buildPdf([wide, tall], { orientation: 'portrait' }));
  ok(portrait.boxes.every(([w, h]) => h > w), 'forced portrait is portrait for every page',
    JSON.stringify(portrait.boxes));

  const landscape = probePdf(buildPdf([wide, tall], { orientation: 'landscape' }));
  ok(landscape.boxes.every(([w, h]) => w > h), 'forced landscape is landscape for every page',
    JSON.stringify(landscape.boxes));

  const letter = probePdf(buildPdf([wide], { pageSize: 'letter' }));
  ok(Math.round(letter.boxes[0][0]) === 612 && Math.round(letter.boxes[0][1]) === 792
     || (Math.round(letter.boxes[0][0]) === 792 && Math.round(letter.boxes[0][1]) === 612),
    'Letter is 612x792pt, the right way up', JSON.stringify(letter.boxes[0]));

  const imageSize = probePdf(buildPdf([wide, tall], { pageSize: 'exact' }));
  ok(imageSize.boxes[0][0] === 1280 && imageSize.boxes[1][1] === 1400,
    'image-size pages are exactly the picture, so nothing is rescaled',
    JSON.stringify(imageSize.boxes));

  section('actual size never enlarges the picture');
  const actual = probePdf(buildPdf([wide], { fit: 'actual' }));
  const [aw, ah] = actual.boxes[0];
  ok(Math.round(aw - 1280) === Math.round(2 * 22.7) && Math.round(ah - 720) === Math.round(2 * 22.7),
    'the page grew to fit the picture rather than shrinking it', `${aw}x${ah}`);

  section('a bad setting is refused, not ignored');
  for (const options of [{ orientation: 'sideways' }, { fit: 'squash' }]) {
    let threw = null;
    try { buildPdf([wide], options); } catch (error) { threw = error.message; }
    ok(threw !== null && /Unknown/.test(threw), `${JSON.stringify(options)} is refused`, String(threw));
  }

  section('every selected picture is in the file');
  for (const count of [1, 5, 12]) {
    const pages = Array.from({ length: count }, (_, i) => ({
      jpeg, width: 100 + i, height: 200 + i,
    }));
    const probed = probePdf(buildPdf(pages, {}));
    ok(probed.pageCount === count, `${count} pictures -> ${count} pages in the PDF`,
      `got ${probed.pageCount}`);
  }
  ok(probePdf(buildPdf([wide, tall], { pageNumbers: true })).pageCount === 2,
    'page numbers do not disturb the page count');
}

/** Lossless embedding, including the case JPEG cannot handle: transparency. */
async function losslessSection() {
  section('lossless pictures, with transparency kept');
  const w = 6, h = 3;
  const rgba = new Uint8Array(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    rgba[i * 4] = (i * 40) % 256;
    rgba[i * 4 + 1] = 255 - ((i * 20) % 256);
    rgba[i * 4 + 2] = 128;
    rgba[i * 4 + 3] = i === 0 ? 0 : (i === 1 ? 128 : 255);
  }
  const parts = await encodeLossless(rgba, w, h);
  ok(parts.rgbDeflated.length > 0, 'the pixels were deflated');
  ok(parts.alphaDeflated !== undefined, 'a picture with transparency gets a mask');
  ok(parts.width === w && parts.height === h, 'and keeps its own size');

  const pdf = buildPdf([parts], { pageSize: 'exact' });
  const probed = probePdf(pdf);
  ok(probed.pageCount === 1, 'the lossless page is written');
  ok(probed.boxes[0][0] === w && probed.boxes[0][1] === h, 'at exactly the picture size',
    JSON.stringify(probed.boxes[0]));

  const opaque = new Uint8Array(4 * 4 * 4).fill(255);
  const flat = await encodeLossless(opaque, 4, 4);
  ok(flat.alphaDeflated === undefined, 'a picture with no transparency skips the mask');

  section('lossless data that does not add up is refused');
  const short = await encodeLossless(new Uint8Array(8), 10, 10).then(() => null, (e) => e.message);
  ok(short !== null && /shorter than/.test(short), 'too little data for the stated size is refused', String(short));
  const noSize = await encodeLossless(new Uint8Array(4), 0, 0).then(() => null, (e) => e.message);
  ok(noSize !== null && /no size/.test(noSize), 'a picture with no size is refused', String(noSize));
}

// --------------------------------------------------------------------------- //

async function main() {
  try {
    ffmpegPath();
  } catch (error) {
    // An environment that cannot run the tests is not the same as failing code:
    // say so plainly and exit with a code no one will read as "the app is broken".
    console.error(`\nCannot run these tests here: ${error.message}\n`);
    process.exit(2);
  }

  fixtureSection();
  sampleSection();
  mediaSection();
  pdfSection();
  filenameSection();
  linkSection();
  timecodeSection();
  qualitySection();
  pdfOptionsSection();
  await losslessSection();

  console.log();
  if (failures) {
    console.log(`${checks} checks, ${failures} failure(s).`);
    process.exit(1);
  }
  console.log(`${checks} checks, 0 failures - the engine, the sample and the PDF writer agree.`);
}

main();
