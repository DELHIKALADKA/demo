# PageSnap upgrade — architecture and plan (written before any code changed)

## What exists today

| Part | What it is | Goes on GitHub Pages? |
|---|---|---|
| `docs/` | The static browser app. Plain ES modules, **no build step, no dependencies**: `index.html` (UI + workflow), `pagesnap-core.js` (page detection), `pagenotes-pdf.js` (a small PDF writer written from scratch), `build_site.py`, `build_single.py`, `test/run.mjs`, `test/browser.mjs` | **Yes** — this is `main` + `/docs` |
| `prototype/` | FastAPI + yt-dlp server (`server.py`, 1648 lines) with `/api/fetch`, `/api/upload`, jobs, library | No — needs Python |
| `engine/` | Python reference implementation of the detector | No |
| `android/` | Wrapper app | No |

### The pipeline in `docs/` today

```
video file → sampleVideo() 3 fps → detect() → for each page:
   seek → draw video frame on canvas at native size (capped 2200 px wide)
        → JPEG blob → grid card {time, reason, blob, url, width, height, keep}
                                   → buildPdf() → download
```

Facts that shape the plan:

1. **Images already exist as an ordered list** — `pages[]` — each with real `width`/`height` and its own JPEG bytes. Features 1 and 2 can build on that instead of inventing a second store.
2. **`buildPdf(pages, opts)`** already takes `pageSize: a4|letter|exact`, `marginPt`, `pageNumbers`, `title`; it auto-rotates landscape and always fits by *contain*. Order and page count follow the array.
3. **The static page cannot fetch remote media.** A browser fetch of `instagram.com/...` is blocked by CORS and by the platform; there is no server behind `docs/`. Only `prototype/` can fetch links, and it does so with yt-dlp — for **video**, not for photo posts. Anything else would be a lie.
4. The detector (`pagesnap-core.js`) is off limits and needs no change.

## Plan

### New file: `docs/pagesnap-media.js` (pure, no DOM)
Everything here is testable in Node without a browser, which is why it is separate:

* `parseTimecode(text)` / `formatTimecode(seconds)` — `HH:MM.mmm`, `HH:MM:SS.mmm`, `MM:SS`, plain seconds; rejects nonsense with a reason.
* `classifyUrl(text)` — recognises YouTube (`watch?v=`, `youtu.be`, `/shorts/`, `/live/`, `/embed/`, `/post/`, mobile hosts), Instagram (`/p/`, `/reel/`, `/reels/`, `/tv/`, `instagr.am`), Facebook (`/reel/`, `/watch/?v=`, `/videos/`, `/photo`, `/photos/`, `/media/set`, `fb.watch`, `fb.me`, `m.facebook.com`); distinguishes **video** from **photo post/carousel**; strips tracking (`si`, `igsh`, `utm_*`, `fbclid`); refuses `javascript:`/`data:`/`file:`/credentials/odd ports.
* Honest per-link verdicts: a YouTube video yields **frames, not photographs**; a photo post needs the server; a Reel/Short is frame material.
* `describeResolution`, `isLowRes`, `humanBytes`.

### `docs/pagenotes-pdf.js` — extend, keep every current call working
* Add `orientation: 'auto'|'portrait'|'landscape'` and `fit: 'contain'|'stretch'|'actual'` (defaults unchanged: auto + contain).
* Add **lossless embedding** for images with transparency: raw RGBA → `/FlateDecode` with `/SMask`, compressed with `CompressionStream('deflate')`, used when available. JPEG stays `/DCTDecode`, embedded byte-for-byte as now.
* `layout: 'one-per-page'` stays the only layout; it is what "one image per page" means and it never silently drops an image.

### `docs/index.html` — same design, same ids, new boxes
All existing ids and handlers stay (`#pages`, `#tick`, `#shot`, `#download`, `#summary`, `#bigger`, `#lightbox`, `#startover`, …) so the existing 114 browser checks keep guarding the app.

* **PDF export box**: count of selected images, move up/down, remove, page size (A4 / Letter / image size), orientation, fit mode, filename, progress, errors. Reads the same `pages[]` the grid shows, so the file cannot disagree with the screen.
* **"Capture a Frame / Replace a Photo"** at the end of the review step: video + `HH:MM.mmm` box + seek preview + Capture (appends), and Replace (swaps one image, keeps order, with Undo).
* **Link box** in the first step: identifies the platform and content type, then tells the truth — this page cannot fetch it; the server can for video links; download the media and upload it here instead.
* Source resolution shown on every card, with a warning when the source itself is small.

### Tests
`docs/test/run.mjs` gains checks for every pure function and every new PDF option; `docs/test/browser.mjs` gains checks that the new boxes really move, remove, replace and capture. Existing checks are kept, not deleted.

### Honest scope
* Photo posts / carousels / community posts: **recognition implemented, extraction not** — it needs an authenticated scraper, which I will not build or fake.
* Remote extraction stays in `prototype/`, documented with its own deployment story. No keys in frontend code, ever.
