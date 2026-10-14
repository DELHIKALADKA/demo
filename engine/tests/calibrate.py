"""Read distances off the ground-truth fixtures so the thresholds in config.py are
chosen from measurements instead of invented.

For every fixture we report the two distributions that actually matter:
  * SAME-page drift   - how far a page moves from its own settled start (the
                        DRIFT rule must NOT fire inside this range),
  * DIFFERENT-page     - distance between the settled states of consecutive pages
                        (the DRIFT rule MUST fire inside this range).

    python tests/calibrate.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pagesnap.config import DetectConfig          # noqa: E402
from pagesnap.detect import PageDetector, distance  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def nearest_index(sigs, t: float) -> int:
    return int(np.argmin([abs(s.t - t) for s in sigs]))


def main() -> None:
    with open(os.path.join(FIX, "ground_truth.json")) as fh:
        data = json.load(fh)

    cfg = DetectConfig()
    all_same: list[float] = []
    all_diff: list[float] = []

    for truth in data["fixtures"]:
        path = os.path.join(FIX, truth["video"])
        det = PageDetector(cfg)
        sigs = det.signatures_from_video(path)
        print(f"\n{truth['video']}  ({len(sigs)} samples @ {cfg.analyze_fps}/s)")

        same, diff = [], []
        pages = truth["pages"]
        for p in pages:
            a, b = p["on_screen"]
            lo, hi = nearest_index(sigs, a), nearest_index(sigs, b)
            if hi - lo < 2:
                continue
            n = hi - lo
            base = lo + max(1, n // 5)                      # skip the settle-in edge
            for k in range(base + 1, hi + 1):
                same.append(distance(sigs[base], sigs[k]))
        for p, q in zip(pages, pages[1:]):
            cut_t = (p["on_screen"][1] + q["on_screen"][0]) / 2.0
            a = nearest_index(sigs, cut_t - 1.2)
            b = nearest_index(sigs, cut_t + 1.2)
            diff.append(distance(sigs[a], sigs[b]))

        for name, arr in (("same-page drift", same), ("page->page jump", diff)):
            if arr:
                a = np.array(arr)
                print(f"  {name:<16} n={len(a):3d}  min={a.min():.3f}  p50={np.median(a):.3f}  "
                      f"p90={np.percentile(a, 90):.3f}  max={a.max():.3f}")
        all_same += same
        all_diff += diff

    s, d = np.array(all_same), np.array(all_diff)
    print("\n" + "=" * 74)
    print(f"ALL FIXTURES  same-page max = {s.max():.3f}   page->page min = {d.min():.3f}")
    if d.min() > s.max():
        print(f"Clean separation. Recommended: drift_floor ~= {(s.max() + d.min()) / 2:.3f} "
              f"(midpoint), settle_floor well below {s.max():.3f}")
    else:
        print("Overlap: the two distributions are not separable (check the fixtures).")


if __name__ == "__main__":
    main()
