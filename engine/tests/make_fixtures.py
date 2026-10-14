"""Build synthetic lecture videos with KNOWN ground truth, so the detector can be
scored instead of eyeballed.

    python tests/make_fixtures.py

Writes to tests/fixtures/*.mp4 plus a matching *.truth.json describing, for each
page, the time window it is on screen and the time it is *finished* (which is the
instant a good capture should land on).

The fixtures deliberately include the cases that break naive scene detection:
  * a black leader + title card,
  * a cross-fade transition,
  * a page that is written on progressively for 10+ seconds,
  * a page that is scrolled slowly from top to bottom,
  * a static page with compression noise (must yield exactly one page).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pagesnap.ffmpegio import ffmpeg_exe  # noqa: E402

W, H, FPS = 1280, 720, 8
OUT_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

INK = (28, 32, 38)
ACCENT = (196, 48, 48)


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


PAGE_CONTENT = {
    0: [  # title card
        ("Chapter 6", 72, ACCENT, 250),
        ("Life Processes", 52, INK, 350),
        ("- nutrition in plants -", 30, (120, 120, 120), 440),
    ],
    1: [  # definition + equation, revealed line by line while "writing"
        ("1. What is photosynthesis?", 40, ACCENT, 120),
        ("It is the process by which green plants make", 31, INK, 200),
        ("their own food using sunlight, water and", 31, INK, 246),
        ("carbon dioxide from the air.", 31, INK, 292),
        ("6CO2 + 6H2O + light energy -> C6H12O6 + 6O2", 30, (20, 60, 140), 370),
        ("This happens inside the chloroplast.", 31, INK, 440),
        ("Chlorophyll is the green pigment that traps", 31, INK, 510),
        ("the light energy needed for this reaction.", 31, INK, 556),
    ],
    2: [  # bulleted list - a very different layout
        ("2. Where does it happen?", 40, ACCENT, 120),
        ("  * Chloroplast  - the kitchen of the cell", 31, INK, 210),
        ("  * Thylakoid    - light reaction here", 31, INK, 262),
        ("  * Stroma       - Calvin cycle here", 31, INK, 314),
        ("  * Stomata      - gas exchange (CO2 in, O2 out)", 31, INK, 366),
        ("Remember: plants also respire, day and night.", 29, (140, 60, 20), 470),
    ],
    3: [  # a "diagram" - boxes and arrows, nothing like text
        ("3. Diagram: flow of energy", 40, ACCENT, 120),
    ],
}


def render_page(page_no: int, progress: float = 1.0, texture: bool = True) -> Image.Image:
    """One page of notes. `progress` 0..1 reveals the body lines one by one,
    imitating a teacher writing while the video keeps running."""
    canvas = Image.new("RGB", (W, H), (250, 249, 246))
    d = ImageDraw.Draw(canvas)

    if texture:  # a permanent header that never changes between pages
        d.rectangle([0, 0, W, 74], fill=ACCENT)
        d.text((28, 18), "CLASS 10 BIOLOGY   |   Chapter 6 - Life Processes",
               font=_font(30), fill=(255, 255, 255))
        d.text((W - 150, H - 54), f"page {page_no + 1}", font=_font(24), fill=(150, 150, 150))

    lines = PAGE_CONTENT[page_no]
    visible = progress * len(lines)
    for i, (text, size, colour, y) in enumerate(lines):
        reveal = visible - i
        if reveal <= 0:
            break
        if 0 < reveal < 1:                      # a pen stroke being drawn right now
            text = text[: max(1, int(len(text) * reveal))]
        d.text((70, y), text, font=_font(size), fill=colour)

    if page_no == 3:  # the diagram only appears once the heading has settled
        if progress >= 1.0:
            boxes = [("Sun", 150), ("Leaf", 480), ("Glucose", 830)]
            for label, x in boxes:
                d.rectangle([x, 300, x + 220, 400], outline=(20, 60, 140), width=4)
                d.text((x + 60, 335), label, font=_font(34), fill=INK)
            for x in (370, 700):
                d.line([x, 350, x + 110, 350], fill=(20, 60, 140), width=6)
                d.polygon([(x + 110, 340), (x + 130, 350), (x + 110, 360)], fill=(20, 60, 140))
    return canvas


TALL_LINES = [
    "Energy from the sun is captured by the chlorophyll.",
    "Water is split into hydrogen and oxygen.",
    "Oxygen is released through the stomata.",
    "Hydrogen combines with carbon dioxide.",
    "This forms glucose, the plant's food.",
    "Glucose is stored as starch in the leaves.",
]


def render_tall_page(scroll: float) -> Image.Image:
    """A long page taller than the screen; `scroll` 0..1 slides it past the lens."""
    tall = Image.new("RGB", (W, H * 3), (250, 249, 246))
    d = ImageDraw.Draw(tall)
    d.text((60, 40), "Long handwritten page", font=_font(44), fill=ACCENT)
    for i in range(60):
        y = 150 + i * 34
        d.text((70, y), f"{i+1}. Step {i+1}: " + TALL_LINES[i % len(TALL_LINES)],
               font=_font(24), fill=INK)
    top = int(scroll * (tall.height - H))
    return tall.crop((0, top, W, top + H))


def encode(path: str, frames) -> None:
    proc = subprocess.Popen(
        [ffmpeg_exe(), "-y", "-v", "error", "-nostdin",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", path],
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for f in frames:
        proc.stdin.write(np.asarray(f, dtype=np.uint8).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed writing {path}")


def n_frames(seconds: float) -> int:
    return int(round(seconds * FPS))


def crossfade(a: Image.Image, b: Image.Image, alpha: float) -> Image.Image:
    return Image.blend(a, b, alpha)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def build_lecture() -> dict:
    """The realistic one: title -> fade -> page written live -> hard cut -> fade."""
    black = Image.new("RGB", (W, H), (0, 0, 0))
    title = render_page(0)
    p1 = render_page(1)
    p2 = render_page(2)
    p3 = render_page(3)

    def frames():
        for _ in range(n_frames(1.0)):                       # black leader
            yield black
        for _ in range(n_frames(2.6)):                       # title card
            yield title
        for k in range(n_frames(0.5)):                       # cross-fade in
            yield crossfade(title, p1, (k + 1) / n_frames(0.5))
        write_start, write_end, hold_end = 4.1, 13.0, 16.0   # p1 written live
        for k in range(n_frames(hold_end - 4.1)):
            t = 4.1 + k / FPS
            progress = 0.0 if t < write_start else min(1.0, (t - write_start) / (write_end - write_start))
            if t < write_start:
                yield p1
            else:
                yield render_page(1, progress)
        for _ in range(n_frames(8.0)):                       # p2 hard cut
            yield p2
        for k in range(n_frames(0.6)):                       # fake out: fade through grey
            yield crossfade(p2, p3, (k + 1) / n_frames(0.6))
        for _ in range(n_frames(9.0)):                       # p3
            yield p3

    truth = {
        "video": "lecture.mp4",
        "duration": 1.0 + 2.6 + 0.5 + (16.0 - 4.1) + 8.0 + 0.6 + 9.0,
        # "acceptable" = the window in which a screenshot of this page is a
        # correct, complete capture: from the moment the page is fully readable
        # until the moment it leaves the screen.
        "pages": [
            {"id": "title", "on_screen": [1.0, 4.1], "acceptable": [1.25, 4.05]},
            {"id": "p1_written_live", "on_screen": [4.6, 16.0], "acceptable": [12.9, 15.95]},
            {"id": "p2_hard_cut", "on_screen": [16.0, 24.0], "acceptable": [16.05, 23.95]},
            {"id": "p3_faded_in", "on_screen": [24.6, 33.6], "acceptable": [24.7, 33.5]},
        ],
        # instants that must NOT become their own page: mid-fade, mid-stroke,
        # the black leader, and the cross-fade into p3.
        "forbidden": [0.5, 4.6, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 23.8, 24.2],
    }
    encode(os.path.join(OUT_DIR, "lecture.mp4"), frames())
    return truth


def build_static() -> dict:
    """A single page for 14s. Exactly one screenshot, please."""
    page = render_page(2)
    rng = np.random.default_rng(7)

    def frames():
        for _ in range(n_frames(14.0)):
            arr = np.asarray(page, dtype=np.int16)
            arr += rng.integers(-3, 4, arr.shape, dtype=np.int16)   # sensor/compression noise
            yield np.clip(arr, 0, 255).astype(np.uint8)

    encode(os.path.join(OUT_DIR, "static.mp4"), frames())
    return {
        "video": "static.mp4", "duration": 14.0,
        # one page on screen for 14s: exactly one screenshot, whenever it is taken
        "pages": [{"id": "only_page", "on_screen": [0.0, 14.0], "acceptable": [0.0, 13.6]}],
        "forbidden": [],
    }


def build_scroll() -> dict:
    """A long page scrolled slowly. One page, captured when the scroll stops."""
    def frames():
        for k in range(n_frames(12.0)):
            t = k / FPS
            # ease-in-out so the movement genuinely stops at both ends
            x = min(1.0, max(0.0, (t - 1.0) / 9.0))
            eased = x * x * (3 - 2 * x)
            yield render_tall_page(eased)
        for _ in range(n_frames(2.0)):
            yield render_tall_page(1.0)

    encode(os.path.join(OUT_DIR, "scroll.mp4"), frames())
    return {
        "video": "scroll.mp4", "duration": 14.0,
        # the top of a long page and the final view are both worth keeping;
        # what must never happen is a screenshot mid-scroll.
        "pages": [
            {"id": "top_of_long_page", "on_screen": [0.0, 1.0], "acceptable": [0.0, 1.2]},
            # the scroll ends at ~10s; anywhere in the final still stretch is a
            # correct capture of the bottom of the page
            {"id": "bottom_of_long_page", "on_screen": [10.5, 14.0], "acceptable": [10.3, 13.9]},
        ],
        "forbidden": [3.0, 5.0, 7.0, 9.0, 11.0],
    }


def degrade(frame: Image.Image, k: int, rng) -> Image.Image:
    """Imitate a phone filming a screen: slight wobble, blur, sensor noise,
    exposure flicker and a vignette. Same content, much harder video."""
    import numpy as _np
    from PIL import Image as _Image
    from PIL import ImageFilter as _ImageFilter

    t = k / FPS
    # geometric wobble: a small perspective shift that never fully settles
    dx = 6.0 * _np.sin(2 * _np.pi * t / 7.0)
    dy = 4.0 * _np.sin(2 * _np.pi * t / 5.0 + 1.0)
    zoom = 1.0 + 0.006 * _np.sin(2 * _np.pi * t / 9.0)
    inv = 1.0 / zoom
    w, h = frame.size
    cx, cy = w / 2, h / 2
    corners = [(cx + (0 - cx) * inv + dx, cy + (0 - cy) * inv + dy),
               (cx + (w - cx) * inv + dx, cy + (0 - cy) * inv + dy),
               (cx + (w - cx) * inv + dx, cy + (h - cy) * inv + dy),
               (cx + (0 - cx) * inv + dx, cy + (h - cy) * inv + dy)]
    # PIL QUAD needs 8 numbers: nw, sw, se, ne order
    quad = (corners[0][0], corners[0][1], corners[3][0], corners[3][1],
            corners[2][0], corners[2][1], corners[1][0], corners[1][1])
    out = frame.transform((w, h), _Image.QUAD, quad, resample=_Image.BILINEAR)

    arr = _np.asarray(out, dtype=_np.float32)
    # exposure flicker + vignette
    flicker = 1.0 + 0.035 * _np.sin(2 * _np.pi * t / 3.0)
    yy, xx = _np.mgrid[0:h, 0:w]
    r = _np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    arr *= (flicker * (1.0 - 0.28 * _np.clip(r - 0.35, 0, None) ** 1.6))[..., None]
    arr += rng.normal(0, 5.0, arr.shape)                     # sensor noise
    arr = _np.asarray(_Image.fromarray(_np.clip(arr, 0, 255).astype(_np.uint8))
                      .filter(_ImageFilter.GaussianBlur(1.1)), dtype=_np.float32)
    return _Image.fromarray(_np.clip(arr, 0, 255).astype(_np.uint8))


def build_camcorder() -> dict:
    """The same lecture, filmed off a screen with a phone. Harder in every way."""
    rng = np.random.default_rng(11)
    black = Image.new("RGB", (W, H), (18, 18, 20))
    title, p1, p2 = render_page(0), render_page(1), render_page(2)

    def frames():
        for k in range(n_frames(0.8)):
            yield degrade(black, k, rng)
        for k in range(n_frames(3.0)):
            yield degrade(title, k, rng)
        base = n_frames(0.8) + n_frames(3.0)
        for k in range(base, base + n_frames(6.0)):
            yield degrade(p1, k, rng)
        base += n_frames(6.0)
        for k in range(base, base + n_frames(7.0)):
            yield degrade(p2, k, rng)

    encode(os.path.join(OUT_DIR, "camcorder.mp4"), frames())
    return {
        "video": "camcorder.mp4", "duration": 0.8 + 3.0 + 6.0 + 7.0,
        "tolerance": 1.6,
        "pages": [
            {"id": "title", "on_screen": [0.8, 3.8], "acceptable": [0.85, 3.75]},
            {"id": "p1", "on_screen": [3.8, 9.8], "acceptable": [3.85, 9.4]},
            # sampling at 3/s can land up to 0.33s either side of a change
            {"id": "p2", "on_screen": [9.8, 16.8], "acceptable": [9.5, 16.7]},
        ],
        # a page change is a hard swap here, so the guard times are the still
        # holds (must not fire on wobble) and the instants just before a swap.
        "forbidden": [3.4, 5.0, 7.0, 9.1],
    }


def build_pause() -> dict:
    """The trap: the teacher writes, PAUSES for ~2.6s, then carries on.

    A detector that triggers on "the picture stopped changing" alone will happily
    screenshot the half-written page during the pause. Ours fires (it is allowed
    to), but the half-written page must be folded into the finished one, so the
    clip still yields exactly one page.
    """
    def frames():
        for _ in range(n_frames(0.8)):
            yield Image.new("RGB", (W, H), (0, 0, 0))
        for k in range(n_frames(12.0)):
            t = 0.8 + k / FPS
            if t < 1.4:
                progress = 0.0
            elif t < 3.4:                     # first burst of writing
                progress = 0.5 * (t - 1.4) / 2.0
            elif t < 6.0:                     # the pause - nothing changes
                progress = 0.5
            elif t < 8.0:                     # second burst
                progress = 0.5 + 0.5 * (t - 6.0) / 2.0
            else:
                progress = 1.0
            yield render_page(1, min(1.0, progress))

    encode(os.path.join(OUT_DIR, "pause.mp4"), frames())
    return {
        "video": "pause.mp4", "duration": 12.8,
        "pages": [{"id": "p1_after_pause", "on_screen": [0.8, 12.8], "acceptable": [8.0, 12.7]}],
        # the pause (3.4-6.0) and the second burst of writing must not produce
        # their own page; the half-written capture has to fold into this one.
        "forbidden": [2.0, 3.0, 4.5, 5.5, 6.8, 7.4],
    }


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    truths = [build_lecture(), build_static(), build_scroll(), build_camcorder(), build_pause()]
    with open(os.path.join(OUT_DIR, "ground_truth.json"), "w") as fh:
        json.dump({"fixtures": truths}, fh, indent=2)
    for t in truths:
        size = os.path.getsize(os.path.join(OUT_DIR, t["video"])) / 1e6
        print(f"  built {t['video']:<14} {t['duration']:5.1f}s  {size:5.2f} MB  "
              f"{len(t['pages'])} expected page(s)")


if __name__ == "__main__":
    main()
