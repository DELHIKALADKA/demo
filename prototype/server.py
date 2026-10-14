"""PageSnap prototype server.

This is the *computer* version of the app - a thin UI over the same engine that
ships in the Android app. It exists so the flow can be used and judged right now:

    upload a video (or paste a link)  ->  it finds the pages  ->  tick/untick and
    name the notes  ->  download the PDF

Two honest notes about this prototype, because they matter for the phone app:

* Paste-a-link only works here because this program runs on a real computer and
  can use yt-dlp. A *browser* can never do that step, which is exactly why the
  phone app has to be a real app with yt-dlp inside it, not a web page.
* Jobs live in memory for the session; nothing is uploaded anywhere and nothing
  is stored after the server stops. The Android app stores notes in the phone's
  own storage, which is the arrangement you asked for.

    python prototype/server.py --port 8000
"""

from __future__ import annotations

import argparse
import io
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile    # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: E402
from PIL import Image                                                 # noqa: E402

from pagesnap.config import DetectConfig                              # noqa: E402
from pagesnap.pdf import CapturedPage, PdfOptions, build_pdf_bytes, to_pdf_filename  # noqa: E402
from pagesnap.pipeline import Job, JobOptions, capture_pages          # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORK = os.path.join(tempfile.gettempdir(), "pagesnap-jobs")

app = FastAPI(title="PageSnap prototype")


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

@dataclass
class Progress:
    stage: str = "queued"
    fraction: float = 0.0


@dataclass
class SnapJob:
    id: str
    source: str
    directory: str
    options: JobOptions
    title: str = ""
    status: str = "running"              # running | ready | failed
    error: str = ""
    progress: Progress = field(default_factory=Progress)
    job: Optional[Job] = None
    thumbs: List[bytes] = field(default_factory=list)
    keep: List[bool] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def public(self) -> dict:
        pages = []
        if self.job:
            for i, page in enumerate(self.job.pages):
                pages.append({
                    "index": i,
                    "thumbnail": f"/api/jobs/{self.id}/thumb/{i}",
                    "time": round(page.source_time, 2),
                    "label": page.label,
                    "width": page.width,
                    "height": page.height,
                    "keep": self.keep[i] if i < len(self.keep) else True,
                    "reason": page.reason,
                })
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "stage": self.progress.stage,
            "fraction": round(self.progress.fraction, 3),
            "title": self.title,
            "duration": round(self.job.duration, 1) if self.job else 0,
            "analyzed_seconds": round(self.job.analyzed_seconds, 2) if self.job else 0,
            "pages": pages,
            "notes": self.job.notes if self.job else [],
        }


JOBS: Dict[str, SnapJob] = {}


def _analyse(store: SnapJob) -> None:
    try:
        def report(stage: str, fraction: float) -> None:
            store.progress = Progress(stage, fraction)

        started = time.time()
        job = capture_pages(store.source, store.options,
                            lambda s, f: report(*_relabel(s, f)))
        job.analyzed_seconds = time.time() - started
        store.job = job
        store.keep = [True] * len(job.pages)
        for page in job.pages:
            store.thumbs.append(_thumbnail(page))
        store.status = "ready"
        store.progress = Progress("done", 1.0)
    except Exception as exc:                      # noqa: BLE001 - surface to the UI
        store.status = "failed"
        store.error = f"{type(exc).__name__}: {exc}"


def _relabel(stage: str, fraction: float) -> tuple[str, float]:
    """Map the engine's progress onto friendlier words for the screen."""
    if fraction < 0.1:
        return "reading the video", fraction
    if fraction < 0.5:
        return "looking for page changes", fraction
    if fraction < 1.0:
        return "saving the finished pages", fraction
    return "done", 1.0


