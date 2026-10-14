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

    python prototype/server.py                # http://localhost:4567
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
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

# --------------------------------------------------------------------------- #
# Before importing anything third-party: check this machine can actually run
# PageSnap, and say exactly what to do when it cannot. A raw ModuleNotFoundError
# traceback is the most common reason a first run fails, and it tells the person
# reading it nothing useful.
# --------------------------------------------------------------------------- #

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("multipart", "python-multipart"),
    ("PIL", "Pillow"),
    ("numpy", "numpy"),
    ("reportlab", "reportlab"),
    ("imageio_ffmpeg", "imageio-ffmpeg"),
]
#: Missing these does not stop PageSnap; it degrades politely instead.
OPTIONAL_PACKAGES = [("yt_dlp", "yt-dlp", "importing links"),
                     ("pypdf", "pypdf", "re-reading the PDF to check it")]


def _missing(packages):
    missing = []
    for entry in packages:
        try:
            found = importlib.util.find_spec(entry[0])
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append(entry)
    return missing


def _require_ready() -> None:
    """Stop with instructions if PageSnap cannot start, and warn if it is partial."""
    if sys.version_info < (3, 9):
        print("PageSnap needs Python 3.9 or newer.\n"
              f"This is Python {sys.version.split()[0]} from {sys.executable}\n\n"
              "Install a newer Python from https://www.python.org/downloads/ and run\n"
              "PageSnap with that one.\n")
        raise SystemExit(1)

    missing = _missing(REQUIRED_PACKAGES)
    if missing:
        names = ", ".join(package for _, package in missing)
        print("\nPageSnap cannot start yet: some Python packages are missing.\n")
        print(f"  missing: {names}\n")
        print("Install them with this exact command (copy the whole line):\n")
        print(f'    "{sys.executable}" -m pip install -r requirements.txt\n')
        print("Run it from the folder that contains requirements.txt (the PageSnap "
              "folder).\nThen start PageSnap again.\n")
        raise SystemExit(1)

    gentle = _missing(OPTIONAL_PACKAGES)
    if gentle:
        # Not fatal: links still import if yt-dlp is there, and the PDF is still
        # written without pypdf - it is only re-read for verification.
        print("Note: these optional packages are missing, so part of PageSnap is limited:")
        for _, package, what in gentle:
            print(f"  - {package} (used for {what})")
        print(f'  To add them:  "{sys.executable}" -m pip install -r requirements.txt\n')


_require_ready()

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
#: Where the PDFs you make are kept, so the Library screen can show the real files
#: after the job itself is gone. A folder on this computer - not a cloud service, not
#: a database, and nothing leaves the machine. Override with PAGESNAP_LIBRARY.
LIBRARY = os.environ.get("PAGESNAP_LIBRARY") or os.path.join(
    os.path.expanduser("~"), "PageSnap")
LIBRARY_KEEP = 30                    # newest first; older ones are pruned

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
    (r"unavailable|removed|deleted|404|not found|gone|410|no longer exists|does not exist",
     "unavailable",
     "That video is not there any more, or the link is wrong.",
     ""),
    # An expired or refused link is the classic "lecture recording" failure: the URL
    # was only valid for a while, or the site wants a token we do not have. Telling
    # the student to check their internet was actively misleading.
    (r"http error 403|http error 401|forbidden|unauthori[sz]ed|access denied|"
     r"\bexpired\b|link has expired|signature|token|session",
     "expired_link",
     "That link is no longer valid, or the site refused to serve the video to anyone "
     "who is not signed in.",
     "Time-limited links - lecture recordings, lecture-capture systems, shared files - "
     "stop working after a while, and this server will not sign in as you. Open the page "
     "while it still works and save the video to this device, then choose the file here."),
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


#: What a link turned out to be, decided before anything is fetched. Defined up here
#: because a job carries one from the moment it is created.
@dataclass
class SourceInfo:
    kind: str = "other"          # see classify_source
    video_id: str = ""
    canonical: str = ""          # what will actually be fetched, if we fetch at all
    label: str = ""              # human words for the screen
    plan: str = ""               # what we are going to do about it


