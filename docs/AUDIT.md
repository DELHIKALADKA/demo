# `docs/` audit — what was broken, what was fixed, what it was tested with

The browser app in `docs/` (the one published to GitHub Pages). The UI, the detection
algorithm, the local-only processing and the PDF writer were preserved: the page
detection code (`pagesnap-core.js`, except nothing at all), the PDF writer's
generation (`buildPdf`) and the whole visual design are unchanged.

Everything below was checked by running it, not by reading it. Commands at the end.

## Fixed

| # | What was wrong | What it does now | Test |
|---|---|---|---|
| 1 | **"Show the full picture" was dead markup** — `display:none` forever, no handler. The lightbox showed the 300px thumbnail, not the page. | The button opens the selected page at full capture size (up to 2200px), with a labelled close button, focus moved into the viewer and returned on close, `Escape`/arrow keys, scroll lock, and a real dialog role. | browser [3], 15 checks |
| 2 | **Clicking a page's picture also unticked the page** (the click bubbled from the image to the card). | The picture opens the viewer; only the rest of the card toggles. | browser [3] |
| 3 | **The sample fetch never checked `response.ok`.** A 404 page was handed to the video element and reported as "could not be read as a video". | Status, content type and size are checked; each failure says what happened and offers the way out. | browser [5], 6 checks |
| 4 | **Start over could not process the same file twice.** The `<input type=file>` kept its value, so choosing the same file again fired no `change` event and nothing happened. | The input is cleared (and the job, grid, title, progress and blob URLs are reset with it). | browser [4], 10 checks |
| 5 | **The video element sat inside a card that is `display:none` while a job runs.** iOS Safari draws a blank frame from a hidden video, so screenshots come out black and detection finds nothing. | The video lives outside the step cards and stays rendered (invisible, 2px). | browser [2] asserts it stays rendered throughout a real run |
| 6 | **The format message promised everyone "mp4, mov, mkv, webm".** Safari cannot play Matroska at all; Chrome cannot play `.mov` by type. | The line is filled in from `canPlayType()`, and a note says the codec inside the file is what decides. Never claims what the browser does not report. | browser [1], run.mjs |
| 7 | **The single-file offline build was broken end to end.** A regex in `build_single.py` swallowed ten elements (`#err`, `#stage`, `#step-work`, `#step-review`, `#summary`, …), so it threw `Cannot set properties of null` on load and did nothing when a video was chosen. | The regex stops at the end of the sample row; the build fails if any element the app uses is missing; the sample button's handler is optional. | browser [9]; `build_single.py` self-check |
| 8 | **`safeFilename` stripped Indic vowel signs.** `\p{L}` does not match combining marks, so "अध्याय" became "अधयय" — a different word. Also: Windows-reserved names (CON, COM1) failed to save. | Letters, marks and numbers in any script are kept; reserved names get a safe prefix; path characters still can't escape. | run.mjs, 16 checks |
| 9 | **`canvasToJpeg` threw `TypeError: null is not an object`** when the browser refused to encode a canvas (memory on a phone). | A clear message about memory, with what to try. | covered by the code path; the failure itself is memory-dependent and was not reproduced |
| 10 | **Decode failures were a dead end** ("This file could not be read as a video"). | The message names the file and the usual cause (an iPhone HEVC recording), and says an mp4 with H.264 opens everywhere. | browser [6] |
| 11 | Dead CSS (`#4a5near`), progress/error text never announced, the drop area and the tick not reachable by keyboard, the duration vanishing from the summary after any toggle. | Fixed: valid CSS, `role="status"`/`role="alert"`, keyboard-operable drop area and real `<button>` ticks, summary keeps its duration. | browser [8], [3] |
| 12 | **Retrying the sample left the previous error on screen** while the new download ran, so you could not tell whether anything had been retried. | A new attempt clears the message first. | browser [5] |
| 13 | **A PDF that could not be written could look like it had saved.** | The failure is reported with its reason, nothing claims success, and the button comes back to try again. | browser [7] |

## Verified as already correct (no change needed)

* **The detection algorithm.** 15/15 on the engine's ground-truth fixtures at three
  sensitivities, unchanged — and in a real browser, decoding the shipped sample.
* **The PDF writer's generation.** Page count, page order, orientation, xref offsets,
  JPEG embedding: 14 unit checks plus reading the real downloaded file back.
* **Local-only processing.** No request leaves the page except `./sample/lecture.mp4`;
  the browser suite watches every console error for the whole session and found none.
* **The sample video and its promise** ("33 seconds, 4 pages of notes"): 4 pages, all
  four different pictures, at 1s / 13s / 16s / the final page.
* **Every way the sample can fail**: missing (404), a reply that is not a video, and a
  dropped connection (the fetch itself rejecting). Each gets its own message, and the
  app is still usable afterwards.

## Known limitations (not fixed, stated plainly)

* **The last page's timestamp.** The sample's final page is on screen from 25s to the
  end; the detector captures it at the end of the video (33.3s in this browser) rather
  than at 25s. It is the same picture, so the PDF is right, but the time shown under
  that thumbnail is the end of the video, not the moment the page appeared.
* **Windows-reserved titles** are saved as `_CON.pdf` rather than `CON.pdf` — on
  purpose; Windows refuses the latter.
* **A 20-minute cap** on videos, and no cap on page count: a very long lecture can use
  a lot of memory on a phone. The screenshots are held as blobs (one copy each, used by
  both the grid and the PDF), which is why this is survivable at all.
* **`mkv` is offered to a browser only when it reports support**; Chrome plays Matroska
  by sniffing even though `canPlayType` says no, and the app does not pretend otherwise.
* The PDF is downloaded with an anchor and `download`; on iOS Safari that can open the
  PDF in a tab instead of saving it. The page now says so and points at Share → Save to
  Files.

## What could not be tested here

* **iOS Safari and a real iPhone.** There is no Apple device and no Safari in this
  environment. The iOS-specific fixes are reasoned from the platform's documented
  behaviour (a `display:none` video draws blank) and checked structurally
  (`playsinline`, `muted`, `preload="auto"`, the element staying rendered, 44px
  targets, no sideways scroll at 390px) — but **nobody has run this on an iPhone**.
  Selecting a video from Files or Photos, and saving the PDF on iOS, are **untested**.
* **A downloaded PDF on a phone.** The download was tested in a desktop Chromium.
* **HEVC.** iPhones record it; Chromium here cannot decode it, so the app's clear
  refusal is tested (browser [6]) but successful HEVC decoding is not.
* **Screen readers.** Roles, labels and focus order are asserted; actual VoiceOver /
  TalkBack output is not.

## Running the tests

```bash
python3 docs/test/make_media.py     # test videos (needs Pillow + imageio-ffmpeg)
node docs/test/run.mjs              # engine + sample + PDF + filenames   (72 checks)
npm install --prefix docs           # puppeteer-core, for the browser tests only
node docs/test/browser.mjs          # the real app in a real browser      (114 checks)
python3 docs/build_single.py        # single-file build + element check
python3 docs/build_site.py          # the deployable _site/ + validations
```

`browser.mjs` needs a browser: `PAGESNAP_CHROME=/path/to/chrome`, or it looks in the
usual places. With none, or without `puppeteer-core`, it prints SKIPPED and exits 0 —
that is not the same as passing, and it says which checks did not run. On Windows the
python commands are `py`.
