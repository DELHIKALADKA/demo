"""PageSnap prototype server.

The computer version of the app: a thin UI over the same detection engine the
phone app uses. It exists so the whole flow can be used and judged right now:

    upload a video or import a link  ->  it finds the pages  ->  review, remove,
    reorder, restore, capture extra moments  ->  Generate PDF  ->  preview and
    download  ->  edit again and regenerate

What this file owns
-------------------
* **Importing a source.** A file from this device always works. A link works when
  the site allows it: the server (not a browser) does the fetching, with yt-dlp.
  Nothing here tries to defeat DRM, logins or paywalls - when a platform wants a
  login, the job says so and points at the upload fallback.
* **Job state.** Each job keeps its pages with stable ids, an order, a removed
  set, and the PDF that was generated from that exact state. Editing anything
  bumps a revision, which is how the UI knows a PDF is stale instead of quietly
  handing back an old file.
* **Writing the PDF.** Delegated to the tested engine (`pagesnap.pdf`); this file
  only supplies the pages in their current order and then *verifies* the bytes
  that came back.

Nothing leaves the machine: the only outbound requests are the ones you ask for
when you paste a link.

    python prototype/server.py --port 8000
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile                  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response                  # noqa: E402
from PIL import Image                                                               # noqa: E402

from pagesnap.config import DetectConfig                                            # noqa: E402
from pagesnap.ffmpegio import ffmpeg_exe, grab_frame, probe_duration                 # noqa: E402
from pagesnap.pdf import (CapturedPage, PdfOptions, build_pdf_bytes,                 # noqa: E402
                          contact_sheet, to_pdf_filename)
from pagesnap.pipeline import Job, JobOptions, capture_pages                         # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORK = os.path.join(tempfile.gettempdir(), "pagesnap-jobs")

#: Longest video we will analyse, and the most we will download from a link.
#: Both are here so a pasted playlist of 3-hour lectures fails with a sentence
#: instead of an hour of silence.
MAX_VIDEO_SECONDS = 90 * 60
MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024

#: A direct link to a video file, as opposed to a page that merely contains one.
MEDIA_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".ogv")

app = FastAPI(title="PageSnap prototype")


# --------------------------------------------------------------------------- #
# what went wrong, in words a student can act on
# --------------------------------------------------------------------------- #

@dataclass
class SourceProblem(Exception):
    """A source we could not use, and exactly why. `kind` drives the UI's advice."""

    message: str
    kind: str = "unsupported"      # bad_url | not_a_video | needs_login | drm
    advice: str = ""               # live_stream | geo | unavailable | too_long | network

    def as_dict(self) -> dict:
        return {"message": self.message, "kind": self.kind, "advice": self.advice}


#: yt-dlp's error text, mapped to something honest and useful. Order matters:
#: the first pattern that matches wins.
_YTDLP_RULES: List[Tuple[str, str, str, str]] = [
    (r"login|log in|sign in|account|authentication|cookies|private|rate.?limit",
     "needs_login",
     "That post is private, or the site wants a signed-in account.",
     "This server will not log in on your behalf. Save the video to this device and "
     "upload the file instead, or use the phone app where you sign in once yourself."),
    (r"drm|protected content|widevine|fairplay",
     "drm",
     "That video is copy-protected (DRM).",
     "DRM-protected video cannot be captured, by design and by law."),
    (r"is live|live stream|premieres in|upcoming",
     "live_stream",
     "That link is a live or upcoming stream, so there is nothing finished to snap.",
     "Wait until it is published as a normal video."),
    (r"geo|not available in your country|region",
     "geo",
     "The site is refusing to serve that video in this region.",
     ""),
    # Before the network rule on purpose: a 404 is a *missing video*, not a broken
    # connection, and the two need different advice.
    (r"unavailable|removed|deleted|404|not found|no longer exists|does not exist",
     "unavailable",
     "That video is not there any more, or the link is wrong.",
     ""),
    # `urlopen error` is deliberately NOT here: a refused connection produces it,
    # and that is a network problem, not a bad video.
    (r"unsupported url|no video formats|no video found|no suitable|unsupported",
     "not_a_video",
     "That link is not a video this tool can read - it looks like a web page.",
     "Open the page and save the video file, then upload it here."),
    (r"unable to download webpage|ssl|tls|timed? ?out|connection|refused|network|"
     r"unreachable|name or service|temporary failure|transporterror|urlopen error|"
     r"errno|http error 5",
     "network",
     "The site could not be reached from this computer.",
     "Check the connection, or try again in a moment."),
]


