"""Tunable knobs, and the three presets the UI exposes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Sensitivity = Literal["strict", "balanced", "eager"]


@dataclass
class DetectConfig:
    # --- how often we look at the video -------------------------------------
    # 3 looks/second is plenty: slides do not change faster than that, and the
    # analysis pass then costs ~50ms per minute of video instead of minutes.
    analyze_fps: float = 3.0

    # Small grayscale "look" frames used for comparison.
    analysis_width: int = 160
    analysis_height: int = 90

    # --- what counts as "the page changed" ----------------------------------
    # A frame-pair distance is a number in 0..1 (see detect.py for the formula).
    # These defaults come from tests/metric_experiment.py, which measures the
    # 'same page at rest' (max 0.040 on the fixtures) and 'page -> page'
    # (min 0.063) clusters and puts the floor in the gap between them.
    cut_floor: float = 0.055      # instant change: slide jumps to the next slide
    drift_floor: float = 0.075    # the page was filled in / the camera panned
    settle_floor: float = 0.048   # below this the picture is "holding still"

    # Multipliers used against the median ("how noisy is this video at rest?").
    # A webcam wobble or projector flicker must never look like a page change.
    adaptive_cut: float = 2.2
    adaptive_drift: float = 2.6
    adaptive_settle: float = 1.8

    # ...but they are CAPPED. On a video that is always moving (a hand-held pan
    # down a long page) an uncapped multiplier would raise the bar so high that a
    # real page change sails past unnoticed. The cap keeps a real change - which
    # is always a large, sudden jump - detectable no matter how lively the video.
    cut_ceiling: float = 0.200
    drift_ceiling: float = 0.220
    settle_ceiling: float = 0.100

    # --- bookkeeping ---------------------------------------------------------
    settle_seconds: float = 0.34        # after a CUT, the page is instantly final
    drift_settle_seconds: float = 1.80  # after a slow change, wait much longer:
                                        # a teacher pausing mid-sentence must not
                                        # be mistaken for a finished page

    # "Has the page stopped being filled in?" - measured over the drift window.
    # Measured on the fixtures: a page at rest gains 0.000 ink, a page being
    # written on gains +0.24..+1.5, and the noise floor of a wobbling hand-held
    # camera is ~0.13. This limit sits in the gap.
    ink_gain_limit: float = 0.15
    min_page_seconds: float = 1.30      # debounce: no two pages closer than this
    merge_distance: float = 0.045       # below this, two settled captures are the
                                        # same page seen twice (real page changes
                                        # measured 0.063+; dupes 0.01-0.03)
    drop_blank_pages: bool = True   # skip black/title cards
    blank_std: float = 7.0          # a frame with almost no pixel variance is blank
    skip_first_seconds: float = 0.0 # ignore intros/ads
    max_pages: int = 400            # safety valve

    extra: dict = field(default_factory=dict)

    @staticmethod
    def preset(name: Sensitivity) -> "DetectConfig":
        cfg = DetectConfig()
        if name == "strict":
            # Fewer, high-confidence pages. Use when a wrong screenshot is worse
            # than a missing one.
            cfg.cut_floor = 0.080
            cfg.drift_floor = 0.100
            cfg.min_page_seconds = 2.0
        elif name == "eager":
            # Catches subtle page changes (dim scans, camcorder footage of paper).
            cfg.cut_floor = 0.042
            cfg.drift_floor = 0.058
            cfg.min_page_seconds = 1.0
        return cfg
