"""Turning captured frames into a tidy PDF - all offline, all on-device.

Design notes
------------
* Screenshots of text want to stay sharp. JPEG at quality 90+ is visually
  lossless for this content and roughly 5-10x smaller than PNG, so it is the
  default; lossless PNG is available for diagrams and handwriting.
* We never re-render text - each PDF page *is* the captured picture - so the PDF
  is byte-light, opens instantly on a phone, and looks exactly like the video.
* Page geometry is chosen so the notes fill the paper: the frame is scaled to fit
  the printable area and centred, with an option to use the exact aspect ratio of
  the video (no white bands at all).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

A4_W, A4_H = A4          # points
LETTER_W, LETTER_H = LETTER


@dataclass
class PdfOptions:
    page_mode: str = "a4"          # "a4" | "letter" | "exact" (page = image size)
    margin_mm: float = 8.0
    image_format: str = "jpeg"     # "jpeg" | "png"
    quality: int = 92              # jpeg quality
    max_width: int = 2200          # downscale wider captures (phone screenshots are ~1080-1440)
    grayscale: bool = False
    title: str = ""                # also the suggested filename
    subject: str = "Notes captured from a video by PageSnap"
    page_numbers: bool = False
    orientation: str = "auto"      # "auto" | "portrait" | "landscape"


@dataclass
class CapturedPage:
    """One screenshot, ready to be embedded."""

    image_bytes: bytes
    width: int
    height: int
    source_time: float = 0.0       # where in the video it came from
    reason: str = ""
    keep: bool = True              # the UI's checkbox
    label: str = ""

    def as_image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.image_bytes))


def _prepare(page: CapturedPage, opts: PdfOptions) -> Tuple[bytes, int, int]:
    """Scale/convert one capture for embedding. Returns (bytes, w, h)."""
    img = page.as_image()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if opts.grayscale and img.mode != "L":
        img = img.convert("L")

    if opts.max_width and img.width > opts.max_width:
        ratio = opts.max_width / float(img.width)
        img = img.resize((opts.max_width, max(1, int(img.height * ratio))), Image.LANCZOS)

    buf = io.BytesIO()
    if opts.image_format == "png":
        img.save(buf, format="PNG", optimize=True)
    else:
        img.save(buf, format="JPEG", quality=int(opts.quality), optimize=True,
                 progressive=True, subsampling=1)
    return buf.getvalue(), img.width, img.height


def build_pdf_bytes(pages: Sequence[CapturedPage], opts: Optional[PdfOptions] = None) -> bytes:
    """Render the kept captures into a PDF and return it as bytes."""
    opts = opts or PdfOptions()
    chosen = [p for p in pages if p.keep]
    if not chosen:
        raise ValueError("No pages selected - nothing to write.")

    out = io.BytesIO()
    c = pdf_canvas.Canvas(out, pagesize=A4)
    if opts.title:
        c.setTitle(opts.title)
    c.setSubject(opts.subject)
    c.setCreator("PageSnap")

    margin = opts.margin_mm * 72.0 / 25.4

    # Professional touches: page numbers are tiny and unobtrusive; PDF metadata
    # carries the name the student typed, so the file is searchable later.
    prepared = [_prepare(p, opts) for p in chosen]

    for index, (raw, w, h) in enumerate(prepared):
        landscape = w > h
        if opts.orientation == "landscape" or (opts.orientation == "auto" and landscape):
            page_w, page_h = A4_H, A4_W
        else:
            page_w, page_h = A4_W, A4_H

        if opts.page_mode == "exact":
            page_w, page_h = float(w), float(h)
            avail_w, avail_h = page_w, page_h
            margin = 0.0
        else:
            if opts.page_mode == "letter":
                page_w, page_h = (LETTER_H, LETTER_W) if landscape else (LETTER_W, LETTER_H)
            c.setPageSize((page_w, page_h))
            avail_w, avail_h = page_w - 2 * margin, page_h - 2 * margin

        scale = min(avail_w / w, avail_h / h)
        draw_w, draw_h = w * scale, h * scale
        x = (page_w - draw_w) / 2.0
        y = (page_h - draw_h) / 2.0

        if opts.page_mode == "exact":
            c.setPageSize((page_w, page_h))
        c.drawImage(ImageReader(io.BytesIO(raw)), x, y, draw_w, draw_h,
                    preserveAspectRatio=True, anchor="c", mask=None)

        if opts.page_numbers:
            c.setFont("Helvetica", 8)
            c.setFillGray(0.55)
            c.drawRightString(page_w - margin - 2, max(6, margin * 0.45), f"{index + 1} / {len(prepared)}")
        c.showPage()

    c.save()
    return out.getvalue()


def safe_filename(name: str, fallback: str = "notes") -> str:
    """A filename that is safe on Android, iOS, Windows and macOS."""
    cleaned = "".join(ch for ch in (name or "") if ch.isalnum() or ch in " -_().[]")
    cleaned = "-".join(cleaned.split()) if cleaned.strip() else ""
    cleaned = cleaned.strip(" .-_")
    if not cleaned:
        cleaned = fallback
    return cleaned[:80]


def to_pdf_filename(name: str, fallback: str = "notes") -> str:
    stem = safe_filename(name, fallback)
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


def contact_sheet(pages: Iterable[CapturedPage], columns: int = 3, thumb_w: int = 420,
                  labels: bool = True) -> bytes:
    """A single PNG showing every captured page - used by the debug tooling and
    by the app's 'review before saving' screen."""
    from PIL import ImageDraw

    items = [p for p in pages if p.keep]
    if not items:
        raise ValueError("No pages to show.")
    thumbs = []
    for p in items:
        img = p.as_image()
        ratio = thumb_w / float(img.width)
        thumbs.append(img.resize((thumb_w, max(1, int(img.height * ratio))), Image.LANCZOS))

    cell_h = max(t.height for t in thumbs) + (24 if labels else 0)
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_w + (columns + 1) * 10,
                              rows * cell_h + (rows + 1) * 10), (28, 30, 34))
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(thumbs):
        cx = (i % columns) * (thumb_w + 10) + 10
        cy = (i // columns) * (cell_h + 10) + 10
        sheet.paste(t, (cx, cy))
        if labels:
            draw.text((cx + 4, cy + t.height + 4), f"#{i + 1}  {items[i].source_time:.1f}s",
                      fill=(220, 220, 220))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()