def explain_ytdlp(error: BaseException) -> SourceProblem:
    """Turn a yt-dlp/network exception into a message that explains the cause."""
    text = " ".join(str(error).split())
    for pattern, kind, message, advice in _YTDLP_RULES:
        if re.search(pattern, text, re.I):
            return SourceProblem(message, kind, advice)
    # Nothing matched: show the first line of the real error rather than nothing,
    # and say plainly that we could not classify it.
    first_line = text.split("ERROR:")[-1].strip().split("\n")[0][:300] or text[:300]
    return SourceProblem(
        f"The site refused to serve that video. ({first_line})",
        "unsupported",
        "Saving the video to this device and uploading the file always works.",
    )


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

@dataclass
class Progress:
    stage: str = "queued"
    fraction: float = 0.0


@dataclass
class PageEntry:
    """One captured page. The uid is what makes editing possible."""

    uid: str
    time: float
    reason: str
    image_bytes: bytes
    width: int
    height: int
    thumb: bytes
    label: str = ""
    manual: bool = False


@dataclass
class PdfState:
    status: str = "none"          # none | generating | ready | failed
    stage: str = ""
    fraction: float = 0.0
    revision: int = -1
    page_count: int = 0
    byte_size: int = 0
    filename: str = ""
    data: Optional[bytes] = None
    verified: bool = False
    verify_note: str = ""
    error: str = ""
    created: float = 0.0


@dataclass
class SnapJob:
    id: str
    directory: str
    options: JobOptions
    title: str = ""
    display_source: str = ""                 # what the user called it (name or URL)
    source_path: Optional[str] = None        # the file on disk, or None while importing
    status: str = "queued"                   # queued|downloading|analysing|ready|failed
    error: str = ""
    error_kind: str = ""
    error_advice: str = ""
    progress: Progress = field(default_factory=Progress)
    download: Progress = field(default_factory=Progress)
    job: Optional[Job] = None
    pages: List[PageEntry] = field(default_factory=list)
    order: List[str] = field(default_factory=list)     # uids, front to back
    removed: Set[str] = field(default_factory=set)     # uids the user took out
    revision: int = 0                                  # bumped by every edit
    pdf: PdfState = field(default_factory=PdfState)
    created: float = field(default_factory=time.time)

    # -- helpers -----------------------------------------------------------

    def by_uid(self, uid: str) -> Optional[PageEntry]:
        return next((p for p in self.pages if p.uid == uid), None)

    def selected(self) -> List[PageEntry]:
        return [e for e in (self.by_uid(u) for u in self.order)
                if e is not None and e.uid not in self.removed]

    def touch(self) -> None:
        """Something changed: the generated PDF no longer describes this job."""
        self.revision += 1

    def pdf_is_current(self) -> bool:
        return (self.pdf.status == "ready" and self.pdf.revision == self.revision
                and self.pdf.data is not None)

    def public(self) -> dict:
        selected = {e.uid for e in self.selected()}
        pages = []
        for i, uid in enumerate(self.order):
            entry = self.by_uid(uid)
            if entry is None:
                continue
            pages.append({
                "uid": entry.uid,
                "position": i + 1,
                "thumbnail": f"/api/jobs/{self.id}/thumb/{entry.uid}",
                "full": f"/api/jobs/{self.id}/page/{entry.uid}",
                "time": round(entry.time, 2),
                "reason": entry.reason,
                "label": entry.label or f"page {i + 1}",
                "width": entry.width,
                "height": entry.height,
                "manual": entry.manual,
                "removed": entry.uid in self.removed,
                "selected": entry.uid in selected,
            })
        return {
            "id": self.id,
            "source": self.display_source,
            "source_kind": "link" if self.display_source.startswith(("http://", "https://")) else "file",
            "status": self.status,
            "error": self.error,
            "error_kind": self.error_kind,
            "error_advice": self.error_advice,
            "stage": self.progress.stage,
            "fraction": round(self.progress.fraction, 3),
            "download_stage": self.download.stage,
            "download_fraction": round(self.download.fraction, 3),
            "title": self.title,
            "duration": round(self.job.duration, 1) if self.job else 0,
            "analyzed_seconds": round(self.job.analyzed_seconds, 2) if self.job else 0,
            "pages": pages,
            "page_count": len(self.pages),
            "selected_count": len(selected),
            "removed_count": len(self.removed),
            "revision": self.revision,
            "can_capture": self.source_path is not None and self.status == "ready",
            "notes": self.job.notes if self.job else [],
            "pdf": {
                "status": self.pdf.status,
                "stage": self.pdf.stage,
                "fraction": round(self.pdf.fraction, 3),
                "page_count": self.pdf.page_count,
                "byte_size": self.pdf.byte_size,
                "filename": self.pdf.filename,
                "verified": self.pdf.verified,
                "verify_note": self.pdf.verify_note,
                "error": self.pdf.error,
                "stale": self.pdf.status == "ready" and self.pdf.revision != self.revision,
                "current": self.pdf_is_current(),
            },
        }


