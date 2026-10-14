"""PageSnap command line - turn a lecture video into a PDF of its pages.

    python -m pagesnap.cli lecture.mp4 -o biology-ch6.pdf --title "Biology Ch 6"
    python -m pagesnap.cli lecture.mp4 --list         # show the pages it found
    python -m pagesnap.cli lecture.mp4 --save-frames ./pages
    python -m pagesnap.cli lecture.mp4 --explain      # why each page was captured
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .config import DetectConfig
from .pdf import CapturedPage, PdfOptions, contact_sheet
from .pipeline import JobOptions, capture_pages, video_to_pdf


def _progress(stage: str, fraction: float) -> None:
    bar = "#" * int(fraction * 28)
    sys.stderr.write(f"\r  [{bar:<28}] {fraction * 100:5.1f}%  {stage:<38}")
    sys.stderr.flush()


def _explain(job) -> None:
    print("\n  why each page was captured")
    print("  " + "-" * 68)
    reasons = {
        "first": "first readable page of the video",
        "cut":   "the picture cut straight to a new page",
        "drift": "the page stopped changing (writing/panning finished)",
        "last":  "final page of the video",
    }
    for i, page in enumerate(job.pages):
        src = job.candidates[i] if i < len(job.candidates) else None
        why = reasons.get(src.reason if src else "", "")
        score = f"signal {src.score:.3f}" if src else ""
        print(f"   page {i + 1:>2}  t={page.source_time:7.2f}s  {why:<48} {score}")
    if job.thresholds:
        t = job.thresholds
        print(f"\n  thresholds on this video: cut {t['cut']:.3f}  drift {t['drift']:.3f}  "
              f"still {t['settle']:.3f}  (video noise floor {t['quiet']:.3f})")
    print(f"  analysed {job.sampled} samples in {job.analyzed_seconds:.2f}s "
          f"= {job.duration / max(job.analyzed_seconds, 1e-6):.0f}x realtime")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pagesnap",
        description="Turn a video of notes into a clean PDF of its pages, offline.",
    )
    p.add_argument("video", help="the video file (mp4, mkv, mov, webm, ...)")
    p.add_argument("-o", "--out", help="where to write the PDF")
    p.add_argument("--title", default="", help="name shown inside the PDF and used for the filename")
    p.add_argument("--sensitivity", choices=["strict", "balanced", "eager"], default="balanced",
                   help="strict = fewer, sure pages; eager = catch subtle changes")
    p.add_argument("--format", choices=["jpeg", "png"], default="jpeg",
                   help="how pictures are stored inside the PDF (png = lossless, bigger)")
    p.add_argument("--quality", type=int, default=92, help="jpeg quality, 60-100")
    p.add_argument("--max-width", type=int, default=2200, help="downscale captures wider than this")
    p.add_argument("--grayscale", action="store_true", help="smaller file for black-and-white notes")
    p.add_argument("--page-size", choices=["a4", "letter", "exact"], default="a4",
                   help="exact = each PDF page is the shape of the video frame")
    p.add_argument("--margin-mm", type=float, default=8.0, help="white border in mm (a4/letter only)")
    p.add_argument("--page-numbers", action="store_true")
    p.add_argument("--scale", type=float, default=1.0, help="capture scale, 1.0 = native pixels")
    p.add_argument("--max-pages", type=int, default=400, help="safety cap")
    p.add_argument("--skip-first", type=float, default=0.0, help="ignore the first N seconds (intro/ad)")
    p.add_argument("--list", action="store_true", help="only list the pages found, write nothing")
    p.add_argument("--explain", action="store_true", help="explain why each page was captured")
    p.add_argument("--save-frames", metavar="DIR", help="also save each screenshot as a PNG")
    p.add_argument("--contact-sheet", metavar="PNG", help="write one image showing every page")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.exists(args.video):
        print(f"error: no such file: {args.video}", file=sys.stderr)
        return 2
    if not args.list and not args.out:
        args.out = os.path.splitext(os.path.basename(args.video))[0] + "-notes.pdf"

    cfg = DetectConfig.preset(args.sensitivity)
    cfg.max_pages = args.max_pages
    cfg.skip_first_seconds = args.skip_first

    opts = JobOptions(
        sensitivity=args.sensitivity,
        detect=cfg,
        scale=args.scale,
        dump_frames_to=args.save_frames,
        pdf=PdfOptions(
            page_mode=args.page_size,
            margin_mm=args.margin_mm,
            image_format=args.format,
            quality=args.quality,
            max_width=args.max_width,
            grayscale=args.grayscale,
            title=args.title,
            page_numbers=args.page_numbers,
        ),
    )

    print(f"PageSnap - {os.path.basename(args.video)}")
    started = time.time()

    try:
        if args.list:
            job = capture_pages(args.video, opts, _progress)
            job.analyzed_seconds = time.time() - started
            sys.stderr.write("\r" + " " * 78 + "\r")
            print(f"  {job.summary()}")
            for i, page in enumerate(job.pages):
                print(f"   page {i + 1:>2}  at {page.source_time:7.2f}s  ({page.width}x{page.height})")
            if args.explain:
                _explain(job)
            return 0 if job.pages else 1

        job = video_to_pdf(args.video, args.out, opts, _progress)
        job.analyzed_seconds = time.time() - started
        sys.stderr.write("\r" + " " * 78 + "\r")
        size_mb = os.path.getsize(args.out) / 1e6
        print(f"  {job.summary()}")
        print(f"  wrote {args.out}  ({size_mb:.2f} MB)")
        if args.explain:
            _explain(job)
        if args.contact_sheet:
            with open(args.contact_sheet, "wb") as fh:
                fh.write(contact_sheet(job.pages))
            print(f"  contact sheet: {args.contact_sheet}")
        for note in job.notes:
            print(f"  note: {note}")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