def _thumbnail(page: CapturedPage, width: int = 240) -> bytes:
    img = Image.open(io.BytesIO(page.image_bytes)).convert("RGB")
    ratio = width / float(img.width)
    img = img.resize((width, max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _new_job(source: str, options: JobOptions, title: str) -> SnapJob:
    jid = uuid.uuid4().hex[:12]
    directory = os.path.join(WORK, jid)
    os.makedirs(directory, exist_ok=True)
    job = SnapJob(id=jid, source=source, directory=directory, options=options, title=title)
    JOBS[jid] = job
    threading.Thread(target=_analyse, args=(job,), daemon=True).start()
    return job


def _preset_options(sensitivity: str, fmt: str, quality: int, title: str,
                    max_width: int, grayscale: bool) -> JobOptions:
    return JobOptions(
        sensitivity=sensitivity,      # type: ignore[arg-type]
        detect=DetectConfig.preset(sensitivity),  # type: ignore[arg-type]
        pdf=PdfOptions(image_format=fmt, quality=quality, title=title,
                       max_width=max_width, grayscale=grayscale, page_numbers=False),
    )


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


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
    suffix = os.path.splitext(video.filename or "clip.mp4")[1] or ".mp4"
    directory = os.path.join(WORK, uuid.uuid4().hex[:12])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "source" + suffix)
    with open(path, "wb") as fh:
        shutil.copyfileobj(video.file, fh)

    store = _new_job(path, _preset_options(sensitivity, image_format, quality, title,
                                           max_width, grayscale), title)
    store.source = video.filename or "upload"
    return JSONResponse({"id": store.id})


@app.post("/api/fetch")
def fetch_link(url: str = Form(...), sensitivity: str = Form("balanced"),
               image_format: str = Form("jpeg"), quality: int = Form(92),
               title: str = Form(""), max_width: int = Form(2200),
               grayscale: bool = Form(False)) -> JSONResponse:
    """Download the video from Instagram / Facebook / YouTube (needs yt-dlp)."""
    try:
        import yt_dlp                      # noqa: F401
    except ImportError:
        raise HTTPException(400, "yt-dlp is not installed here (pip install yt-dlp). "
                                 "The phone app bundles it, so this step works on-device.")

    directory = os.path.join(WORK, uuid.uuid4().hex[:12])
    os.makedirs(directory, exist_ok=True)

    import yt_dlp
    opts = {
        "outtmpl": os.path.join(directory, "source.%(ext)s"),
        "format": "mp4/bestvideo*+bestaudio/best",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:                  # noqa: BLE001
        raise HTTPException(400, f"could not download that link: {exc}")

    files = [os.path.join(directory, f) for f in os.listdir(directory)]
    if not files:
        raise HTTPException(400, "the download produced no file")
    store = _new_job(files[0], _preset_options(sensitivity, image_format, quality,
                                               title, max_width, grayscale), title)
    store.source = url
    return JSONResponse({"id": store.id})


@app.get("/api/jobs/{jid}")
def job_status(jid: str) -> JSONResponse:
    store = JOBS.get(jid)
    if not store:
        raise HTTPException(404, "unknown job")
    return JSONResponse(store.public())


@app.get("/api/jobs/{jid}/thumb/{index}")
def thumb(jid: str, index: int) -> Response:
    store = JOBS.get(jid)
    if not store or index >= len(store.thumbs):
        raise HTTPException(404, "no such page")
    return Response(store.thumbs[index], media_type="image/jpeg")


@app.post("/api/jobs/{jid}/selection")
def selection(jid: str, body: dict) -> JSONResponse:
    store = JOBS.get(jid)
    if not store or not store.job:
        raise HTTPException(404, "unknown job")
    keep = body.get("keep")
    if isinstance(keep, list):
        store.keep = [bool(x) for x in keep][: len(store.job.pages)]
        while len(store.keep) < len(store.job.pages):
            store.keep.append(True)
    if isinstance(body.get("title"), str):
        store.title = body["title"]
    return JSONResponse(store.public())


@app.get("/api/jobs/{jid}/pdf")
def pdf(jid: str, title: str = "") -> Response:
    store = JOBS.get(jid)
    if not store or not store.job:
        raise HTTPException(404, "unknown job")
    if store.status != "ready":
        raise HTTPException(409, "still working on it")

    name = title or store.title
    store.title = name
    pages = [p for i, p in enumerate(store.job.pages) if i < len(store.keep) and store.keep[i]]
    if not pages:
        raise HTTPException(400, "no pages selected")

    opts = store.options.pdf
    opts.title = name
    data = build_pdf_bytes(pages, opts)
    filename = to_pdf_filename(name or os.path.splitext(os.path.basename(store.source))[0])
    return Response(
        data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
