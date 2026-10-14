"""The whole job: video in, PDF out - no network, no database, no server.

    from pagesnap.pipeline import job_from_video, JobOptions
    job = job_from_video("lecture.mp4", JobOptions(title="Biology Ch 6"))
    open("notes.pdf", "wb").write(job.pdf_bytes)

`Job` also carries the captured pages themselves, so a UI can show them for
review (untick, rename, reorder) before the PDF is written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .config import DetectConfig, Sensitivity
from .detect import PageCandidate, PageDetector
from .ffmpegio import FFmpegNotFound, grab_frame, probe_duration
from .pdf import CapturedPage, PdfOptions, build_pdf_bytes, to_pdf_filename

ProgressFn = Callable[[str, float], None]


@dataclass
class JobOptions:
    sensitivity: Sensitivity = "balanced"
    pdf: PdfOptions = field(default_factory=PdfOptions)
    detect: Optional[DetectConfig] = None
    #: capture at this multiple of the video's resolution (1.0 = native pixels)
    scale: float = 1.0
    #: keep the frames on disk as well (useful for debugging the detector)
    dump_frames_to: Optional[str] = None


@dataclass
class Job:
    pages: List[CapturedPage] = field(default_factory=list)
    duration: float = 0.0
    analyzed_seconds: float = 0.0
    sampled: int = 0
    notes: List[str] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)
    candidates: List[PageCandidate] = field(default_factory=list)
    pdf_bytes: bytes = b""
    pdf_name: str = "notes.pdf"

    @property
    def captured(self) -> int:
        return len([p for p in self.pages if p.keep])

    def summary(self) -> str:
        return (f"{self.captured} page(s) from {self.duration:.1f}s of video "
                f"({self.captured / max(self.duration, 0.001) * 60:.1f} pages/minute)")


def _to_captured(raw: bytes, page: PageCandidate, scale: float, label: str) -> CapturedPage:
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(raw))
    if scale and abs(scale - 1.0) > 1e-3:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")           # lossless here; PDF does the compressing
    return CapturedPage(
        image_bytes=buf.getvalue(), width=img.width, height=img.height,
        source_time=page.time, reason=page.reason, label=label,
    )


def capture_pages(
    video_path: str,
    opts: Optional[JobOptions] = None,
    progress: Optional[ProgressFn] = None,
) -> Job:
    """Analyse the video and pull the full-resolution screenshot of every page."""
    opts = opts or JobOptions()
    cfg = opts.detect or DetectConfig.preset(opts.sensitivity)
    job = Job()
    job.pdf_name = to_pdf_filename(opts.pdf.title or os.path.splitext(os.path.basename(video_path))[0])

    def say(stage: str, fraction: float) -> None:
        if progress:
            progress(stage, max(0.0, min(1.0, fraction)))

    say("reading video", 0.02)
    job.duration = probe_duration(video_path) or 0.0

    say("finding page changes", 0.10)
    detector = PageDetector(cfg)
    sigs = detector.signatures_from_video(video_path)
    report = detector.detect_from_signatures(sigs)
    job.sampled = report.sampled
    job.notes = list(report.notes)
    job.thresholds = dict(report.thresholds)
    job.candidates = list(report.pages)

    if not report.pages:
        job.notes.append("No page changes found - the video may be a single static page.")
    if progress:
        progress(f"capturing {len(report.pages)} page(s)", 0.5)

    for i, page in enumerate(report.pages):
        raw = grab_frame(video_path, page.time, "png")
        if raw is None:
            job.notes.append(f"Could not read the frame at {page.time:.2f}s")
            continue
        job.pages.append(_to_captured(raw, page, opts.scale, f"page {i + 1}"))
        if progress:
            progress(f"capturing page {i + 1} of {len(report.pages)}",
                     0.5 + 0.45 * (i + 1) / max(1, len(report.pages)))
        if opts.dump_frames_to:
            os.makedirs(opts.dump_frames_to, exist_ok=True)
            with open(os.path.join(opts.dump_frames_to, f"page_{i + 1:03d}_{page.time:07.2f}s.png"), "wb") as fh:
                fh.write(raw)

    say("done", 1.0)
    return job


def write_pdf(job: Job, opts: Optional[JobOptions] = None) -> bytes:
    """Write the PDF for the pages currently ticked. Call again after the user
    unticked something or renamed the notes - it is pure computation, no state."""
    opts = opts or JobOptions()
    job.pdf_bytes = build_pdf_bytes(job.pages, opts.pdf)
    job.pdf_name = to_pdf_filename(opts.pdf.title or "notes")
    return job.pdf_bytes


def video_to_pdf(
    video_path: str,
    out_path: str,
    opts: Optional[JobOptions] = None,
    progress: Optional[ProgressFn] = None,
) -> Job:
    """One call, start to finish: analyse, capture, write the file."""
    opts = opts or JobOptions()
    if not opts.pdf.title:
        opts.pdf.title = os.path.splitext(os.path.basename(video_path))[0]
    job = capture_pages(video_path, opts, progress)
    if not job.pages:
        raise ValueError("No pages were captured, so no PDF was written.")
    data = write_pdf(job, opts)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return job


__all__ = [
    "FFmpegNotFound", "Job", "JobOptions", "capture_pages", "video_to_pdf", "write_pdf",
]
