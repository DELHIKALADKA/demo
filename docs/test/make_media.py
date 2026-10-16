"""Build the little videos the browser tests use.

These are not committed - they are generated on demand, the same way the engine's
fixtures are, because binary test media does not belong in the repository.

    python3 docs/test/make_media.py           ->  docs/test/media/*.mp4|mov|webm|mkv

What they are for:

    colours.mp4    three pages in known colours (red, green, blue), each held
                   still for 2s with a hard cut between them. Because the colours
                   are known, the PDF that comes out of the browser can be checked
                   page by page: page 1 must be red, page 2 green, page 3 blue.
                   That is how "the PDF follows the order you see on screen" is
                   tested rather than assumed.
    phone.mov      the same shape, in a QuickTime container - what an iPhone gives
                   you from Files or Photos.
    slides.webm    VP8 in WebM (what Firefox and Chrome record).
    slides.mkv     Matroska (Chrome plays it; Safari cannot - so the app must not
                   promise it).
    hevc.mp4       H.265/HEVC in MP4. iPhones record this, many browsers cannot
                   decode it: the app has to say so clearly instead of hanging.
    fake.mp4       a text file wearing a .mp4 extension.
    broken.mp4     a real MP4 cut in half (no index left in the file).

The frames are drawn with Pillow and piped to the ffmpeg that imageio-ffmpeg
ships, so nothing has to be installed.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")

FPS = 10
W, H = 640, 360
PAGES = [                       # (background, ink, number) - colours are checked in tests
    ((196, 44, 44), (250, 244, 236), "1"),
    ((38, 164, 74), (250, 244, 236), "2"),
    ((48, 88, 198), (250, 244, 236), "3"),
]


def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def page_image(background, ink, number) -> bytes:
    """One page of 'notes': a solid page colour with writing on it.

    Two things matter here and both were learned the hard way:

    * the page cannot be a flat colour - the detector drops a featureless frame as
      blank, exactly like a blank page in a real lecture recording;
    * the pages cannot differ *only* in colour - the detector normalises brightness
      and contrast away on purpose (so a projector dimming is not mistaken for a new
      page), which makes two pages with identical writing and different ink colours
      look like the same page. So each page gets a different layout, while its
      dominant colour stays unmistakable for the tests that read the PDF back.
    """
    img = Image.new("RGB", (W, H), background)
    d = ImageDraw.Draw(img)
    d.rectangle([28, 24, W - 28, H - 24], outline=ink, width=3)
    d.text((52, 44), f"Page {number}", fill=ink)

    if number == "1":                       # a numeral and plain ruled lines
        for i, (top, bottom) in enumerate(((0.14, 0.34), (0.40, 0.60), (0.14, 0.34))):
            x0 = W - 200 + i * 52
            d.rectangle([x0, H * top + 60, x0 + 40, H * bottom + 60], fill=ink)
        for row in range(3):
            y = 150 + row * 40
            d.line([(60, y), (W - 300 - row * 50, y)], fill=ink, width=5)
    elif number == "2":                     # a grid of boxes and a full page of lines
        for row in range(3):
            for col in range(3):
                x0, y0 = 350 + col * 80, 70 + row * 80
                d.rectangle([x0, y0, x0 + 62, y0 + 62], outline=ink, width=4)
        for row in range(7):
            y = 90 + row * 32
            d.line([(60, y), (280 - (row % 3) * 40, y)], fill=ink, width=5)
    else:                                   # a circle with diagonals through it
        d.ellipse([380, 80, 570, 270], outline=ink, width=6)
        d.line([(380, 80), (570, 270)], fill=ink, width=5)
        d.line([(570, 80), (380, 270)], fill=ink, width=5)
        for row in range(4):
            d.line([(60, 110 + row * 46), (60 + 250 - row * 30, 110 + row * 46)], fill=ink, width=6)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def encode(path: str, frames: list[bytes], extra: list[str]) -> None:
    cmd = [ffmpeg(), "-y", "-v", "error", "-nostdin",
           "-f", "image2pipe", "-framerate", str(FPS), "-i", "-", *extra, path]
    subprocess.run(cmd, input=b"".join(frames), check=True)
    print(f"  built {os.path.basename(path):14} {os.path.getsize(path) / 1024:8.1f} KB")


def hold(frames: list[bytes], seconds: float) -> list[bytes]:
    """Hold every picture still for `seconds` - one page after another.

    Not `frames * n`: with more than one page that alternates between them five times
    a second, and the detector - rightly - refuses to call a flickering picture a page.
    """
    repeats = int(seconds * FPS)
    held = []
    for frame in frames:
        held.extend([frame] * repeats)
    return held


def main() -> int:
    force = "--force" in sys.argv
    os.makedirs(MEDIA, exist_ok=True)

    pages = [page_image(*p) for p in PAGES]
    two = pages[:2]

    # --- the colour-order video: red, green, blue, each held still ------------
    colour_frames = hold([pages[0]], 2) + hold([pages[1]], 2) + hold([pages[2]], 2)
    encode(os.path.join(MEDIA, "colours.mp4"), colour_frames,
           ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"])

    # --- one file per container we make claims about --------------------------
    encode(os.path.join(MEDIA, "phone.mov"), hold(two, 2),
           ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"])
    encode(os.path.join(MEDIA, "slides.webm"), hold(two, 2),
           ["-c:v", "libvpx", "-b:v", "500k", "-pix_fmt", "yuv420p"])
    # VP8 inside Matroska: that way the Matroska check does not also depend on the
    # browser having H.264, which a plain Chromium build often does not.
    encode(os.path.join(MEDIA, "slides.mkv"), hold(two, 2),
           ["-c:v", "libvpx", "-b:v", "500k", "-pix_fmt", "yuv420p"])
    encode(os.path.join(MEDIA, "hevc.mp4"), hold(two, 2),
           ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-tag:v", "hvc1", "-movflags", "+faststart", "-x265-params", "log-level=error"])

    # --- files that must produce a clear message, not a hang ------------------
    with open(os.path.join(MEDIA, "fake.mp4"), "w", encoding="utf-8") as fh:
        fh.write("This is a text file. It is not a video, whatever the extension says.\n")

    # An MP4 keeps its index at the end of the file unless the recorder moved it to the
    # front ("faststart"). Cutting such a file short removes the index - which is what a
    # half-finished download looks like, and what the app has to report clearly.
    whole = os.path.join(MEDIA, ".whole.mp4")
    encode(whole, hold(pages[:2], 2),
           ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"])
    with open(whole, "rb") as fh:
        data = fh.read()
    os.remove(whole)
    with open(os.path.join(MEDIA, "broken.mp4"), "wb") as fh:
        fh.write(data[: max(1024, len(data) // 3)])
    print(f"  built {'broken.mp4':14} {os.path.getsize(os.path.join(MEDIA, 'broken.mp4')) / 1024:8.1f} KB"
          "  (a valid MP4 cut short)")

    print(f"\nMedia written to {os.path.relpath(MEDIA)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
