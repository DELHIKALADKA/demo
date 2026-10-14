"""Page-change detection - the heart of PageSnap.

The goal
--------
Given a video of someone teaching (slides, a scanned page, a whiteboard), we want
*one* screenshot per page of notes. Not one per frame, not three per fade, not
one per pen stroke.

Why this is not just "scene detection"
--------------------------------------
Off-the-shelf scene detection finds hard cuts. A lecture video has three kinds of
change and only the first is handled by it:

  1. HARD CUT      slide jumps straight to the next slide.     -> want a page.
  2. TRANSITION    a fade/wipe/slide-in lasting 0.3-1.0s.      -> want ONE page,
                   captured *after* the transition has finished, never mid-fade.
  3. PROGRESSIVE   the teacher keeps writing on the SAME page, or the camera
                   pans slowly down a long page.                -> want ONE page,
                   captured when writing/panning finally stops.

So a page is not "a moment where pixels changed a lot". A page is:

        a stretch of video that held still long enough to be read.

Everything below follows from that one idea. We look for *transitions*, then take
the screenshot at the moment the picture settles again. Writing continuously
never settles, so it never fires a trigger by itself; a fade only settles once it
is over, so we capture the finished slide.

The signal
----------
We sample the video at `analyze_fps` (3/second) as tiny 160x90 grayscale frames,
one sequential decode of the whole file. Between two neighbouring samples we
compute a distance in 0..1 from three cheap, complementary measurements, each on
a 48x27 grid after normalising brightness and contrast away:

  * pixels    mean |a-b| over the grid. Says "this is a different picture".
  * profile   row-means and column-means compared as 1-D curves. Robust to small
              shifts, very good at "a block of text appeared here".
  * ink       per-cell structure map (local spread + local contrast), 6x6 cells.
              Says "there is more/less stuff written here" and survives blur.

    d = 0.34*pixels + 0.33*profile + 0.33*ink

Thresholds were not invented: `tests/metric_experiment.py` measures the three
distributions that matter on ground-truth fixtures (same page at rest, page being
written on, page->page jump) and reports where the clusters separate. See
`tests/README.md` for the numbers.

The five rules
--------------
  R1 CUT      d[i] is a local peak and above the adaptive cut threshold
              -> a transition starts here.
  R2 DRIFT    the picture has drifted far from the page we last kept, is holding
              still for a *longer* window, AND has stopped gaining ink -> a page
              was being filled in (or the camera panned) and is now done.
  R3 SETTLE   whichever rule fired, walk forward to the first sample where the
              picture has held still, and capture THERE. This is what makes a
              fade and a slow scroll land on the final, complete page.
  R4 QUALITY  inside the still window, keep the *sharpest* sample, then decode
              that instant at full resolution.
  R5 FOLD     a speculative DRIFT capture that a later page merely extends is
              the same page half-written; it is folded into the complete one.
              Hard cuts are never folded - a cut is a page that really appeared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .config import DetectConfig
from .ffmpegio import sample_gray_frames

# --------------------------------------------------------------------------- #
# working resolution
# --------------------------------------------------------------------------- #

#: comparison grid for pixels/profile (width, height)
GRID_W, GRID_H = 48, 27
#: grid for the ink/structure maps
CELLS = 6
#: below this pixel spread a frame counts as "flat" and we stop normalising it,
#: otherwise pure sensor noise on a blank slide gets amplified into detail.
STD_FLOOR = 12.0

#: weights for the three terms (chosen in tests/metric_experiment.py)
W_PIXEL, W_PROFILE, W_INK = 0.34, 0.33, 0.33

#: R5 thresholds for "b is just a fuller version of a"
FOLD_MAX_LOST_INK = 0.015     # real page changes lose 4-8% of ink, writing < 0.5%
FOLD_MIN_GAIN = 0.04          # it must also be meaningfully fuller


@dataclass
class FrameSig:
    """A cheap fingerprint of one sampled instant of the video."""

    index: int
    t: float
    pixels: np.ndarray        # (GRID_H, GRID_W) float32 in 0..1, contrast-normalised
    profile: np.ndarray       # (GRID_W + GRID_H,) float32
    ink: np.ndarray           # (CELLS, CELLS) local contrast - "how much is written"
    structure: np.ndarray     # (CELLS, CELLS) local spread - "how busy is it"
    sharpness: float          # variance of Laplacian on the analysis frame
    stddev: float             # ~0 => blank frame
    mean: float


def _box_downsample(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    """Area-average any grayscale image down to (h, w). No OpenCV needed."""
    src_h, src_w = frame.shape
    ys = np.linspace(0, src_h, h + 1).astype(int)
    xs = np.linspace(0, src_w, w + 1).astype(int)
    out = np.empty((h, w), dtype=np.float32)
    for j in range(h):
        rows = frame[ys[j]: max(ys[j + 1], ys[j] + 1)]
        for i in range(w):
            out[j, i] = rows[:, xs[i]: max(xs[i + 1], xs[i] + 1)].mean()
    return out


def _normalise(img: np.ndarray) -> np.ndarray:
    """Make frames comparable across exposure and contrast differences.

    A projector that auto-dims, or a phone that hunts exposure, must not look
    like a new page. Subtracting the mean and dividing by the spread removes
    both, and STD_FLOOR stops a blank page from having its noise amplified.
    Layout is preserved - and layout is what says "different page".
    """
    scale = max(float(img.std()), STD_FLOOR)
    return np.clip((img - float(img.mean())) / scale, -1.0, 1.0)


def _cell_maps(norm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-cell 'ink' (local contrast) and 'structure' (local spread) maps."""
    h, w = norm.shape
    ys = np.linspace(0, h, CELLS + 1).astype(int)
    xs = np.linspace(0, w, CELLS + 1).astype(int)
    ink = np.zeros((CELLS, CELLS), dtype=np.float32)
    structure = np.zeros((CELLS, CELLS), dtype=np.float32)
    for j in range(CELLS):
        for i in range(CELLS):
            patch = norm[ys[j]:ys[j + 1], xs[i]:xs[i + 1]]
            ink[j, i] = float(np.abs(patch).mean())
            structure[j, i] = float(patch.std())
    return ink, structure


