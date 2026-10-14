"""Score the detector against the ground-truth fixtures.

    python tests/test_detect.py            # run everything
    python tests/test_detect.py -v         # show every detected timestamp

A fixture passes only if ALL of these hold:
  * the number of pages found is exactly right,
  * every page is captured near the moment that page was FINISHED (not mid-fade),
  * none of the "forbidden" timestamps - mid-transition, mid-stroke, mid-scroll -
    produced a screenshot of their own,
  * no two screenshots are near-duplicates of each other.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from pagesnap.config import DetectConfig                  # noqa: E402
from pagesnap.detect import PageDetector, distance, signature  # noqa: E402
from pagesnap.ffmpegio import grab_frame                  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
WINDOW_SLACK = 0.35   # sampling at 3/s can be a third of a second either side
VERBOSE = "-v" in sys.argv


def io_array(png_bytes: bytes):
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("L").resize((160, 90))
    return img


def check_fixture(truth: dict, cfg: DetectConfig, mode: str = "balanced") -> tuple[bool, list[str]]:
    path = os.path.join(FIX, truth["video"])
    problems: list[str] = []

    t0 = time.time()
    report = PageDetector(cfg).detect(path)
    elapsed = time.time() - t0

    pages = report.pages
    expected = truth["pages"]

    if VERBOSE:
        print(f"\n  {truth['video']}  ({report.duration:.1f}s, {report.sampled} samples, "
              f"{elapsed:.2f}s to analyse = {report.duration/max(elapsed,1e-6):.0f}x realtime)")
        for p in pages:
            print(f"     t={p.time:6.2f}s  {p.reason:<6} sharp={p.sharpness:9.1f} score={p.score:.3f}")

    # Each preset has a different promise: 'balanced' must be exact, 'strict' is
    # allowed to miss a page but never to invent one, 'eager' may add pages but
    # must never miss a real one.
    if mode == "balanced" and len(pages) != len(expected):
        problems.append(
            f"expected exactly {len(expected)} page(s) {[e['id'] for e in expected]}, got {len(pages)}"
        )
    if mode == "strict" and len(pages) > len(expected):
        problems.append(f"strict invented {len(pages) - len(expected)} extra page(s)")
    if mode == "eager" and len(pages) < len(expected):
        problems.append(f"eager missed {len(expected) - len(pages)} page(s)")

    used: set[int] = set()
    for exp in expected:
        lo, hi = exp["acceptable"]
        # a screenshot of this page is correct anywhere in its acceptable window
        inside = [i for i, p in enumerate(pages) if lo - 0.35 <= p.time <= hi + 0.35]
        if not inside:
            nearest = min(pages, key=lambda p: min(abs(p.time - lo), abs(p.time - hi)))
            problems.append(
                f"page '{exp['id']}' (correct capture window {lo}-{hi}s, on screen "
                f"{exp['on_screen']}) was never captured; nearest attempt t={nearest.time:.2f}s"
            )
        else:
            used.add(inside[0])

    for t in truth["forbidden"]:
        for p in pages:
            if abs(p.time - t) < 0.34:
                problems.append(
                    f"captured t={p.time:.2f}s, which is inside a transition/stroke/scroll "
                    f"window that must not produce its own page (guard was t≈{t}s)"
                )
                break

    # no two pages may be near-identical (that would mean double-capturing)
    sigs = []
    for p in pages:
        raw = grab_frame(path, p.time)
        if raw is None:
            problems.append(f"could not decode frame at t={p.time:.2f}s")
            continue
        import numpy as np

        arr = np.frombuffer(io_array(raw).tobytes(), dtype=np.uint8).reshape(90, 160)
        sigs.append((p.time, signature(p.time, arr)))
    for i in range(len(sigs) - 1):
        d = distance(sigs[i][1], sigs[i + 1][1])
        if d < 0.035:
            problems.append(
                f"pages at t={sigs[i][0]:.2f}s and t={sigs[i+1][0]:.2f}s are duplicates (d={d:.3f})"
            )

    return (not problems), problems


def main() -> int:
    truth_path = os.path.join(FIX, "ground_truth.json")
    if not os.path.exists(truth_path):
        print("No fixtures yet - building them...")
        import make_fixtures

        make_fixtures.main()
    with open(truth_path) as fh:
        data = json.load(fh)

    configs = {
        "balanced (default)": DetectConfig.preset("balanced"),
        "strict": DetectConfig.preset("strict"),
        "eager": DetectConfig.preset("eager"),
    }

    failures = 0
    for name, cfg in configs.items():
        print(f"\n=== sensitivity: {name} ===")
        for truth in data["fixtures"]:
            ok, problems = check_fixture(truth, cfg, mode=name.split()[0])
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {truth['video']} - {len(truth['pages'])} expected page(s)")
            for p in problems:
                failures += 1
                print(f"         ! {p}")
    print()
    if failures:
        print(f"{failures} problem(s).")
    else:
        print("All fixtures pass at all three sensitivities.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
