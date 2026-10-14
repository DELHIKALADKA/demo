# How the page detector was tuned (and how to re-tune it)

Everything here is a measurement, not an opinion. There are five synthetic videos
with **known ground truth** in `fixtures/`, built by `make_fixtures.py`, and three
scripts that score the detector against them.

```
python tests/make_fixtures.py       # build the videos (~45 s)
python tests/test_detect.py -v      # the actual pass/fail suite
python tests/calibrate.py           # eyeball the distances
python tests/metric_experiment.py   # compare candidate distance metrics
```

## The fixtures

| video | what it tests |
|---|---|
| `lecture.mp4` | black leader → title card → a page **written live for 9 s** → hard cut → cross-fade into a diagram |
| `static.mp4` | one page for 14 s with compression noise → must yield exactly **one** screenshot |
| `scroll.mp4` | a long page scrolled slowly → one screenshot at the top, one at the bottom, **nothing mid-scroll** |
| `camcorder.mp4` | the same lecture filmed off a screen with a phone: wobble, blur, exposure flicker, vignette, noise |
| `pause.mp4` | the trap: writing, a **2.6 s pause**, then more writing → must still be exactly **one** page |

Each page in `ground_truth.json` carries an `acceptable` window: the span of time
in which a screenshot is a *correct* capture of that page (from "the page is fully
readable" until "the page leaves the screen"). A capture anywhere in that window
passes; a capture in `forbidden` (mid-fade, mid-stroke, mid-scroll) fails.

## Measurements that set the thresholds

Metric comparison (`metric_experiment.py`). `quiet` = two frames of the same page
at rest, `jump` = settled state of one page vs the next:

| variant | quiet max | write max | jump min |
|---|---|---|---|
| **0.34 pixels + 0.33 profile + 0.33 ink** | **0.0395** | 0.1491 | **0.0632** |
| 0.40 / 0.30 / 0.30 | 0.0409 | 0.1543 | 0.0647 |
| pixels only | 0.0547 | 0.2065 | 0.0797 |
| ink only | 0.0342 | 0.1137 | 0.0474 |

That is why the weights are 0.34 / 0.33 / 0.33: the "quiet" cluster is tightest
and the gap to a real page change is widest. `cut_floor = 0.055` sits in the gap.

Why "still being written" needed its own rule — per-step distances overlap:

| what the video is doing | per-step distance |
|---|---|
| a page at rest (clean video) | 0.0000 |
| writing on the page | 0.007 – 0.024 |
| a wobbling hand-held camera at rest | 0.009 – 0.033 |

Writing and wobble are in the same range, so *no* threshold separates them. The
**net ink gained over a 1.8 s window** does:

| what the video is doing | net ink gain |
|---|---|
| a page at rest | 0.000 |
| writing on the page | +0.24 … +1.5 |
| a wobbling camera at rest | p50 ≈ +0.13 (oscillation cancels) |

Hence `ink_gain_limit = 0.15`: a page is only "finished" once it has stopped
gaining ink. And for folding half-written pages into finished ones
(`is_extension`): writing loses 0.0–0.3 % of its ink and gains 8–18 %, while a
page change loses 4–8 %, so `FOLD_MAX_LOST_INK = 0.015` and `FOLD_MIN_GAIN = 0.04`.

## What the suite asserts

* **balanced** — exactly the right number of pages, each landing inside its
  `acceptable` window, nothing in `forbidden`, and no two screenshots being
  near-duplicates of each other.
* **strict** — may miss a page, must never invent one.
* **eager** — may add a page, must never miss a real one.

All 15 combinations (5 fixtures × 3 sensitivities) currently pass, and the
analysis runs at roughly 20–35× realtime on a laptop CPU.
