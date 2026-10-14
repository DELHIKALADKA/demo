/**
 * PageSnap engine, in plain JavaScript, for the browser.
 *
 * This is a faithful port of engine/pagesnap/detect.py and the Kotlin in
 * android/app/src/main/java/app/pagesnap/engine/PageDetector.kt. It runs entirely
 * inside the page: it reads a video the student chose from their own device, finds
 * the pages, and never sends a byte anywhere. That is what makes it possible to
 * share as a public link with no server behind it.
 *
 * The idea, in one line: a page is not "a moment where pixels changed a lot", it
 * is a stretch of video that held still long enough to be read. So we fire on
 * transitions and capture where the picture settles.
 */

export const DEFAULTS = {
  analyzeFps: 3,
  analysisWidth: 160,
  analysisHeight: 90,
  gridW: 48,
  gridH: 27,
  cells: 6,
  stdFloor: 12.0,
  wPixel: 0.34,
  wProfile: 0.33,
  wInk: 0.33,
  foldMaxLostInk: 0.015,
  foldMinGain: 0.04,
};

/** The three ways someone can ask the detector to behave. */
export const PRESETS = {
  balanced: {
    cutFloor: 0.055, driftFloor: 0.075, settleFloor: 0.048,
    adaptiveCut: 2.2, adaptiveDrift: 2.6, adaptiveSettle: 1.8,
    cutCeiling: 0.200, driftCeiling: 0.220, settleCeiling: 0.100,
    settleSeconds: 0.34, driftSettleSeconds: 1.80, inkGainLimit: 0.15,
    minPageSeconds: 1.30, mergeDistance: 0.045,
    dropBlankPages: true, blankStd: 7.0, maxPages: 400,
  },
  strict: {
    cutFloor: 0.080, driftFloor: 0.100, settleFloor: 0.048,
    adaptiveCut: 2.2, adaptiveDrift: 2.6, adaptiveSettle: 1.8,
    cutCeiling: 0.200, driftCeiling: 0.220, settleCeiling: 0.100,
    settleSeconds: 0.34, driftSettleSeconds: 1.80, inkGainLimit: 0.15,
    minPageSeconds: 2.00, mergeDistance: 0.045,
    dropBlankPages: true, blankStd: 7.0, maxPages: 400,
  },
  eager: {
    cutFloor: 0.042, driftFloor: 0.058, settleFloor: 0.048,
    adaptiveCut: 2.2, adaptiveDrift: 2.6, adaptiveSettle: 1.8,
    cutCeiling: 0.200, driftCeiling: 0.220, settleCeiling: 0.100,
    settleSeconds: 0.34, driftSettleSeconds: 1.80, inkGainLimit: 0.15,
    minPageSeconds: 1.00, mergeDistance: 0.045,
    dropBlankPages: true, blankStd: 7.0, maxPages: 400,
  },
};

export const REASONS = {
  FIRST: 'first readable page of the video',
  CUT: 'the video cut straight to a new page',
  DRIFT: 'the page stopped changing (writing finished)',
  LAST: 'final page of the video',
};

// --------------------------------------------------------------------------- //
// signatures
// --------------------------------------------------------------------------- //

/** Area-average a grayscale frame down to outW x outH. */
export function boxDownsample(gray, w, h, outW, outH) {
  const out = new Float32Array(outH * outW);
  for (let oy = 0; oy < outH; oy++) {
    const y0 = Math.floor((oy * h) / outH);
    const y1 = Math.max(Math.floor(((oy + 1) * h) / outH), y0 + 1);
    for (let ox = 0; ox < outW; ox++) {
      const x0 = Math.floor((ox * w) / outW);
      const x1 = Math.max(Math.floor(((ox + 1) * w) / outW), x0 + 1);
      let acc = 0, n = 0;
      for (let y = y0; y < y1; y++) {
        const row = y * w;
        for (let x = x0; x < x1; x++) { acc += gray[row + x]; n++; }
      }
      out[oy * outW + ox] = acc / Math.max(n, 1);
    }
  }
  return out;
}

