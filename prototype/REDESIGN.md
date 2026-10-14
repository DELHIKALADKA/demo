# PageSnap UI redesign — report

The detection engine, the server's processing logic and the existing workflow were
kept. This document says what was found, what changed, what was tested, and what is
still imperfect.

## 1. Framework and architecture discovered

| | |
|---|---|
| **Front end** | One static file, `prototype/static/index.html` — plain HTML, one `<style>` block, one `<script>` block. **No framework, no build step, no bundler, no npm dependencies at runtime.** |
| **Back end** | `prototype/server.py`, FastAPI + uvicorn. Job state in memory, files in a temp directory. |
| **Engine** | `engine/pagesnap/` — the working detector, ffmpeg wrapper and PDF writer. Driven only through `capture_pages()`, `build_pdf_bytes()`, `grab_frame()`, `contact_sheet()`. **Not touched.** |
| **Communication** | The page talks to the server over `/api/*` with `fetch`. Jobs are polled every 600 ms. |
| **Existing screens** | Three sections toggled by one function, `show('input' \| 'progress' \| 'result')`: import, progress, review+export combined. |

So the redesign was a **front-end restructuring of a single file**, not a migration.
Everything the script already did is still done by the same functions — I extracted
the original 532-line script, applied nine surgical patches to it, and appended the
new UI code. No handler was rewritten and no processing logic was moved.

## 2. Files changed

| file | what changed |
|---|---|
| `prototype/static/index.html` | rewritten around the existing script: design tokens, dark **and** light themes, SVG logo and icon set, seven screens, bottom navigation, responsive layout |
| `prototype/server.py` | **added** a real local library (saved PDFs + an index, and four endpoints). No existing route or processing code changed |
| `prototype/tests/ui_test.mjs` | four new sections (shell, navigation, themes, library, accessibility); existing sections kept |
| `prototype/tests/test_prototype.py` | a new library section; both suites now run against a throwaway library folder |
| `README.md` | points at this report |
| `prototype/REDESIGN.md` | this report |

The git checkpoint from before any of this work is the tag **`pre-ui-redesign`**
(`ca05c5c`).

## 3. Screens redesigned

| screen | state |
|---|---|
| **Home** | new — greeting, hero action card, both import options, recent PDFs, dismissible three-step guide, "work in progress" resume card |
| **Import (Create)** | redesigned — large upload card, link row with inline validation and live status, the real supported-source list, detection/picture options |
| **Processing** | redesigned — spinner, real stage text, real percentage, honest indeterminate bar when there is no figure, source and length, and a way to leave safely |
| **Review** | redesigned — grouped toolbars, responsive grid, page cards with a real tick button and labelled move/remove controls, sticky action bar with the live selection count, manual capture |
| **Export** | new screen, using the existing panel — editable name, selected count, the options actually in force, Create PDF, progress, verified result, download, preview, stale warning, retry-by-regenerating, and "open my Library" |
| **Library** | new, and backed by real files — name, page count, size, date, open, download, inline rename, delete with confirmation |
| **Settings** | new — appearance (Dark / Light / System), accurate privacy description, supported formats read from the server, about |

Bottom navigation: **Home · Create · Library · Settings**, one real screen each, with
`aria-current` marking the active destination, and it converts to a left rail above
900 px. Processing, review and export deliberately stay under **Create** — they are
one workflow, and duplicating them as tabs would have split it.

## 4. Features preserved

Every one of these is still the original code, verified working after the redesign:

- local video upload, and drag-and-drop on desktop
- link import, including YouTube Shorts classification, direct file links and the
  honest refusals (login-required, DRM, live, playlists, channels, web pages)
- the detection engine and its results (4 pages at 1.0 / 13.0 / 16.0 / 25.0 s for
  `lecture.mp4`, unchanged)
- review: tick/untick, remove, restore, reorder, select all, deselect all
- manual capture of a page at a timestamp
- contact-sheet preview, full-size page preview
- PDF generation, verification, staleness refusal, editable filename (including
  non-Latin names), download with a real `%PDF-` check, preview
- re-analysis at another sensitivity without re-uploading
- the sources guidance and all the error messages

## 5. New functionality implemented

1. **A real local library.** Saving a PDF also writes it to `~/PageSnap` with an
   `index.json`, so it survives a server restart and can be downloaded again. New
   endpoints: `GET /api/library`, `GET /api/library/{id}/file`,
   `POST /api/library/{id}` (rename), `DELETE /api/library/{id}`. The newest 30 are
   kept and the rest pruned. Nothing here is a cloud service or a database.
2. **Dark (default) and light themes**, applied to the whole document through 34
   design tokens, switched from Settings or the header, saved in `localStorage`, and
   applied before the first paint so there is no flash. A third option, **System**,
   follows the operating system.
3. **Bottom navigation** with real destinations and persistent state.
4. **A dismissible guide**, remembered locally, restorable from Settings.
5. **A resume card** that appears only when there really is work in progress.
6. **Accessibility work**: every icon-only button has a name, the page tick is a real
   `<button aria-pressed>`, move controls are labelled ("Move page 2 earlier"), focus
   rings are visible, the tab bar uses `aria-current`, error text is a live region,
   and destructive actions are confirmed.