def _laplacian_variance(img: np.ndarray) -> float:
    p = np.pad(img, 1, mode="edge")
    lap = p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4.0 * p[1:-1, 1:-1]
    return float(lap.var())


def signature(t: float, frame: np.ndarray, index: int = 0) -> FrameSig:
    """Fingerprint one sampled instant."""
    small = _box_downsample(frame.astype(np.float32), GRID_W, GRID_H)
    norm = _normalise(small)
    ink, structure = _cell_maps(norm)
    return FrameSig(
        index=index,
        t=t,
        pixels=norm,
        profile=np.concatenate([norm.mean(axis=1), norm.mean(axis=0)]),
        ink=ink,
        structure=structure,
        sharpness=_laplacian_variance(frame.astype(np.float32)),
        stddev=float(frame.std()),
        mean=float(frame.mean()),
    )


def distance(a: FrameSig, b: FrameSig) -> float:
    """How different are two sampled instants? 0 = identical, 1 = unrelated."""
    d_pixel = float(np.abs(a.pixels - b.pixels).mean())
    d_profile = float(np.abs(a.profile - b.profile).mean())
    d_structure = float(np.abs(a.structure - b.structure).mean())
    d_ink = float(np.abs(a.ink - b.ink).mean())
    d_ink_all = 0.5 * d_structure + 0.5 * d_ink
    return float(np.clip(
        W_PIXEL * d_pixel + W_PROFILE * d_profile + W_INK * d_ink_all, 0.0, 1.0
    ))


def net_ink_gain(sigs: List[FrameSig], i: int, run: int) -> float:
    """How much ink the page gained over the last `run` samples.

    This is the "is the teacher still writing?" probe. It is a *net* measure, so
    the back-and-forth of a wobbling hand-held camera cancels out (~0.1 on the
    fixtures) while a page being filled in only ever grows (+0.46..+1.5).
    """
    j = max(0, i - run)
    return float(sigs[i].ink.sum() - sigs[j].ink.sum())


def is_extension(a: FrameSig, b: FrameSig) -> bool:
    """Is `b` just page `a` with more written on it?

    Manuscripts only grow: when a teacher writes, ink is *added* and nothing is
    rubbed out. A page change replaces content, so it visibly loses ink. Measured
    on the fixtures: writing loses 0.0-0.3% of ink and gains 8-18%; a page change
    loses 4-8% (the title card -> notes case is the tightest at 1.0%).
    """
    ink_a, ink_b = a.ink, b.ink
    total_b = float(ink_b.sum())
    if total_b < 1e-6:
        return False
    lost = float(np.maximum(0.0, ink_a - ink_b).sum()) / total_b
    gain = (total_b - float(ink_a.sum())) / total_b
    return lost <= FOLD_MAX_LOST_INK and gain >= FOLD_MIN_GAIN


# --------------------------------------------------------------------------- #
# detector
# --------------------------------------------------------------------------- #

@dataclass
class PageCandidate:
    time: float
    sharpness: float
    frame_index: int = 0
    score: float = 0.0                       # the sampler distance that triggered it
    reason: str = "cut"                      # first | cut | drift | last
    stddev: float = 0.0
    mean: float = 0.0