/**
 * Fingerprint one sampled instant.
 * Brightness and contrast are normalised away, because a projector auto-dimming
 * or a phone hunting exposure must not look like a new page. `stdFloor` stops a
 * blank page's sensor noise from being amplified into "detail".
 */
export function signature(index, t, gray, srcW, srcH) {
  const { gridW, gridH, cells, stdFloor } = DEFAULTS;
  const small = boxDownsample(gray, srcW, srcH, gridW, gridH);

  let sum = 0;
  for (let i = 0; i < small.length; i++) sum += small[i];
  const mean = sum / small.length;
  let acc = 0;
  for (let i = 0; i < small.length; i++) { const d = small[i] - mean; acc += d * d; }
  const std = Math.sqrt(acc / small.length);
  const scale = Math.max(std, stdFloor);
  const norm = new Float32Array(small.length);
  for (let i = 0; i < small.length; i++) {
    norm[i] = Math.max(-1, Math.min(1, (small[i] - mean) / scale));
  }

  const profile = new Float32Array(gridW + gridH);
  for (let y = 0; y < gridH; y++) {
    let row = 0;
    for (let x = 0; x < gridW; x++) row += norm[y * gridW + x];
    profile[y] = row / gridW;
  }
  for (let x = 0; x < gridW; x++) {
    let col = 0;
    for (let y = 0; y < gridH; y++) col += norm[y * gridW + x];
    profile[gridH + x] = col / gridH;
  }

  const ink = new Float32Array(cells * cells);
  const structure = new Float32Array(cells * cells);
  for (let cy = 0; cy < cells; cy++) {
    const y0 = Math.floor((cy * gridH) / cells);
    const y1 = Math.max(Math.floor(((cy + 1) * gridH) / cells), y0 + 1);
    for (let cx = 0; cx < cells; cx++) {
      const x0 = Math.floor((cx * gridW) / cells);
      const x1 = Math.max(Math.floor(((cx + 1) * gridW) / cells), x0 + 1);
      let absSum = 0, sqSum = 0, n = 0;
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const v = norm[y * gridW + x];
          absSum += Math.abs(v); sqSum += v * v; n++;
        }
      }
      const idx = cy * cells + cx;
      ink[idx] = absSum / Math.max(n, 1);
      structure[idx] = Math.sqrt(sqSum / Math.max(n, 1));
    }
  }

  let meanGray = 0;
  for (let i = 0; i < gray.length; i++) meanGray += gray[i];
  meanGray /= gray.length;
  let varGray = 0;
  for (let i = 0; i < gray.length; i++) { const d = gray[i] - meanGray; varGray += d * d; }
  varGray /= gray.length;

  return {
    index, t, pixels: norm, profile, ink, structure,
    sharpness: laplacianVariance(gray, srcW, srcH),
    stddev: Math.sqrt(varGray), mean: meanGray,
    inkTotal: ink.reduce((a, b) => a + b, 0),
  };
}

/** Variance of Laplacian: "how much fine detail is here" - used to pick the
 *  least blurry frame inside a still stretch. */
function laplacianVariance(gray, w, h) {
  if (w < 3 || h < 3) return 0;
  let sum = 0, sumSq = 0, n = 0;
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const lap = gray[i - w] + gray[i + w] + gray[i - 1] + gray[i + 1] - 4 * gray[i];
      sum += lap; sumSq += lap * lap; n++;
    }
  }
  if (!n) return 0;
  const m = sum / n;
  return sumSq / n - m * m;
}

// --------------------------------------------------------------------------- //
// distance and the "still being written?" probe
// --------------------------------------------------------------------------- //

