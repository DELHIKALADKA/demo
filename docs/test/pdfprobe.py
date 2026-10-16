"""Inspect a PDF the way a reader would: how many pages, and what is on each one.

Used by the browser tests to check the file the app actually downloaded - the page
count, the order of the pages, and that each page carries the picture it should.

    python3 docs/test/pdfprobe.py notes.pdf            ->  JSON on stdout

Each page's image is decoded and averaged to one RGB triple, so a test can say
"page 1 is the red page, page 2 is the green one" instead of hoping.
"""

from __future__ import annotations

import io
import json
import sys

from pypdf import PdfReader


def average_colour(data: bytes) -> list[int] | None:
    try:
        from PIL import Image
    except ImportError:                       # pragma: no cover - Pillow is a test dep
        return None
    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("RGB").resize((32, 32))
        pixels = list(small.getdata())
    n = len(pixels)
    return [round(sum(p[i] for p in pixels) / n) for i in range(3)]


def describe(path: str) -> dict:
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        entry: dict = {
            "width": round(float(page.mediabox.width), 1),
            "height": round(float(page.mediabox.height), 1),
            "images": [],
        }
        try:
            for image in page.images:
                data = image.data
                entry["images"].append({
                    "name": image.name,
                    "bytes": len(data),
                    "jpeg": data[:2] == b"\xff\xd8",
                    "mean": average_colour(data),
                })
        except Exception as exc:              # a page with no image at all
            entry["imageError"] = f"{type(exc).__name__}: {exc}"
        pages.append(entry)

    result = {
        "path": path,
        "header": open(path, "rb").read(8).decode("latin-1"),
        "pageCount": len(reader.pages),
        "trailerRoot": "/Root" in (reader.trailer or {}),
        "pages": pages,
        "text": (reader.metadata or {}).get("/Title"),
    }
    return result


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    print(json.dumps(describe(sys.argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