@dataclass
class PdfState:
    status: str = "none"          # none | generating | ready | failed
    stage: str = ""
    fraction: float = 0.0
    revision: int = -1
    page_count: int = 0
    byte_size: int = 0
    filename: str = ""                          # what the user should see, in their script
    filename_ascii: str = ""                    # header-safe fallback
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
    source: SourceInfo = field(default_factory=SourceInfo)   # classified once, up front
    library_id: str = ""                     # set once the PDF is on the library shelf
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
            # `source_kind` stays what the screen already keys off: how the video got
            # here (a link or a local file). The *classification* - a YouTube Short,
            # an embedded player, a direct file link - is additive, in source_class.
            "source_kind": "link" if self.display_source.startswith(("http://", "https://"))
            else "file",
            "source_class": self.source.kind,
            "source_label": self.source.label,
            "source_video_id": self.source.video_id,
            "source_plan": self.source.plan,
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
            "library_id": self.library_id,
            "pdf": {
                "status": self.pdf.status,
                "stage": self.pdf.stage,
                "fraction": round(self.pdf.fraction, 3),
                "page_count": self.pdf.page_count,
                "byte_size": self.pdf.byte_size,
                "filename": self.pdf.filename,
                "filename_ascii": self.pdf.filename_ascii,
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


def _explain_for_source(store: SnapJob, problem: SourceProblem) -> SourceProblem:
    """Say what actually went wrong when a platform video cannot be fetched.

    A YouTube link failing is not the same as a broken link: the video is fine, it
    plays in any browser, and the machine doing the fetching is the thing that
    cannot reach it. That distinction is the whole difference between "check your
    internet" (useless) and "playable, but this server cannot fetch it - upload the
    file instead" (actionable).
    """
    kind = store.source.kind or ""
    if not kind.startswith("youtube") or kind == "youtube_other":
        return problem

    what = store.source.label or "that video"
    if problem.kind == "network":
        return SourceProblem(
            f"YouTube could not be reached from this machine, so {what} could not be "
            "fetched.",
            "cannot_fetch",
            "The video itself is fine - it plays in your browser, and nothing about the "
            "link is wrong. This server simply cannot get to YouTube from here. Save the "
            "video where it does play, or use a downloader you are entitled to use, then "
            "choose the file here.")
    if problem.kind == "needs_login":
        return SourceProblem(
            "YouTube asked this machine to prove it is a signed-in person, and this "
            "server will not sign in as you.",
            "needs_login",
            "The video is playable while you are signed in. Save it to this device from "
            "there, then choose the file here - or set PAGESNAP_COOKIES to cookies you "
            "exported yourself.")
    if problem.kind in ("unsupported", "not_a_video", "expired_link"):
        return SourceProblem(
            f"YouTube would not hand {what} over to this machine.",
            problem.kind,
            (problem.advice + " " if problem.advice else "")
            + "The video may still play perfectly in your browser. Save it from there and "
              "choose the file here.")
    return problem


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
    if not parts.scheme:
        # "not a url at all" has no scheme, and 'that' links are not unsupported -
        # it is simply not an address. Say which of the two it is.
        raise SourceProblem(
            "That is not a web address.", "bad_url",
            "A link looks like https://example.com/video - copy it from the address bar "
            "or the app's Share menu.")
    if parts.scheme not in ("http", "https"):
        raise SourceProblem(
            f"'{parts.scheme}' links are not supported - only http and https.",
            "bad_url",
            "Copy the link from the address bar or the app's Share menu.")
    if not parts.netloc or "." not in parts.netloc:
        raise SourceProblem(f"'{url}' is not a valid web address.", "bad_url",
                            "A link looks like https://example.com/video.")

    _assert_allowed(url)
    return url


def _assert_allowed(url: str, redirecting: bool = False) -> None:
    """The public-address rule, applied to any URL we are about to open.

    Called again for every redirect hop: a public link must not be able to bounce
    us onto a private one. `169.254.x.x` is the cloud metadata service.
    """
    where = "redirected to" if redirecting else "is"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SourceProblem(
            f"That link {where} a '{parts.scheme or 'relative'}' address - only http and "
            "https can be fetched.", "bad_url",
            "Copy the link from the address bar or the app's Share menu.")
    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise SourceProblem(f"Could not find the site '{host}'.", "network",
                            "Check the spelling, or the connection.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_link_local:
            raise SourceProblem(
                f"That link {where} an address this server is not allowed to open.",
                "bad_url", "Link-local and metadata addresses are refused on purpose.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if the place it points to is allowed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):    # noqa: N802
        _assert_allowed(newurl, redirecting=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: YouTube video ids are always 11 characters of this alphabet.
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
            "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be"}


def _canonical_yt(video_id: str) -> str:
    """The plain watch URL for a video id.

    A Short, an embed and a watch link are three addresses for the same video.
    Normalising to one address means one code path, and it is the form the
    extractor is asked about rather than an embedded player page.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


_YT_REFUSE = {
    "youtube_playlist": (
        "That link is a playlist, not a video.",
        "A playlist would be every video in it, one after another. Open the one video "
        "you want and copy its own link."),
    "youtube_channel": (
        "That link is a channel, not a video.",
        "Open the video you want and copy its link from the address bar or the Share menu."),
    "youtube_search": (
        "That link is a search results page, not a video.",
        "Open the video you want and copy its link from the address bar."),
}


def classify_source(url: str) -> SourceInfo:
    """Work out what a link is: a file, a YouTube Short, another platform, or a page.

    A YouTube Short is treated as its own kind because it is not a file and not a
    generic web page: it is a video on a platform, so its id is extracted, the link
    is normalised to the canonical watch URL, and only the official extractor
    (yt-dlp) ever touches it. Nothing here signs in as the user, and an embedded
    player is never treated as a downloadable file - at most its id is read out of
    the URL, which is the same public video by another address.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    query = urllib.parse.parse_qs(parts.query)

    if host == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
        if YT_ID.match(candidate):
            return SourceInfo("youtube_video", candidate, _canonical_yt(candidate),
                              "a YouTube video", "fetched with yt-dlp")
        return SourceInfo("youtube_other", label="a youtu.be link")

    if host in YT_HOSTS or host.endswith(".youtube.com"):
        head, _, tail = path.lstrip("/").partition("/")
        tail = tail.split("/")[0]
        watched = (query.get("v") or [""])[0]

        if head == "shorts":
            if YT_ID.match(tail):
                return SourceInfo(
                    "youtube_short", tail, _canonical_yt(tail), "a YouTube Short",
                    "fetched with yt-dlp, the same way any YouTube video is")
            return SourceInfo("youtube_other", label="a YouTube Shorts link",
                              plan="the video id could not be read from it")
        if head in ("embed", "v", "e"):
            # An embedded player is a page, not a file. Reading the id out of it and
            # asking the extractor about that video is a different thing entirely.
            if YT_ID.match(tail):
                return SourceInfo("youtube_embed", tail, _canonical_yt(tail),
                                  "an embedded YouTube player",
                                  "the player page is not a file, so its video is fetched "
                                  "by id instead")
            return SourceInfo("youtube_other", label="an embedded player")
        if head == "live":
            if YT_ID.match(tail):
                return SourceInfo("youtube_live", tail, _canonical_yt(tail),
                                  "a YouTube live stream",
                                  "tried as a normal video in case the stream has ended")
        if head == "playlist" or ("list" in query and not watched):
            return SourceInfo("youtube_playlist", label="a YouTube playlist")
        if head == "results":
            return SourceInfo("youtube_search", label="a YouTube search page")
        if head.startswith("@") or head in ("channel", "c", "user"):
            return SourceInfo("youtube_channel", label="a YouTube channel")
        if watched and YT_ID.match(watched):
            if "list" in query:
                return SourceInfo("youtube_video_with_playlist", watched,
                                  _canonical_yt(watched), "a YouTube video",
                                  "fetched on its own, without the playlist around it")
            return SourceInfo("youtube_video", watched, _canonical_yt(watched),
                              "a YouTube video", "fetched with yt-dlp")
        return SourceInfo("youtube_other", label="a YouTube page")

    if _looks_like_media(url):
        return SourceInfo("direct_file", canonical=url, label="a video file link")
    return SourceInfo("web_link", canonical=url, label="a link")


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


def _probe_content_type(url: str, timeout: float = 8.0) -> str:
    """What would this URL send us? Answered with headers only, never a download.

    Used when a link is not obviously a media file, to tell three things apart: a
    direct video file, a web page, and something else entirely. Servers that refuse
    HEAD are asked again for a single byte. Anything unexpected counts as unknown,
    and the caller falls back to whatever the downloader reported.
    """
    opener = urllib.request.build_opener(_CheckedRedirects)
    for attempt, headers in enumerate(({"User-Agent": "PageSnap/0.1"},
                                       {"User-Agent": "PageSnap/0.1",
                                        "Range": "bytes=0-0"})):
        try:
            request = urllib.request.Request(
                url, headers=headers, method="HEAD" if attempt == 0 else "GET")
            with opener.open(request, timeout=timeout) as response:
                return (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in (405, 501):     # HEAD not allowed here
                continue
            if exc.code in (401, 403, 404, 410):
                raise explain_ytdlp(exc)                    # a real reason, e.g. expired
            return ""
        except SourceProblem:
            raise                                           # already explained
        except Exception:                                   # noqa: BLE001
            return ""
    return ""


def _download_direct(url: str, target_dir: str, store: SnapJob) -> str:
    """Plain HTTP download, for links that are already a video file."""
    request = urllib.request.Request(url, headers={"User-Agent": "PageSnap/0.1"})
    opener = urllib.request.build_opener(_CheckedRedirects)
    path = os.path.join(target_dir, "source" + (os.path.splitext(
        urllib.parse.urlsplit(url).path)[1] or ".mp4"))
    with opener.open(request, timeout=30) as response:
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
            # A link yt-dlp does not know. Before deciding anything, ask the server
            # what it actually serves: a web page is not a video, and a direct file
            # link without a .mp4 in the path is still a video.
            served = "" if _looks_like_media(url) else _probe_content_type(url)
            if _looks_like_media(url) or served.startswith(("video/", "audio/")) \
                    or served == "application/octet-stream":
                # A plain file link: fetch it ourselves.
                store.progress = Progress("downloading the file", 0.1)
                path = _download_direct(url, store.directory, store)
                store.source_path = path
                info = None
            elif served.startswith("text/") or served.endswith(("html", "xml")):
                raise SourceProblem(
                    "That link is a web page, not a video file.", "web_page",
                    "It is the page the video sits on, and this server will not take a "
                    "page apart to guess which video you meant. Open the page, save the "
                    "video to this device, then choose the file here - that always works.")
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
        _fail(store, _explain_for_source(store, problem))
    except Exception as exc:                          # noqa: BLE001
        _fail(store, _explain_for_source(store, explain_ytdlp(exc)))


# --------------------------------------------------------------------------- #
# PDF: generate, verify, and refuse to hand back a stale one
# --------------------------------------------------------------------------- #

#: Characters that cannot appear in a filename on Windows, macOS or Android.
_BAD_NAME_CHARS = set('\\/:*?"<>|')


def download_names(title: str) -> Tuple[str, str]:
    """(the name to save as, an ASCII fallback) - both ending in .pdf.

    Titles are free text, and a student here writes them in Punjabi, Hindi and
    English. Two separate problems meet in this function:

    * A filename must not contain path separators or quotes, but it should keep the
      letters the user typed. The engine's `safe_filename` keeps only `isalnum()`
      characters, and Devanagari vowel signs are not alphanumeric - so it silently
      mangles the word: "अध्याय" comes out as "अधयय".
    * An HTTP header is latin-1. Putting a Devanagari or CJK name straight into
      `Content-Disposition` raised UnicodeEncodeError, which meant generating a PDF
      worked and *downloading* it answered 500. That was the download bug. The name
      now also travels as `filename*=UTF-8''...` (RFC 5987), which browsers prefer
      and anything else can decode, and the plain `filename=` is pure ASCII.
    """
    cleaned = "".join(ch for ch in (title or "")
                      if ch.isprintable() and ch not in _BAD_NAME_CHARS)
    cleaned = "-".join(cleaned.split()).strip(" .-_")[:80].strip(" .-_")
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4].strip(" .-_")
    if not cleaned:
        cleaned = "notes"

    ascii_stem = "-".join("".join(ch for ch in cleaned if ord(ch) < 128).split())
    ascii_stem = ascii_stem.strip(" .-_")[:80].strip(" .-_") or "notes"
    return cleaned + ".pdf", ascii_stem + ".pdf"


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
        store.pdf.filename, store.pdf.filename_ascii = download_names(store.title or "notes")
        store.pdf.verified = True
        store.pdf.verify_note = note
        store.pdf.created = time.time()
        store.pdf.stage = "ready"
        store.pdf.fraction = 1.0
        _library_save(store)
    except SourceProblem as problem:
        store.pdf.status = "failed"
        store.pdf.error = problem.message
        store.pdf.stage = "failed"
    except Exception as exc:                              # noqa: BLE001
        store.pdf.status = "failed"
        store.pdf.error = f"{type(exc).__name__}: {exc}"
        store.pdf.stage = "failed"


# --------------------------------------------------------------------------- #
# the library - the PDFs you actually made, saved as files on this computer
# --------------------------------------------------------------------------- #

def _library_index_path() -> str:
    return os.path.join(LIBRARY, "index.json")


def _library_read() -> Dict[str, dict]:
    try:
        with open(_library_index_path(), "r", encoding="utf-8") as fh:
            entries = json.load(fh)
        return {str(e["id"]): e for e in entries if isinstance(e, dict) and e.get("id")}
    except (OSError, ValueError):
        return {}


def _library_write(entries: Dict[str, dict]) -> None:
    os.makedirs(LIBRARY, exist_ok=True)
    ordered = sorted(entries.values(), key=lambda e: e.get("updated", 0), reverse=True)
    # Keep the shelf tidy: the newest survive, the rest are removed with their files.
    for stale in ordered[LIBRARY_KEEP:]:
        try:
            os.remove(os.path.join(LIBRARY, os.path.basename(stale.get("file", ""))))
        except OSError:
            pass
    ordered = ordered[:LIBRARY_KEEP]
    tmp = _library_index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=1)
    os.replace(tmp, _library_index_path())


def _library_save(store: SnapJob) -> None:
    """Put the PDF that was just written on the shelf, replacing any earlier copy.

    Keyed by job, because regenerating after an edit is the *same* document with new
    contents - not a second file with the same name.
    """
    if store.pdf.status != "ready" or not store.pdf.data:
        return
    try:
        os.makedirs(LIBRARY, exist_ok=True)
        filename = f"{store.id}.pdf"
        with open(os.path.join(LIBRARY, filename), "wb") as fh:
            fh.write(store.pdf.data)
        entries = _library_read()
        entry = {
            "id": store.id,
            "title": store.title or "notes",
            "filename": store.pdf.filename or "notes.pdf",
            "filename_ascii": store.pdf.filename_ascii or "notes.pdf",
            "file": filename,
            "page_count": store.pdf.page_count,
            "byte_size": store.pdf.byte_size,
            "created": entries.get(store.id, {}).get("created", time.time()),
            "updated": time.time(),
            "source": store.display_source,
            "source_label": store.source.label or ("your own file"
                                                  if store.source.kind in ("other", "")
                                                  else store.source.kind),
            "duration": round(store.job.duration, 1) if store.job else 0,
        }
        entries[store.id] = entry
        _library_write(entries)
        store.library_id = store.id
    except OSError as exc:
        # A library that cannot be written must not take the PDF down with it.
        store.notes.append(f"Could not save a copy to the library: {exc}")


def _library_entry(entry_id: str) -> Optional[dict]:
    safe = os.path.basename(entry_id)
    entry = _library_read().get(safe)
    if not entry:
        return None
    path = os.path.join(LIBRARY, os.path.basename(entry.get("file", "")))
    return entry if os.path.exists(path) else None


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
            "often_needs_login": ["Instagram", "Facebook",
                                  "YouTube videos that trigger its bot check"],
            "youtube": {
                "shorts": "supported: a /shorts/ link is recognised, its video id read, "
                          "and the video fetched by id with yt-dlp",
                "watch_and_short_links": "supported the same way, including youtu.be",
                "embed_links": "an embedded player is a page, not a file - its video id is "
                               "used instead of the player",
                "refused_up_front": ["playlists", "channels", "search pages"],
                "needs": "this machine has to be able to reach YouTube. If it cannot, the "
                         "link still plays in your browser and the file can be uploaded.",
            },
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

    info = classify_source(url)
    if info.kind in _YT_REFUSE:
        message, advice = _YT_REFUSE[info.kind]
        return JSONResponse({"error": message, "kind": "wrong_link", "advice": advice},
                            status_code=400)

    store = _new_job(_preset_options(sensitivity, image_format, quality, title,
                                     max_width, grayscale), title, url)
    store.source = info
    store.status = "downloading"
    store.progress = Progress("checking the link", 0.02)
    # The job fetches `canonical` (for a Short, the same video at its watch address)
    # while the screen keeps showing the link the user pasted.
    threading.Thread(target=_import_link, args=(store, info.canonical or url),
                     daemon=True).start()
    return JSONResponse({
        "id": store.id, "status": store.status,
        "source": {"kind": info.kind, "transport": "link", "label": info.label,
                   "video_id": info.video_id, "plan": info.plan}})


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
    # `filename` is ASCII so the header can always be encoded; `filename*` carries
    # the name the user actually chose. Both are needed: the first for old clients,
    # the second for every current browser.
    ascii_name = store.pdf.filename_ascii or "notes.pdf"
    quoted = urllib.parse.quote(store.pdf.filename or "notes.pdf", safe="")
    return Response(
        store.pdf.data, media_type="application/pdf",
        headers={"Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; "
                                        f"filename*=UTF-8''{quoted}",
                 "X-Page-Count": str(store.pdf.page_count),
                 "X-Revision": str(store.pdf.revision)},
    )


@app.get("/api/library")
def library_list() -> JSONResponse:
    """The PDFs on this computer, newest first. Real files, checked as they are listed."""
    entries = []
    for entry in sorted(_library_read().values(), key=lambda e: e.get("updated", 0),
                        reverse=True):
        path = os.path.join(LIBRARY, os.path.basename(entry.get("file", "")))
        if not os.path.exists(path):
            continue                                   # never show a file that is not there
        entry = dict(entry)
        entry["exists"] = True
        entry["download"] = f"/api/library/{entry['id']}/file"
        entries.append(entry)
    return JSONResponse({"folder": LIBRARY, "count": len(entries), "items": entries})


@app.get("/api/library/{entry_id}/file")
def library_file(entry_id: str, mode: str = "download") -> Response:
    """Hand back a saved PDF. Still never a stale one: the stored copy is what was made."""
    entry = _library_entry(entry_id)
    if not entry:
        raise HTTPException(404, "That file is not in the library any more.")
    path = os.path.join(LIBRARY, os.path.basename(entry["file"]))
    with open(path, "rb") as fh:
        data = fh.read()
    disposition = "inline" if mode == "preview" else "attachment"
    ascii_name = entry.get("filename_ascii") or "notes.pdf"
    quoted = urllib.parse.quote(entry.get("filename") or "notes.pdf", safe="")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"{disposition}; "
                                                    f"filename=\"{ascii_name}\"; "
                                                    f"filename*=UTF-8''{quoted}"})


