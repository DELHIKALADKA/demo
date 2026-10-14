"""All ffmpeg access goes through here.

We never bundle ffmpeg ourselves: on a laptop we borrow the static binary that
`imageio-ffmpeg` ships, on a phone the Android app uses MediaMetadataRetriever.
That keeps PageSnap dependency-light and 100% offline.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from typing import Iterator, Optional

import numpy as np


class FFmpegNotFound(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """Locate an ffmpeg binary: $PAGESNAP_FFMPEG, then PATH, then imageio-ffmpeg."""
    env = os.environ.get("PAGESNAP_FFMPEG")
    if env and os.path.exists(env):
        return env

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - environment specific
        raise FFmpegNotFound(
            "No ffmpeg found. Install it (apt install ffmpeg) or run "
            "`pip install imageio-ffmpeg`, or set PAGESNAP_FFMPEG=/path/to/ffmpeg"
        ) from exc


def probe_duration(path: str) -> float:
    """Duration in seconds, or 0.0 if we cannot tell.

    ffprobe is not always shipped next to ffmpeg, so we parse ffmpeg's own stderr.
    """
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    for line in proc.stderr.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            try:
                h, m, s = stamp.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                return 0.0
    return 0.0


def sample_gray_frames(
    path: str,
    fps: float,
    width: int,
    height: int,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (timestamp, small grayscale frame) at `fps`.

    One sequential decode of the whole file - this is the fast path. Frames come
    out as raw bytes on a pipe, so nothing is ever written to disk.
    """
    cmd = [
        ffmpeg_exe(), "-v", "error", "-nostdin",
        "-i", path,
        "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    frame_bytes = width * height
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
            yield index / fps, frame
            index += 1
    finally:
        proc.stdout.close()
        proc.wait()


def grab_frame(path: str, t: float, image_format: str = "png") -> Optional[bytes]:
    """Decode the *exact* frame at time `t` at the video's native resolution."""
    fmt = "png" if image_format == "png" else "mjpeg"
    cmd = [
        ffmpeg_exe(), "-v", "error", "-nostdin",
        "-ss", f"{max(t, 0.0):.4f}",   # before -i => fast seek, still frame-accurate
        "-i", path,
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", fmt, "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout or None