/** How different are two sampled instants? 0 = identical, 1 = unrelated. */
export function distance(a, b) {
  const { wPixel, wProfile, wInk } = DEFAULTS;
  let dPixel = 0;
  for (let i = 0; i < a.pixels.length; i++) dPixel += Math.abs(a.pixels[i] - b.pixels[i]);
  dPixel /= a.pixels.length;

  let dProfile = 0;
  for (let i = 0; i < a.profile.length; i++) dProfile += Math.abs(a.profile[i] - b.profile[i]);
  dProfile /= a.profile.length;

  let dStruct = 0, dInk = 0;
  for (let i = 0; i < a.ink.length; i++) {
    dStruct += Math.abs(a.structure[i] - b.structure[i]);
    dInk += Math.abs(a.ink[i] - b.ink[i]);
  }
  dStruct /= a.ink.length; dInk /= a.ink.length;
  const dInkAll = 0.5 * dStruct + 0.5 * dInk;

  return Math.max(0, Math.min(1, wPixel * dPixel + wProfile * dProfile + wInk * dInkAll));
}

/**
 * Net ink gained over the last `run` samples: the "is the teacher still writing?"
 * probe. It is a net measure, so a wobbling hand-held camera cancels out (~0.13 on
 * the fixtures) while a page being filled in only ever grows (+0.24 .. +1.5),
 * and a finished page gains exactly 0.
 */
export function netInkGain(sigs, i, run) {
  const j = Math.max(0, i - run);
  return sigs[i].inkTotal - sigs[j].inkTotal;
}

/**
 * Is `b` just page `a` with more written on it? Manuscripts only grow: writing
 * adds ink and rubs nothing out, while a page change replaces content and visibly
 * loses ink (measured: writing loses 0.0-0.3%, a page change 4-8%).
 */
export function isExtension(a, b) {
  const totalB = b.inkTotal;
  if (totalB < 1e-6) return false;
  let lost = 0;
  for (let i = 0; i < a.ink.length; i++) lost += Math.max(0, a.ink[i] - b.ink[i]);
  lost /= totalB;
  const gain = (totalB - a.inkTotal) / totalB;
  return lost <= DEFAULTS.foldMaxLostInk && gain >= DEFAULTS.foldMinGain;
}

function median(values) {
  if (!values.length) return 0;
  const copy = Float64Array.from(values).sort();
  const mid = copy.length >> 1;
  return copy.length % 2 ? copy[mid] : (copy[mid - 1] + copy[mid]) / 2;
}

// --------------------------------------------------------------------------- //
// the detector: five rules
// --------------------------------------------------------------------------- //

/**
 * Find the pages. `sigs` comes from the sampler in this file.
 *
 *  R1 CUT      d[i] is a local peak above the adaptive cut threshold.
 *  R2 DRIFT    the picture drifted far from the last kept page, stayed still for a
 *              longer window, and stopped gaining ink -> a page was being filled
 *              in and is now finished.
 *  R3 SETTLE   whichever fired, capture at the first moment the picture holds
 *              still: that is what lands a fade on the finished slide and refuses
 *              to chop a slow scroll into a dozen screenshots.
 *  R4 QUALITY  keep the sharpest sample inside the still stretch.
 *  R5 FOLD     a capture that a later one merely extends, with nothing sudden in
 *              between, was the same page half-finished -> keep the fuller one.
 */