@dataclass
class DetectReport:
    pages: List[PageCandidate] = field(default_factory=list)
    duration: float = 0.0
    sampled: int = 0
    distance_series: Optional[np.ndarray] = None
    thresholds: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _cand(sig: FrameSig, reason: str) -> PageCandidate:
    return PageCandidate(
        time=sig.t, sharpness=sig.sharpness, reason=reason,
        frame_index=sig.index, stddev=sig.stddev, mean=sig.mean,
    )


def _is_blank(sig: FrameSig, cfg: DetectConfig) -> bool:
    return sig.stddev < cfg.blank_std


class PageDetector:
    """Detects page changes in a video. Feed it a path, or signatures you made."""

    def __init__(self, config: Optional[DetectConfig] = None):
        self.cfg = config or DetectConfig()

    # -- public API ---------------------------------------------------------

    def signatures_from_video(self, path: str) -> List[FrameSig]:
        cfg = self.cfg
        sigs: List[FrameSig] = []
        for t, frame in sample_gray_frames(
            path, cfg.analyze_fps, cfg.analysis_width, cfg.analysis_height
        ):
            if t < cfg.skip_first_seconds:
                continue
            sigs.append(signature(t, frame, len(sigs)))
        return sigs

    def detect(self, path: str) -> DetectReport:
        return self.detect_from_signatures(self.signatures_from_video(path))

    def detect_from_signatures(self, sigs: List[FrameSig]) -> DetectReport:
        cfg = self.cfg
        report = DetectReport(sampled=len(sigs))

        if len(sigs) < 3:
            report.notes.append("Video too short to analyse.")
            for s in sigs:
                if not _is_blank(s, cfg):
                    report.pages.append(_cand(s, "first"))
            return report

        report.duration = sigs[-1].t

        # ---- 1. distances between neighbouring samples ---------------------
        d = np.array([0.0] + [distance(sigs[i - 1], sigs[i]) for i in range(1, len(sigs))])
        report.distance_series = d

        # Adaptive floors: a video that is never still (webcam wobble, film grain,
        # a projector flicker) must not flag its own noise as page changes.
        quiet = float(np.median(d))
        thr_cut = float(np.clip(quiet * cfg.adaptive_cut, cfg.cut_floor, cfg.cut_ceiling))
        thr_drift = float(np.clip(quiet * cfg.adaptive_drift, cfg.drift_floor, cfg.drift_ceiling))
        thr_settle = float(np.clip(quiet * cfg.adaptive_settle, cfg.settle_floor, cfg.settle_ceiling))
        report.thresholds = {
            "cut": thr_cut, "drift": thr_drift, "settle": thr_settle, "quiet": quiet,
        }

        fps = cfg.analyze_fps
        settle_run = max(1, int(round(cfg.settle_seconds * fps)))
        drift_run = max(settle_run, int(round(cfg.drift_settle_seconds * fps)))

        candidates: List[PageCandidate] = []

        # the first readable page (skips black leader, counts in, title cards)
        first = self._first_readable(sigs, thr_settle, settle_run)
        if first is not None:
            candidates.append(_cand(sigs[first], "first"))

        # ---- 2. walk the timeline -------------------------------------------
        i, n = 1, len(sigs)
        while i < n - 1 and len(candidates) < cfg.max_pages:
            is_peak = d[i] >= d[i - 1] and d[i] >= d[i + 1]
            cut = is_peak and d[i] >= thr_cut

            drift = False
            if not cut and candidates:
                ref = candidates[-1].frame_index
                # a long, quiet window is what makes DRIFT different from "jitter"
                if i - ref >= drift_run:
                    drift = distance(sigs[i], sigs[ref]) >= thr_drift

            if cut or drift:
                if drift:
                    j = self._settled_after(
                        sigs, i, thr_settle, drift_run, ink_gain_limit=cfg.ink_gain_limit
                    )
                else:
                    j = self._settled_after(sigs, i, thr_settle, settle_run)
                if j is not None:
                    too_soon = bool(candidates) and sigs[j].t - candidates[-1].time < cfg.min_page_seconds
                    if not too_soon:
                        page = self._best_in_window(sigs, j, thr_settle, settle_run)
                        page.reason = "drift" if drift else "cut"
                        page.score = float(d[i])
                        candidates.append(page)
                    i = j + 1
                    continue
            i += 1

        # ---- 3. the tail: the last page of the video -------------------------
        if candidates:
            tail = self._best_in_window(sigs, n - 1, thr_settle, settle_run)
            last_ref = candidates[-1]
            if (
                tail.frame_index > last_ref.frame_index
                and distance(sigs[tail.frame_index], sigs[last_ref.frame_index]) > cfg.merge_distance
                and not _is_blank(sigs[tail.frame_index], cfg)
            ):
                tail.reason = "last"
                candidates.append(tail)

        # ---- 4. clean-up -----------------------------------------------------
        pages = self._fold_incomplete(candidates, sigs, d, thr_settle)
        pages = self._merge(pages, sigs, cfg.merge_distance)
        if cfg.drop_blank_pages:
            kept = [p for p in pages if not _is_blank(sigs[p.frame_index], cfg)]
            if len(kept) != len(pages):
                report.notes.append(f"Dropped {len(pages) - len(kept)} blank frame(s).")
            pages = kept

        pages.sort(key=lambda p: p.time)
        report.pages = pages
        return report

    # -- internals ---------------------------------------------------------

    def _is_stable(self, sigs: List[FrameSig], i: int, thr: float, run: int) -> bool:
        if i + run >= len(sigs):
            return False
        return all(distance(sigs[k], sigs[k + 1]) < thr for k in range(i, i + run))

    def _settled_after(
        self, sigs, start: int, thr: float, run: int,
        ink_gain_limit: Optional[float] = None,
    ) -> Optional[int]:
        """First sample at/after `start` where the picture has held still.

        For a slow change (DRIFT) we also require that the page has stopped
        gaining ink, which is what tells "finished page" apart from "still being
        written on" - the two look identical sample-by-sample.
        """
        limit = len(sigs) - run - 1
        for i in range(start, limit + 1):
            if not self._is_stable(sigs, i, thr, run):
                continue
            if ink_gain_limit is not None:
                gain = net_ink_gain(sigs, min(i + run, len(sigs) - 1), run)
                if gain > ink_gain_limit:
                    continue          # still being written on - not a page yet
            return i
        return None

    def _first_readable(self, sigs, thr: float, run: int) -> Optional[int]:
        limit = len(sigs) - run - 1
        for i in range(0, limit + 1):
            if not _is_blank(sigs[i], self.cfg) and self._is_stable(sigs, i, thr, run):
                return i
        return 0

    def _best_in_window(self, sigs, centre: int, thr: float, run: int) -> PageCandidate:
        """R4: inside the still stretch, keep the sharpest sample."""
        lo, hi = max(0, centre - run), min(len(sigs) - 1, centre + run)
        tol = max(thr, 0.05)
        best = centre
        for k in range(lo, hi + 1):
            if distance(sigs[k], sigs[centre]) < tol and sigs[k].sharpness > sigs[best].sharpness:
                best = k
        return _cand(sigs[best], "cut")

    def _fold_incomplete(
        self, cands: List[PageCandidate], sigs, d: np.ndarray, thr_settle: float
    ) -> List[PageCandidate]:
        """R5: drop a capture that a later capture merely *extends*.

        Two conditions must both hold:

          * the later picture has more ink and lost none of it (manuscripts only
            grow - a page change replaces content and visibly loses ink), and
          * nothing sudden happened in between. If the video jumps at any point
            between the two captures, a different page arrived and the earlier
            one was a real page, however similar it looks.

        That second condition is what separates "the teacher filled this page in"
        from "this slide swapped for a similar-looking one".
        """
        out: List[PageCandidate] = []
        for c in cands:
            if out:
                a = out[-1]
                grew_slowly = bool(
                    c.frame_index > a.frame_index
                    and float(d[a.frame_index + 1: c.frame_index + 1].max(initial=0.0)) < thr_settle
                )
                if grew_slowly and is_extension(sigs[a.frame_index], sigs[c.frame_index]):
                    c.reason = a.reason if a.reason in ("first", "last") else c.reason
                    out[-1] = c            # keep the full page, and its timing
                    continue
            out.append(c)
        return out

    def _merge(self, cands: List[PageCandidate], sigs, thr: float) -> List[PageCandidate]:
        """Collapse candidates that are the same page seen twice.

        Jittery footage can hit the cut rule twice for one transition. Comparing
        the *settled* frames (not the noisy neighbours that triggered them) is the
        honest test of "was this really a different page?". We keep the LATER one:
        in a lecture the later frame is the finished page.
        """
        out: List[PageCandidate] = []
        for c in cands:
            if out and distance(sigs[out[-1].frame_index], sigs[c.frame_index]) < thr:
                c.reason = out[-1].reason
                out[-1] = c
            else:
                out.append(c)
        return out


def detect_pages(path: str, config: Optional[DetectConfig] = None) -> DetectReport:
    """Convenience wrapper: detect the pages in `path`."""
    return PageDetector(config).detect(path)