### Two bugs the redesign exposed, and fixed

- **The Create tab opened Home.** `show('create')` was not a known screen, so it fell
  through to the default. Fixed in the router.
- **A freshly made PDF could be born stale.** Type a name and click *Create PDF* in
  the same instant and the rename request and the generate request raced: the PDF was
  built from the old revision, so the download button was disabled with a "regenerate"
  banner the moment it appeared. Generation now waits for any in-flight page edit.
  This was intermittent and would have hit real users; the browser test reproduced it
  about half the time before the fix, and has not failed in three runs since.

## 6. Tests actually performed, and their results

All of these were run, on this machine, against the real server and the real engine.

| what | result |
|---|---|
| `prototype/tests/test_prototype.py` — real server, real PDF bytes read back with pypdf, real filenames, real library files | **257 passed, 0 failed** |
| `prototype/tests/ui_test.mjs` — the real page in a DOM, whole workflow clicked through | **134 passed, 0 failed**, run 3× |
| `engine/tests/test_detect.py` — 5 fixtures × 3 sensitivities | **15/15**, identical landings to before the redesign |
| console errors / failed requests during the whole UI run | **0 console errors**; the only failed requests are the negatives the tests provoke; no 5xx; no tracebacks |
| live check on `localhost:4567` | page 85 KB, all screens and the SVG logo present, theme set pre-paint; upload → 4 pages → PDF verified → library entry → download 290 KB opening as 4 pages |
| CSS | 188/188 braces balanced, 34 tokens defined, 0 undefined `var()` references, 4 media queries |

Specific things the tests assert, rather than assume:

- the app opens on **Home**; each tab opens its own screen and marks itself current
- the guide hides, is remembered, and Settings can restore it
- **dark is the default**; Light switches the document and is saved; System resolves
  and is saved; the header toggle always leaves an explicit choice
- the library lists the PDF that was made, downloads real `%PDF-` bytes, renames
  inline (the server confirms the new name), and deletes for real
- every button has an accessible name; ticks are buttons with `aria-pressed`
- the viewport meta carries `viewport-fit=cover`; `overflow-x:hidden`,
  `overflow-wrap:anywhere`, `max-width` and `env(safe-area-inset-bottom)` are present
- upload, detection (same 4 pages), selection, removal, restore, reorder, capture,
  generation, staleness refusal, and download-with-verification all still work

### What these tests cannot tell you

- **jsdom does not lay out or paint.** So "no horizontal overflow on a phone" is
  checked by structure and CSS (no fixed width over 300 px, wrapping enabled, small
  grid minimums that fit a 320 px screen with the padding and gaps accounted for) and
  **not** by measuring a rendered layout. A real browser is needed for that, and there
  is none in this sandbox — see the run instructions below to look at it yourself.
- **Colour contrast was computed, not measured with a tool.** Body text is ≥ 12:1 on
  the dark surface and ≥ 15:1 on light; white on the primary gradient stays ≥ 4.5:1
  across all three stops (the gradient was deliberately darkened for that).
- YouTube/Instagram fetches still cannot be tested here (no route from this sandbox);
  the classification and honest-failure paths are covered, as before.

## 7. Known issues remaining

- **No layout measurement** in the sandbox, as above. Please give it one look at a
  narrow width.
- **The server holds jobs in memory.** A page refresh loses the current job (the
  Library keeps the PDFs). Restarting the server loses the jobs, not the library.
- **Library pruning** keeps the newest 30 files; older ones are removed with their
  entries, silently. There is no warning before that.
- **Renaming a library entry does not rename the file on disk** — the stored file
  keeps its id as its name, and the friendly name lives in the index and in the
  download headers. Nothing is lost, but the folder listing looks like ids.
- **No search or sorting in the Library.** With a 30-file cap it is not yet useful.
- **`prefers-color-scheme` is honoured only through the System option.** Dark is the
  default even on a light-preferring machine, because dark was the explicit brand
  requirement. If you would rather follow the OS on first run, change the default in
  `currentStoredTheme()` from `'dark'` to `'system'`.
- The theme is per browser, not per device — `localStorage`, so a different browser or
  a private window starts dark again.
- **No APK.** Unchanged from before; the Android sources stay in `android/` unbuilt.

## 8. How to run and preview it locally

```bash
pip install -r requirements.txt      # once
python prototype/server.py           # then open http://localhost:4567
```

It prints the address on startup and works from any directory. If 4567 is busy:
`python prototype/server.py --port 4570`.

To see the phone layout, open it in a browser and use the device toolbar
(Ctrl/Cmd + Shift + M in Chrome or Firefox) at 320–430 px wide, and check both themes
with the toggle in the header or Settings → Appearance.

To run the same tests that produced the numbers above:

```bash
cd prototype
npm install jsdom                    # once
npm test                             # 257 API checks, then 134 browser checks
```

Both suites start their own server on a free port and use a throwaway library folder,
so running them never touches the PDFs in `~/PageSnap`.
