"""Pick the distance metric from measurements.

We have two distributions that must not overlap:
  * QUIET  - two frames of the SAME page while nothing is happening.
             (must stay below the cut threshold, or we capture junk)
  * JUMP   - the settled state of one page vs the settled state of the next.
             (must exceed the drift threshold, or we miss pages)

Plus one target we WANT to cross:
  * WRITE  - how far a page drifts while the teacher fills it in. The drift rule
             should fire at the end of writing, so JUMP > drift_floor > WRITE_max
             is the sweet spot, and QUIET must be far below.

Every variant is scored on the same fixtures, so the winner is a measurement, not
an opinion. Run: python tests/metric_experiment.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pagesnap.config import DetectConfig                # noqa: E402
from pagesnap.detect import PageDetector                # noqa: E402

GRID_W, GRID_H = 48, 27
C, R = 6, 6


def features(frame: np.ndarray, grid=(GRID_W, GRID_H)) -> dict:
    w, h = grid
    src_h, src_w = frame.shape
    ys = np.linspace(0, src_h, h + 1).astype(int)
    xs = np.linspace(0, src_w, w + 1).astype(int)
    small = np.empty((h, w), dtype=np.float32)
    for j in range(h):
        rows = frame[ys[j]:max(ys[j + 1], ys[j] + 1)]
        for i in range(w):
            small[j, i] = rows[:, xs[i]:max(xs[i + 1], xs[i] + 1)].mean()

    scale = max(float(small.std()), 12.0)
    norm = np.clip((small - float(small.mean())) / scale, -1.0, 1.0)

    # per-cell ink / structure density maps
    cys = np.linspace(0, h, C + 1).astype(int)
    cxs = np.linspace(0, w, C + 1).astype(int)
    cell_std = np.zeros((C, C), dtype=np.float32)
    cell_abs = np.zeros((C, C), dtype=np.float32)
    for j in range(C):
        for i in range(C):
            patch = norm[cys[j]:cys[j + 1], cxs[i]:cxs[i + 1]]
            cell_std[j, i] = float(patch.std())
            cell_abs[j, i] = float(np.abs(patch).mean())
    return {
        "norm": norm,
        "profile": np.concatenate([norm.mean(axis=1), norm.mean(axis=0)]),
        "cell_std": cell_std,
        "cell_abs": cell_abs,
    }


def d_pixel(a, b):
    return float(np.abs(a["norm"] - b["norm"]).mean())


def d_profile(a, b):
    return float(np.abs(a["profile"] - b["profile"]).mean())


def d_density(a, b):
    s = float(np.abs(a["cell_std"] - b["cell_std"]).mean())
    m = float(np.abs(a["cell_abs"] - b["cell_abs"]).mean())
    return 0.5 * s + 0.5 * m


VARIANTS = {
    "v1 px.5/prof.32/grad.18": lambda a, b: 0.50 * d_pixel(a, b) + 0.32 * d_profile(a, b) + 0.18 * d_density(a, b),
    "v2 px.40/prof.30/den.30": lambda a, b: 0.40 * d_pixel(a, b) + 0.30 * d_profile(a, b) + 0.30 * d_density(a, b),
    "v3 px.34/prof.33/den.33": lambda a, b: 0.34 * d_pixel(a, b) + 0.33 * d_profile(a, b) + 0.33 * d_density(a, b),
    "v4 pixel only":           lambda a, b: d_pixel(a, b),
    "v5 density only":         lambda a, b: d_density(a, b),
    "v6 px.6/den.4":           lambda a, b: 0.60 * d_pixel(a, b) + 0.40 * d_density(a, b),
}


def nearest(sigs, t):
    return int(np.argmin([abs(s.t - t) for s in sigs]))


def main() -> None:
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "ground_truth.json")) as fh:
        data = json.load(fh)

    cfg = DetectConfig()
    quiet, jump, write = {k: [] for k in VARIANTS}, {k: [] for k in VARIANTS}, {k: [] for k in VARIANTS}

    for truth in data["fixtures"]:
        # scroll.mp4 is deliberately always-moving: its motion is gated by the
        # SETTLE rule, not by a distance threshold, so it must not be allowed to
        # inflate the "quiet" distribution (that would force thresholds too high).
        if truth["video"] == "scroll.mp4":
            continue
        path = os.path.join(os.path.dirname(__file__), "fixtures", truth["video"])
        sigs = PageDetector(cfg).signatures_from_video(path)
        # re-derive cheap features from the stored small frames
        feats = [features((s.pixels * 255.0).astype(np.uint8)) for s in sigs]
        pages = truth["pages"]

        for p in pages:
            a, b = p["on_screen"]
            lo, hi = nearest(sigs, a), nearest(sigs, b)
            if hi - lo < 3:
                continue
            base = lo + max(1, (hi - lo) // 5)
            stable = min(3, (hi - base) // 2)
            for k in range(base + 1, min(base + stable + 1, hi + 1)):
                for name, fn in VARIANTS.items():
                    quiet[name].append(fn(feats[base], feats[k]))
            for k in range(base + 1, hi + 1):        # includes the writing window
                for name, fn in VARIANTS.items():
                    write[name].append(fn(feats[base], feats[k]))

        for p, q in zip(pages, pages[1:]):
            cut_t = (p["on_screen"][1] + q["on_screen"][0]) / 2.0
            a, b = nearest(sigs, cut_t - 1.2), nearest(sigs, cut_t + 1.2)
            for name, fn in VARIANTS.items():
                jump[name].append(fn(feats[a], feats[b]))

    print(f"{'variant':<26} {'quiet_max':>10} {'write_max':>10} {'jump_min':>10} {'margin':>8} {'score':>7}")
    print("-" * 76)
    ranked = []
    for name in VARIANTS:
        q, w, j = np.array(quiet[name]), np.array(write[name]), np.array(jump[name])
        margin = j.min() - w.max()
        # we want: jump_min well above drift_floor, quiet_max far below cut_floor,
        # and both thresholds sitting comfortably between the clusters
        separability = (j.min() / max(w.max(), 1e-6)) * (w.max() / max(q.max(), 1e-6))
        ranked.append((separability, name, q.max(), w.max(), j.min(), margin))
    for sep, name, qmax, wmax, jmin, margin in sorted(ranked, reverse=True):
        print(f"{name:<26} {qmax:>10.4f} {wmax:>10.4f} {jmin:>10.4f} {margin:>+8.4f} {sep:>7.2f}")

    print("\nInterpretation: 'margin' > 0 means the drift rule can be tuned to catch the")
    print("end of writing WITHOUT ever firing on a page-to-page jump. Higher 'score' = cleaner split.")
    print("\nSuggested thresholds for the winner:")
    _, best, qmax, wmax, jmin, _ = max(ranked)
    print(f"  cut_floor   = {(qmax + wmax) / 2:.3f}   (quiet stays below)")
    print(f"  drift_floor = {(wmax + jmin) / 2:.3f}   (writing ends above, jumps start above)")


if __name__ == "__main__":
    main()