export function detect(sigsIn, presetName = 'balanced') {
  const cfg = PRESETS[presetName] || PRESETS.balanced;
  const notes = [];
  const sigs = sigsIn;
  const n = sigs.length;

  if (n < 3) {
    const first = sigs.find((s) => !isBlank(s, cfg));
    return {
      pages: first ? [candidate(first, 'FIRST')] : [],
      sampled: n, notes: ['Video too short to analyse.'], thresholds: {},
      duration: n ? sigs[n - 1].t : 0,
    };
  }

  const d = new Float64Array(n);
  for (let i = 1; i < n; i++) d[i] = distance(sigs[i - 1], sigs[i]);

  const quiet = median(d);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const thrCut = clamp(quiet * cfg.adaptiveCut, cfg.cutFloor, cfg.cutCeiling);
  const thrDrift = clamp(quiet * cfg.adaptiveDrift, cfg.driftFloor, cfg.driftCeiling);
  const thrSettle = clamp(quiet * cfg.adaptiveSettle, cfg.settleFloor, cfg.settleCeiling);

  const fps = DEFAULTS.analyzeFps;
  const settleRun = Math.max(1, Math.round(cfg.settleSeconds * fps));
  const driftRun = Math.max(settleRun, Math.round(cfg.driftSettleSeconds * fps));

  const isBlank = (sig) => sig.stddev < cfg.blankStd;
  const isStable = (i, thr, run) => {
    if (i + run >= n) return false;
    for (let k = i; k < i + run; k++) if (distance(sigs[k], sigs[k + 1]) >= thr) return false;
    return true;
  };
  const settledAfter = (start, thr, run, inkGainLimit) => {
    const limit = n - run - 1;
    for (let i = start; i <= limit; i++) {
      if (!isStable(i, thr, run)) continue;
      if (inkGainLimit != null &&
          netInkGain(sigs, Math.min(i + run, n - 1), run) > inkGainLimit) continue;
      return i;
    }
    return null;
  };
  const bestInWindow = (centre, thr, run) => {
    const lo = Math.max(0, centre - run);
    const hi = Math.min(n - 1, centre + run);
    const tol = Math.max(thr, 0.05);
    let best = centre;
    for (let k = lo; k <= hi; k++) {
      if (distance(sigs[k], sigs[centre]) < tol && sigs[k].sharpness > sigs[best].sharpness) best = k;
    }
    return candidate(sigs[best], 'CUT');
  };

  const pages = [];

  // the first readable page (skips black leader and countdowns)
  let firstIdx = null;
  for (let i = 0; i <= n - settleRun - 1; i++) {
    if (!isBlank(sigs[i]) && isStable(i, thrSettle, settleRun)) { firstIdx = i; break; }
  }
  if (firstIdx === null) firstIdx = 0;
  if (!isBlank(sigs[firstIdx])) pages.push(candidate(sigs[firstIdx], 'FIRST'));

  let i = 1;
  while (i < n - 1 && pages.length < cfg.maxPages) {
    const isPeak = d[i] >= d[i - 1] && d[i] >= d[i + 1];
    const cut = isPeak && d[i] >= thrCut;

    let drift = false;
    if (!cut && pages.length) {
      const ref = pages[pages.length - 1].frameIndex;
      if (i - ref >= driftRun) drift = distance(sigs[i], sigs[ref]) >= thrDrift;
    }

    if (cut || drift) {
      const j = drift
        ? settledAfter(i, thrSettle, driftRun, cfg.inkGainLimit)
        : settledAfter(i, thrSettle, settleRun, null);
      if (j !== null) {
        const tooSoon = pages.length && sigs[j].t - pages[pages.length - 1].time < cfg.minPageSeconds;
        if (!tooSoon) {
          pages.push({ ...bestInWindow(j, thrSettle, settleRun), reason: drift ? 'DRIFT' : 'CUT', score: d[i] });
        }
        i = j + 1;
        continue;
      }
    }
    i++;
  }

  // the tail: the last page of the video always deserves a screenshot
  if (pages.length) {
    const tail = bestInWindow(n - 1, thrSettle, settleRun);
    const last = pages[pages.length - 1];
    if (tail.frameIndex > last.frameIndex &&
        distance(sigs[tail.frameIndex], sigs[last.frameIndex]) > cfg.mergeDistance &&
        !isBlank(sigs[tail.frameIndex])) {
      pages.push({ ...tail, reason: 'LAST' });
    }
  }

  // R5 then the merge pass
  let out = [];
  for (const c of pages) {
    if (out.length) {
      const a = out[out.length - 1];
      let grewSlowly = c.frameIndex > a.frameIndex;
      if (grewSlowly) {
        for (let k = a.frameIndex + 1; k <= c.frameIndex; k++) {
          if (d[k] >= thrSettle) { grewSlowly = false; break; }
        }
      }
      if (grewSlowly && isExtension(sigs[a.frameIndex], sigs[c.frameIndex])) {
        out[out.length - 1] = {
          ...c,
          reason: (a.reason === 'FIRST' || a.reason === 'LAST') ? a.reason : c.reason,
        };
        continue;
      }
    }
    out.push(c);
  }

  const merged = [];
  for (const c of out) {
    if (merged.length &&
        distance(sigs[merged[merged.length - 1].frameIndex], sigs[c.frameIndex]) < cfg.mergeDistance) {
      merged[merged.length - 1] = { ...c, reason: merged[merged.length - 1].reason };
    } else {
      merged.push(c);
    }
  }
  out = merged;

  if (cfg.dropBlankPages) {
    const kept = out.filter((p) => !isBlank(sigs[p.frameIndex]));
    if (kept.length !== out.length) notes.push(`Dropped ${out.length - kept.length} blank frame(s).`);
    out = kept;
  }

  return {
    pages: out.sort((a, b) => a.time - b.time),
    sampled: n,
    duration: sigs[n - 1].t,
    notes,
    thresholds: { cut: thrCut, drift: thrDrift, settle: thrSettle, quiet },
  };
}