JOBS: Dict[str, SnapJob] = {}


def _relabel(stage: str, fraction: float) -> Tuple[str, float]:
    """Map the engine's progress onto friendlier words for the screen."""
    if fraction < 0.1:
        return "reading the video", fraction
    if fraction < 0.5:
        return "looking for page changes", fraction
    if fraction < 1.0:
        return "saving the finished pages", fraction
    return "done", 1.0


def _thumbnail(image_bytes: bytes, width: int = 240) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    ratio = width / float(img.width)
    img = img.resize((width, max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _entry_from_capture(index: int, page: CapturedPage) -> PageEntry:
    return PageEntry(
        uid=uuid.uuid4().hex[:10],
        time=page.source_time,
        reason=page.reason,
        image_bytes=page.image_bytes,
        width=page.width,
        height=page.height,
        thumb=_thumbnail(page.image_bytes),
        label=page.label or f"page {index + 1}",
    )


def _preset_options(sensitivity: str, fmt: str, quality: int, title: str,
                    max_width: int, grayscale: bool) -> JobOptions:
    return JobOptions(
        sensitivity=sensitivity,                   # type: ignore[arg-type]
        detect=DetectConfig.preset(sensitivity),   # type: ignore[arg-type]
        pdf=PdfOptions(image_format=fmt, quality=quality, title=title,
                       max_width=max_width, grayscale=grayscale, page_numbers=False),
    )


def _new_job(options: JobOptions, title: str, display_source: str) -> SnapJob:
    jid = uuid.uuid4().hex[:12]
    directory = os.path.join(WORK, jid)
    os.makedirs(directory, exist_ok=True)
    store = SnapJob(id=jid, directory=directory, options=options, title=title,
                    display_source=display_source)
    JOBS[jid] = store
    return store


def _fail(store: SnapJob, problem: SourceProblem) -> None:
    store.status = "failed"
    store.error = problem.message
    store.error_kind = problem.kind
    store.error_advice = problem.advice
    store.progress = Progress("failed", 0.0)


def _analyse(store: SnapJob) -> None:
    """Find the pages. Runs on a worker thread so the request can return at once."""
    try:
        source = store.source_path
        if not source:
            raise SourceProblem("The video is not ready yet.", "bad_url")

        def report(stage: str, fraction: float) -> None:
            store.progress = Progress(*_relabel(stage, fraction))

        started = time.time()
        job = capture_pages(source, store.options, report)
        job.analyzed_seconds = time.time() - started

        store.job = job
        for i, page in enumerate(job.pages):
            entry = _entry_from_capture(i, page)
            store.pages.append(entry)
            store.order.append(entry.uid)
        store.status = "ready"
        store.progress = Progress("done", 1.0)
        store.touch()

        if not store.pages:
            store.status = "failed"      # an empty result is a failure, not a success
            _fail(store, SourceProblem(
                "No pages were found in that video - it looks like one unbroken shot.",
                "no_pages",
                "If someone is writing in the video, try the Eager setting in Options."))
    except SourceProblem as problem:
        _fail(store, problem)
    except Exception as exc:                      # noqa: BLE001 - surface to the UI
        _fail(store, SourceProblem(f"{type(exc).__name__}: {exc}", "engine"))


# --------------------------------------------------------------------------- #
# importing: a file, or a link
# --------------------------------------------------------------------------- #

def _check_url(url: str) -> str:
    """Refuse anything that is not a plain public http(s) address."""
    url = (url or "").strip()
    if not url:
        raise SourceProblem("Paste a link first.", "bad_url",
                            "Copy the address of the page the video is on, then paste "
                            "it in the box. Or choose a file from this device.")
    if len(url) > 2000:
        raise SourceProblem("That link is implausibly long.", "bad_url")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SourceProblem(
            f"'{parts.scheme or 'that'}' links are not supported - only http and https.",
            "bad_url",
            "Copy the link from the address bar or the app's Share menu.")
    if not parts.netloc or "." not in parts.netloc:
        raise SourceProblem(f"'{url}' is not a valid web address.", "bad_url",
                            "A link looks like https://example.com/video.")

    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise SourceProblem(f"Could not find the site '{host}'.", "network",
                            "Check the spelling, or the connection.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        # 169.254.x.x is the cloud metadata service; nothing good comes of it.
        if address.is_link_local:
            raise SourceProblem("That address is not allowed.", "bad_url")
    return url


def _looks_like_media(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.lower().endswith(MEDIA_SUFFIXES)


#: Containers ffmpeg will "play" that are not video at all. This matters because
#: ffmpeg cheerfully renders a plain text file as a `tty` stream (ANSI art, 640x400)
#: and a still picture as `image2` - so duration alone would say "yes, a video".
_NOT_VIDEO_CONTAINERS = {
    "tty": "plain text",
    "image2": "a single still image",
    "image2pipe": "a single still image",
    "lavfi": "a generated test pattern",
    "txt": "subtitles",
    "srt": "subtitles",
    "sub": "subtitles",
    "ass": "subtitles",
    "sdp": "a stream description",
    "concat": "a file list",
    "dshow": "a capture device",
    "v4l2": "a capture device",
    "x11grab": "a screen grab",
    "gdigrab": "a screen grab",
}


def probe_container(path: str) -> Tuple[str, float, str, str]:
    """What ffmpeg thinks the file is: (container, duration, video codec, reason).

    The container name is the giveaway - a real video says `mov,mp4,m4a,3gp`,
    `matroska,webm`, `avi` or similar, never `tty` or `image2`. When ffmpeg cannot
    open the file at all, `reason` carries its explanation, which is far more
    useful to a student than a generic "could not read".
    """
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    container, duration, codec, reason = "", 0.0, "", ""
    for line in proc.stderr.splitlines():
        found = re.search(r"Input #0,\s*(.+?),\s*from", line)
        if found:
            container = found.group(1).strip().lower()
        found = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", line)
        if found:
            duration = (int(found.group(1)) * 3600 + int(found.group(2)) * 60
                        + float(found.group(3)))
        found = re.search(r"Stream #0:\d+.*?:\s*Video:\s*([A-Za-z0-9_]+)", line)
        if found and not codec:
            codec = found.group(1).lower()
        if not reason:
            found = re.search(r"(?:Error opening input|Invalid data found|\.\w+ @ .*?\] *)(.*)$",
                              line)
            if found and found.group(1).strip():
                reason = found.group(1).strip().lstrip(":").strip()[:160]
    return container, duration, codec, reason


def _verify_video_file(path: str) -> Optional[SourceProblem]:
    """A file that is not a video must never reach the detector."""
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        return SourceProblem("That file is empty.", "not_a_video",
                             "The download or upload produced nothing.")
    container, duration, codec, reason = probe_container(path)
    main = container.split(",")[0].strip()
    if main in _NOT_VIDEO_CONTAINERS:
        return SourceProblem(
            f"That file is not a video - ffmpeg reads it as {_NOT_VIDEO_CONTAINERS[main]}.",
            "not_a_video",
            "MP4, MOV, MKV and WEBM work. Screen recordings and phone videos always work.")
    if duration > 0:
        return None
    if grab_frame(path, 0.0) is not None:
        return None                      # decodable, just missing duration metadata
    detail = f" (ffmpeg says: {reason})" if reason else ""
    return SourceProblem(
        f"That file could not be opened as a video{detail}.", "not_a_video",
        "It may be damaged, or not really the type of file its name suggests. "
        "MP4 (H.264), MOV, MKV and WEBM work.")


def _download_direct(url: str, target_dir: str, store: SnapJob) -> str:
    """Plain HTTP download, for links that are already a video file."""
    request = urllib.request.Request(url, headers={"User-Agent": "PageSnap/0.1"})
    path = os.path.join(target_dir, "source" + (os.path.splitext(
        urllib.parse.urlsplit(url).path)[1] or ".mp4"))
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0]
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_DOWNLOAD_BYTES:
            raise SourceProblem("That file is too large to fetch here.", "too_long")
        if content_type and not content_type.startswith(("video/", "application/octet-stream")):
            raise SourceProblem(
                f"That link served a '{content_type}', not a video.", "not_a_video",
                "Open the page and save the video file, then upload it here.")
        done = 0
        with open(path, "wb") as fh:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_DOWNLOAD_BYTES:
                    raise SourceProblem("That file is too large to fetch here.", "too_long")
                fh.write(chunk)
                if declared:
                    store.download = Progress(
                        f"downloading {done // 1024 // 1024} MB of {declared // 1024 // 1024} MB",
                        min(0.95, done / declared))
                else:
                    store.download = Progress(f"downloading {done // 1024 // 1024} MB",
                                              0.5)
    return path


def _import_link(store: SnapJob, url: str) -> None:
    """Fetch a link on a worker thread, reporting real progress the whole way."""
    try:
        store.status = "downloading"
        store.progress = Progress("checking the link", 0.02)

        try:
            import yt_dlp
        except ImportError:
            store.download = Progress("downloader not installed", 0.0)
            raise SourceProblem(
                "This server has no yt-dlp installed, so it cannot fetch links.",
                "no_downloader",
                "Install it with: pip install yt-dlp   -   or use 'Choose a video' "
                "to upload a file, which needs nothing extra.")

        store.source_path = None
        opts = {
            "outtmpl": os.path.join(store.directory, "source.%(ext)s"),
            "format": "mp4/bestvideo*+bestaudio/best[height<=1080]/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "max_filesize": MAX_DOWNLOAD_BYTES,
            "retries": 2,
            "socket_timeout": 20,
        }
        try:
            # Without this, yt-dlp cannot merge a separate audio+video stream and
            # reports "ffmpeg not found" even though we ship one.
            opts["ffmpeg_location"] = ffmpeg_exe()
        except Exception:                            # noqa: BLE001 - optional
            pass
        cookies = os.environ.get("PAGESNAP_COOKIES")
        if cookies and os.path.exists(cookies):
            # Your own exported cookies, for videos you can already watch. This is
            # not a way around a login: it is the same access you already have.
            opts["cookiefile"] = cookies

        # 1. ask what the link is, before downloading anything
        store.progress = Progress("checking the link", 0.05)
        try:
            with yt_dlp.YoutubeDL(dict(opts, skip_download=True)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:                     # noqa: BLE001
            if _looks_like_media(url):
                # A plain file link that yt-dlp does not know: fetch it ourselves.
                store.progress = Progress("downloading the file", 0.1)
                path = _download_direct(url, store.directory, store)
                store.source_path = path
                info = None
            else:
                raise explain_ytdlp(exc)

        if info is not None:
            if info.get("is_live"):
                raise SourceProblem("That is a live stream, so there is nothing finished "
                                    "to snap.", "live_stream", "Try it after the stream ends.")
            if info.get("has_drm"):
                raise SourceProblem("That video is copy-protected (DRM).", "drm",
                                    "DRM-protected video cannot be captured.")
            duration = info.get("duration") or 0
            if duration and duration > MAX_VIDEO_SECONDS:
                raise SourceProblem(
                    f"That video is {duration / 60:.0f} minutes long; this prototype "
                    f"stops at {MAX_VIDEO_SECONDS // 60}.", "too_long",
                    "Snip a shorter section, or use the phone app.")
            if info.get("title") and not store.title:
                store.title = str(info["title"])[:80]

            # 2. download it
            store.progress = Progress("downloading the video", 0.15)
            store.download = Progress("starting the download", 0.0)
            total = info.get("filesize") or info.get("filesize_approx") or 0

            def hook(status: dict) -> None:
                if status.get("status") == "downloading":
                    done = status.get("downloaded_bytes") or 0
                    size = status.get("total_bytes") or status.get("total_bytes_estimate") or total
                    if size:
                        store.download = Progress(
                            f"downloading {done // 1024 // 1024} MB of {size // 1024 // 1024} MB",
                            min(0.95, done / size))
                    else:
                        store.download = Progress(f"downloading {done // 1024 // 1024} MB",
                                                  0.4)
                elif status.get("status") == "finished":
                    store.download = Progress("download finished", 1.0)

            try:
                with yt_dlp.YoutubeDL(dict(opts, progress_hooks=[hook])) as ydl:
                    ydl.download([url])
            except Exception as exc:                 # noqa: BLE001
                raise explain_ytdlp(exc)

            found = [os.path.join(store.directory, f) for f in os.listdir(store.directory)
                     if f.startswith("source.")]
            found = [f for f in found if not f.endswith(".part")]
            if not found:
                raise SourceProblem("The download finished but produced no file.",
                                    "unsupported", "Try uploading the video file instead.")
            store.source_path = max(found, key=os.path.getsize)

            store.progress = Progress("downloading the video", 0.3)

        # 3. make sure what arrived really is a video, then analyse it
        problem = _verify_video_file(store.source_path or "")
        if problem:
            raise problem
        store.download = Progress("download finished", 1.0)
        _analyse(store)
    except SourceProblem as problem:
        _fail(store, problem)
    except Exception as exc:                          # noqa: BLE001
        _fail(store, explain_ytdlp(exc))


# --------------------------------------------------------------------------- #
# PDF: generate, verify, and refuse to hand back a stale one
# --------------------------------------------------------------------------- #

def _verify_pdf(data: bytes, expected_pages: int, expected_title: str) -> Tuple[bool, int, str]:
    """Check the bytes really are a PDF with the pages we think it has.

    Cheap structural checks first (they catch a truncated or text response), then
    pypdf when it happens to be installed, for a page count from a real parser.
    """
    if not data.startswith(b"%PDF-"):
        return False, 0, "the file does not start with %PDF-"
    if b"%%EOF" not in data[-2048:]:
        return False, 0, "the file has no end-of-file marker (truncated)"
    structural = len(re.findall(rb"/Type\s*/Page[^s]", data))
    try:
        import pypdf                                       # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(data))
        count = len(reader.pages)
        note = f"pypdf read {count} page(s)"
        if count != expected_pages:
            return False, count, f"pypdf found {count} page(s), expected {expected_pages}"
        title = (reader.metadata.title if reader.metadata else None) or ""
        if expected_title and title and title != expected_title:
            note += ", title differs"
        return True, count, note
    except ImportError:
        pass
    except Exception as exc:                               # noqa: BLE001
        return False, structural, f"a PDF reader rejected the file: {exc}"

    if structural != expected_pages:
        return False, structural, (f"{structural} page object(s) found, expected "
                                   f"{expected_pages}")
    return True, structural, f"{structural} page object(s) found (structure check)"


def _generate_pdf(store: SnapJob) -> None:
    """Build the PDF from the *current* order and selection, with progress."""
    try:
        entries = store.selected()
        if not entries:
            raise SourceProblem("No pages are selected - tick at least one.", "selection")

        store.pdf = PdfState(status="generating", stage="preparing the pages",
                             fraction=0.05, revision=store.revision)

        # Loading each page is the slow part, and it is genuinely per-page work -
        # so this is real progress, not a decoration.
        pages: List[CapturedPage] = []
        for i, entry in enumerate(entries):
            store.pdf.stage = f"loading page {i + 1} of {len(entries)}"
            store.pdf.fraction = 0.05 + 0.65 * (i / len(entries))
            pages.append(CapturedPage(
                image_bytes=entry.image_bytes, width=entry.width, height=entry.height,
                source_time=entry.time, reason=entry.reason, label=entry.label))

        store.pdf.stage = f"writing {len(pages)} page(s)"
        store.pdf.fraction = 0.75
        opts = store.options.pdf
        opts.title = store.title
        data = build_pdf_bytes(pages, opts)

        store.pdf.stage = "checking the file"
        store.pdf.fraction = 0.92
        ok, count, note = _verify_pdf(data, len(pages), store.title)
        if not ok:
            raise SourceProblem(f"The PDF did not come out right ({note}).", "pdf")

        store.pdf.status = "ready"
        store.pdf.data = data
        store.pdf.page_count = count
        store.pdf.byte_size = len(data)
        store.pdf.filename = to_pdf_filename(store.title or "notes")
        store.pdf.verified = True
        store.pdf.verify_note = note
        store.pdf.created = time.time()
        store.pdf.stage = "ready"
        store.pdf.fraction = 1.0
    except SourceProblem as problem:
        store.pdf.status = "failed"
        store.pdf.error = problem.message
        store.pdf.stage = "failed"
    except Exception as exc:                              # noqa: BLE001
        store.pdf.status = "failed"
        store.pdf.error = f"{type(exc).__name__}: {exc}"
        store.pdf.stage = "failed"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/sources")
def sources() -> JSONResponse:
    """What can and cannot be imported, stated plainly (shown in the UI)."""
    return JSONResponse({
        "file": {
            "works": True,
            "note": "Any video saved on this device. Nothing is uploaded anywhere.",
            "formats": ["mp4", "mov", "mkv", "webm", "m4v", "avi"],
        },
        "link": {
            "works_when": [
                "the link points straight at a video file (.mp4, .webm, .mov...)",
                "or the site serves it publicly without a login",
            ],
            "often_needs_login": ["Instagram", "Facebook", "many YouTube videos"],
            "never": ["DRM-protected video (Netflix, Prime, etc.)",
                      "private or paid content",
                      "live streams"],
            "note": "When a site asks for a login, this server will not pretend to be "
                    "you. It says so and you can upload the file instead.",
            "cookies_env": "PAGESNAP_COOKIES=/path/cookies.txt uses cookies you exported "
                           "yourself, for videos you can already watch.",
        },
    })


@app.post("/api/upload")
async def upload(
    video: UploadFile = File(...),
    sensitivity: str = Form("balanced"),
    image_format: str = Form("jpeg"),
    quality: int = Form(92),
    title: str = Form(""),
    max_width: int = Form(2200),
    grayscale: bool = Form(False),
) -> JSONResponse:
    display = video.filename or "upload"
    suffix = os.path.splitext(display)[1].lower() or ".mp4"
    directory = os.path.join(WORK, uuid.uuid4().hex[:12])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "source" + suffix)
    with open(path, "wb") as fh:
        shutil.copyfileobj(video.file, fh)
    if not title:
        title = os.path.splitext(os.path.basename(display))[0][:80]

    store = _new_job(_preset_options(sensitivity, image_format, quality, title,
                                     max_width, grayscale), title, display)
    store.source_path = path

    # Refuse a file we cannot decode *before* claiming a job is running, instead
    # of reporting success and showing an empty review screen.
    problem = _verify_video_file(path)
    if problem:
        _fail(store, problem)
        return JSONResponse({"id": store.id, "status": store.status,
                             "error": store.error, "kind": problem.kind,
                             "advice": problem.advice},
                            status_code=400)

    store.status = "analysing"
    threading.Thread(target=_analyse, args=(store,), daemon=True).start()
    return JSONResponse({"id": store.id, "status": store.status, "title": store.title})


@app.post("/api/fetch")
def fetch_link(url: str = Form(""), sensitivity: str = Form("balanced"),
               image_format: str = Form("jpeg"), quality: int = Form(92),
               title: str = Form(""), max_width: int = Form(2200),
               grayscale: bool = Form(False)) -> JSONResponse:
    """Import a link. Validates first, then downloads on a worker thread."""
    try:
        url = _check_url(url)
    except SourceProblem as problem:
        return JSONResponse({"error": problem.message, "kind": problem.kind,
                             "advice": problem.advice}, status_code=400)

    store = _new_job(_preset_options(sensitivity, image_format, quality, title,
                                     max_width, grayscale), title, url)
    store.status = "downloading"
    store.progress = Progress("checking the link", 0.02)
    threading.Thread(target=_import_link, args=(store, url), daemon=True).start()
    return JSONResponse({"id": store.id, "status": store.status})


@app.get("/api/jobs/{jid}")
def job_status(jid: str) -> JSONResponse:
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    return JSONResponse(store.public())


@app.get("/api/jobs/{jid}/thumb/{uid}")
def thumb(jid: str, uid: str) -> Response:
    store = JOBS.get(jid)
    entry = store.by_uid(uid) if store else None
    if not entry:
        raise HTTPException(404, "no such page")
    return Response(entry.thumb, media_type="image/jpeg")


@app.get("/api/jobs/{jid}/page/{uid}")
def full_page(jid: str, uid: str) -> Response:
    """The full-resolution screenshot, for looking at one page closely."""
    store = JOBS.get(jid)
    entry = store.by_uid(uid) if store else None
    if not entry:
        raise HTTPException(404, "no such page")
    return Response(entry.image_bytes, media_type="image/png")


@app.get("/api/jobs/{jid}/sheet")
def sheet(jid: str) -> Response:
    """A contact sheet of the current selection - 'show me what will be exported'."""
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    entries = store.selected()
    if not entries:
        raise HTTPException(400, "No pages are selected.")
    try:
        png = contact_sheet([
            CapturedPage(image_bytes=e.image_bytes, width=e.width, height=e.height,
                         source_time=e.time, reason=e.reason, label=e.label)
            for e in entries
        ])
    except Exception as exc:                                 # noqa: BLE001
        raise HTTPException(500, f"Could not draw the preview: {exc}")
    return Response(png, media_type="image/png")


@app.post("/api/jobs/{jid}/pages")
def edit_pages(jid: str, body: dict) -> JSONResponse:
    """Apply the user's edits: order, removals, and the note title.

    Everything is re-validated against the pages that actually exist, so a stale
    browser tab cannot corrupt a job.
    """
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")

    known = {e.uid for e in store.pages}
    order = body.get("order")
    if order is not None:
        if not isinstance(order, list):
            raise HTTPException(422, "'order' must be a list of page ids")
        cleaned = [str(u) for u in order if str(u) in known]
        missing = known - set(cleaned)
        store.order = cleaned + [u for u in store.order if u in missing]
    removed = body.get("removed")
    if removed is not None:
        if not isinstance(removed, list):
            raise HTTPException(422, "'removed' must be a list of page ids")
        store.removed = {str(u) for u in removed if str(u) in known}
    if isinstance(body.get("title"), str):
        store.title = body["title"][:120]
    store.options.pdf.title = store.title
    store.touch()
    return JSONResponse(store.public())


@app.post("/api/jobs/{jid}/capture")
def capture(jid: str, body: dict) -> JSONResponse:
    """Grab one more page from the original video at a time the user chooses."""
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    if store.status != "ready" or not store.source_path:
        raise HTTPException(409, "There is no video to capture from yet.")
    if not os.path.exists(store.source_path):
        raise HTTPException(409, "The video file is no longer on disk.")

    try:
        seconds = float(body.get("time"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Give the time in seconds, for example 42.5")

    duration = store.job.duration if store.job else 0.0
    if seconds < 0:
        raise HTTPException(422, "The time cannot be negative.")
    if duration and seconds > duration + 0.5:
        raise HTTPException(422, f"That moment is past the end of the video "
                                 f"({duration:.1f}s).")
    seconds = min(seconds, max(0.0, duration - 0.05)) if duration else seconds

    raw = grab_frame(store.source_path, seconds, "png")
    if raw is None:
        raise HTTPException(422, f"Could not read a frame at {seconds:.2f}s.")
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.width, img.height

    entry = _entry_from_capture(len(store.pages), CapturedPage(
        image_bytes=raw, width=width, height=height, source_time=seconds,
        reason="manual", label=f"captured at {seconds:.1f}s"))
    entry.manual = True
    store.pages.append(entry)
    store.order.append(entry.uid)
    store.touch()
    return JSONResponse({"job": store.public(), "uid": entry.uid})


@app.post("/api/jobs/{jid}/reanalyse")
def reanalyse(jid: str, body: dict) -> JSONResponse:
    """Run the detector again on the same video, with a different sensitivity.

    This is the way out of "no pages were found": change how eager the detector
    is and try again, without uploading or downloading the video a second time.
    """
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    if not store.source_path or not os.path.exists(store.source_path):
        raise HTTPException(409, "The video is no longer on disk.")

    sensitivity = str((body or {}).get("sensitivity", "")).lower()
    if sensitivity not in ("strict", "balanced", "eager"):
        raise HTTPException(422, "sensitivity must be strict, balanced or eager")

    store.options.sensitivity = sensitivity                       # type: ignore[assignment]
    store.options.detect = DetectConfig.preset(sensitivity)       # type: ignore[arg-type]
    store.pages, store.order, store.removed = [], [], set()
    store.job = None
    store.pdf = PdfState()
    store.error = store.error_kind = store.error_advice = ""
    store.status = "analysing"
    store.progress = Progress("queued", 0.0)
    store.touch()
    threading.Thread(target=_analyse, args=(store,), daemon=True).start()
    return JSONResponse(store.public())


@app.post("/api/jobs/{jid}/pdf")
def generate_pdf(jid: str, body: Optional[dict] = None) -> JSONResponse:
    """Generate the PDF from the current state, and wait for it (it is quick).

    Waiting means the UI can report a real result - page count, file size, and
    whether the file was verified - instead of a hopeful 'download started'.
    """
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    if store.status != "ready":
        raise HTTPException(409, "The video is still being analysed.")
    if body and isinstance(body.get("title"), str):
        store.title = body["title"][:120]
        store.options.pdf.title = store.title
        store.touch()

    _generate_pdf(store)
    state = store.public()
    if store.pdf.status != "ready":
        return JSONResponse(state, status_code=400)
    return JSONResponse(state)


@app.get("/api/jobs/{jid}/pdf")
def fetch_pdf(jid: str, mode: str = "download") -> Response:
    """Hand back the generated PDF - but never a stale one.

    A PDF is only served when the revision it was built from still matches the
    job. After any edit the client is told to regenerate, so the file a student
    downloads always matches the pages they can see.
    """
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    if store.pdf.status == "generating":
        raise HTTPException(409, "The PDF is still being written.")
    if store.pdf.status != "ready" or not store.pdf.data:
        raise HTTPException(409, store.pdf.error or "No PDF has been generated yet.")
    if store.pdf.revision != store.revision:
        raise HTTPException(409, "The page list changed after this PDF was generated - "
                                 "generate it again so the file matches what you see.")
    disposition = "inline" if mode == "preview" else "attachment"
    return Response(
        store.pdf.data, media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{store.pdf.filename}"',
                 "X-Page-Count": str(store.pdf.page_count),
                 "X-Revision": str(store.pdf.revision)},
    )


@app.delete("/api/jobs/{jid}")
def drop(jid: str) -> JSONResponse:
    store = JOBS.pop(jid, None)
    if store:
        shutil.rmtree(store.directory, ignore_errors=True)
    return JSONResponse({"ok": True})


def main() -> None:
    ap = argparse.ArgumentParser(description="PageSnap prototype server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