@app.post("/api/library/{entry_id}")
def library_rename(entry_id: str, body: dict) -> JSONResponse:
    """Rename a saved PDF. The file on disk is renamed with it, not just the label."""
    entry = _library_entry(entry_id)
    if not entry:
        raise HTTPException(404, "That file is not in the library any more.")
    title = str((body or {}).get("title", "")).strip()[:120]
    if not title:
        raise HTTPException(422, "Give the PDF a name first.")
    name, ascii_name = download_names(title)
    entries = _library_read()
    entries[entry["id"]].update({"title": title, "filename": name,
                                 "filename_ascii": ascii_name, "updated": time.time()})
    _library_write(entries)
    return JSONResponse(entries[entry["id"]])


@app.delete("/api/library/{entry_id}")
def library_delete(entry_id: str) -> JSONResponse:
    """Take a PDF off the shelf, and delete the file itself."""
    entry = _library_entry(entry_id)
    if not entry:
        raise HTTPException(404, "That file is not in the library any more.")
    try:
        os.remove(os.path.join(LIBRARY, os.path.basename(entry["file"])))
    except OSError:
        pass
    entries = _library_read()
    entries.pop(entry["id"], None)
    _library_write(entries)
    return JSONResponse({"ok": True})


@app.delete("/api/jobs/{jid}")
def drop(jid: str) -> JSONResponse:
    store = JOBS.pop(jid, None)
    if store:
        shutil.rmtree(store.directory, ignore_errors=True)
    return JSONResponse({"ok": True})


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _already_running(port: int) -> bool:
    """Is the thing on this port PageSnap itself? (Then there is nothing to start.)"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/sources", timeout=2) as response:
            payload = json.loads(response.read().decode())
        return isinstance(payload, dict) and "file" in payload and "link" in payload
    except Exception:                                        # noqa: BLE001
        return False


def _choose_port(wanted: int) -> Tuple[int, str]:
    """(the port to use, a note to print). Never guesses in silence."""
    if _port_free(wanted):
        return wanted, ""
    if _already_running(wanted):
        print(f"\nPageSnap is ALREADY running on http://localhost:{wanted}\n"
              "Just open that address in your browser — there is nothing to start.\n"
              f"(For a second copy at the same time, use: --port {wanted + 1})\n")
        raise SystemExit(0)
    for candidate in range(wanted + 1, wanted + 11):
        if _port_free(candidate):
            return candidate, (f"Port {wanted} is being used by something else on this "
                               f"computer, so PageSnap started on {candidate} instead.")
    print(f"\nPorts {wanted} to {wanted + 10} are all busy. Start PageSnap on a free one:\n\n"
          f"    python prototype/server.py --port 4580\n")
    raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="PageSnap prototype server")
    ap.add_argument("--port", type=int, default=4567,
                    help="port to serve on (default 4567; the next free one is used if "
                         "this is taken)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to bind (default 0.0.0.0, so a phone on the same "
                         "network can reach it; use 127.0.0.1 for this computer only)")
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    import uvicorn

    port, note = _choose_port(args.port)
    if note:
        print("\n" + note, flush=True)
    print(f"\nPageSnap is running - open http://localhost:{port} in your browser\n"
          f"Press Ctrl+C here to stop it.\n", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\nStopped.\n")
    except OSError as exc:
        print(f"\nCould not use port {port}: {exc}\n"
              f"Try another one:  python prototype/server.py --port {port + 1}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