function candidate(sig, reason) {
  return {
    time: sig.t, frameIndex: sig.index, sharpness: sig.sharpness,
    reason, score: 0, stddev: sig.stddev,
  };
}

// --------------------------------------------------------------------------- //
// reading the video: browser side
// --------------------------------------------------------------------------- //

/**
 * Samples the chosen video at 3 frames a second and hands back signatures.
 *
 * A browser has no decoder API that streams frames, so we seek: set currentTime,
 * wait for `seeked`, draw the frame into a 160x90 canvas, read it back as
 * grayscale. Seeking is not instant (~20-60 ms), so a minute of video takes a few
 * seconds - and it happens on the student's own device, offline, with no upload.
 *
 * The big win: we only ever ask for a tiny frame, so the browser downsamples for
 * us and a 1080p lecture never lands in memory at full size.
 */
export async function sampleVideo(video, { fps = DEFAULTS.analyzeFps, onProgress } = {}) {
  if (!isFinite(video.duration) || video.duration <= 0) {
    await new Promise((resolve) => {
      const done = () => { video.removeEventListener('loadedmetadata', done); resolve(); };
      video.addEventListener('loadedmetadata', done);
    });
  }
  const duration = video.duration || 0;
  const w = DEFAULTS.analysisWidth, h = DEFAULTS.analysisHeight;

  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  const sigs = [];
  const step = 1 / fps;
  let index = 0;
  for (let t = 0; t < duration; t += step) {
    await seek(video, t);
    ctx.drawImage(video, 0, 0, w, h);
    const data = ctx.getImageData(0, 0, w, h).data;
    const gray = new Uint8Array(w * h);
    for (let i = 0, p = 0; i < gray.length; i++, p += 4) {
      gray[i] = (data[p] * 77 + data[p + 1] * 150 + data[p + 2] * 29) >> 8;
    }
    sigs.push(signature(index++, t, gray, w, h));
    if (onProgress && index % 4 === 0) onProgress(Math.min(1, t / duration));
  }
  return sigs;
}

function seek(video, t) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      video.removeEventListener('seeked', finish);
      clearTimeout(timer);
      resolve();
    };
    // If a seek silently fails (some files at the very end), carry on rather than
    // hanging the whole job.
    const timer = setTimeout(finish, 1500);
    video.addEventListener('seeked', finish);
    try { video.currentTime = Math.min(t, Math.max(0, (video.duration || t) - 0.001)); }
    catch (_) { finish(); }
  });
}

/** Full-resolution grab of one instant - this is the actual screenshot. */
export function grabFrame(video, canvas, seconds, maxWidthPx = 0) {
  const natural = { w: video.videoWidth, h: video.videoHeight };
  let w = natural.w, h = natural.h;
  if (maxWidthPx && w > maxWidthPx) {
    h = Math.round((h * maxWidthPx) / w);
    w = maxWidthPx;
  }
  canvas.width = w; canvas.height = h;
  canvas.getContext('2d').drawImage(video, 0, 0, w, h);
  return { canvas, width: w, height: h };
}

export function seekTo(video, seconds) {
  return seek(video, seconds);
}
