# What was wrong, what changed, and how to check it

The detection engine (`engine/`) was **not touched**. Every change is in the
server and the page around it.

## Files changed

| file | change |
|---|---|
| `prototype/server.py` | rewritten: URL validation, honest error mapping, background jobs, stable page ids, PDF generation + verification, manual capture, re-analysis |
| `prototype/static/index.html` | rewritten against the new API; same visual style as before |
| `prototype/tests/test_prototype.py` | new — 82 checks against a real running server |
| `prototype/tests/ui_test.mjs` | new — 50 checks driving the real page in a DOM |
| `prototype/package.json` | new — `npm test` runs both suites |
| `README.md` | documents the prototype workflow, what it fetches and what it refuses |
| `.gitignore` | ignores `node_modules/` and the test logs |

## The actual causes, and the fixes

### 1. A file that is not a video was accepted as a video

This was the worst one. Uploading a plain text file reported **success** and
produced a one-page PDF. The cause was not a missing check but a wrong one:

```
$ ffmpeg -i notes.txt
Input #0, tty, from 'notes.txt':
  Duration: 00:00:01.60
  Stream #0:0: Video: ansi, pal8, 640x400, 25 fps
```

FFmpeg *renders* a text file as a 640×400 terminal stream. So `probe_duration()`
returned 1.6 seconds and `grab_frame()` returned a real 1743-byte PNG — the file
genuinely was a decodable video source, just not a video anyone meant to upload.

**Fix:** ask what the *container* is, not whether pixels came out. `tty`,
`image2`, `lavfi`, subtitle formats and capture devices are refused by name, with
ffmpeg's own explanation passed through:

- `notes.txt` → *"That file is not a video — ffmpeg reads it as plain text."*
- `chapter.txt` renamed to `.mp4` → *"That file could not be opened as a video (ffmpeg says: Invalid data found when processing input)."*
- a real `.jpg` → *"That file is not a video — ffmpeg reads it as a single still image."*

Both of the first two are now regression tests.

### 2. Link failures were raw dumps, and one class was misdiagnosed

yt-dlp's message was shown as-is, so a student saw `Unable to download webpage:
<urlopen error [Errno 111] Connection refused>`. Worse, my first attempt at
mapping these classified an unreachable host as *"not a video"*, because a refused
connection also produces `urlopen error`.

**Fix:** ordered rules, most specific first. A 404 is a *missing video*; a refused
connection is a *network problem*. Verified against nine real strings:

| yt-dlp says | the student is told |
|---|---|
| `urlopen error [Errno 111] Connection refused` | the site could not be reached — check the connection |
| `HTTP Error 404` | the video is not there any more, or the link is wrong |
| `Unsupported URL` | that link looks like a web page, not a video |
| `TLS/SSL connection has been closed` | the site could not be reached |
| `Sign in to confirm you're not a bot` | private, or an account is wanted — upload the file instead |
| `DRM protected` | copy-protected, refused by design |
| `This live stream is not available` | live streams have nothing finished to snap |
| `This video is private` | private — upload the file instead |

Every one of them ends with the same sentence: *"You can always choose a video
from this device instead."*

### 3. The download ran inside the request

`POST /api/fetch` downloaded the video before answering, so the page hung with no
progress and a slow site looked like a freeze.

**Fix:** the request returns a job id immediately; the download and the detection
run in a worker thread and the page polls. An unreachable host now fails in
seconds with a connection error rather than hanging — asserted by a test that
requires the failure to name the connection.

### 4. Reordering captured the wrong images

Pages were addressed by position (`/thumb/0`), so after a reorder the UI was
showing one image while the server kept another.

**Fix:** every page has a stable `uid`; thumbnails are `/thumb/{uid}`. The test
moves page 1 right and checks the DOM and the PDF both follow.

### 5. The PDF could only be built once, and could go stale silently

The download was a `window.location` navigation to `GET /pdf`, which meant
generating a PDF *was* navigating away, and any later edit left the old file
being served as if nothing had happened.

**Fix:** generation and download are separate. Editing the selection (remove,
restore, reorder, capture) marks the PDF **stale** — the panel says why, the
download button is disabled, and the single button becomes *Regenerate PDF*. The
server refuses to serve a stale or absent file with `409` and a message, so a
stale PDF can never be handed out silently. Generating re-reads the written file
and verifies it before offering it.

## What works now

Paste a link **or** choose a file → detect → review → tick / remove / restore /
reorder / capture a page at a timestamp → preview the selection → **Generate
PDF** → preview → **Download PDF** → edit → **Regenerate PDF**.

Regenerating never re-uploads and never re-decodes the video: the page images are
already on disk, so it is immediate. Changing the sensitivity re-runs only the
detection, from the file already on disk.

Verified against the real files, not mocks — the PDF bytes are read back with
`pypdf` (`A4 842×595`, page images at native `1280×720`), and each embedded page
is matched against the page image it came from.

## What it will not do, and why

- **Instagram and Facebook links usually fail** with a login error. That is the
  honest answer, and it is what the message says. Signing in on the user's behalf
  is not something this will do.
- **DRM-protected, paywalled and live content is refused by design.**
- **No site is bypassed.** If the platform does not serve the file to an
  anonymous request, the tool says so and points at the upload instead.
- Videos over 90 minutes or 600 MB are refused.
- Jobs live in memory and files in `/tmp/pagesnap-jobs`, so **restarting the
  server clears them**. That is fine for a prototype; the phone app keeps notes in
  app storage.
- This is a prototype with a server. The shareable no-server app is `docs/`, and
  it cannot download links from a browser at all (CORS), which is exactly why it
  only accepts files.

## How to run and test it

```bash
cd prototype
pip install fastapi "uvicorn[standard]" python-multipart yt-dlp pypdf
python server.py --port 8000          # open http://localhost:8000

npm install jsdom                     # once
npm test                              # 82 API checks, then 50 browser checks
```

Manual pass, two minutes:

1. Choose `engine/tests/fixtures/lecture.mp4` → expect **4 pages** at 1.0, 13.0,
   16.0 and 25.0 seconds.
2. Untick one, move another, type a timestamp in *add a page* → the banner turns
   amber and Download is disabled.
3. **Regenerate PDF** → the banner clears, Generate turns back into *Generate
   PDF*, page count in the panel matches the selection.
4. Download → a real PDF opens in any reader.
5. Paste `https://www.instagram.com/reel/xyz/` → a login explanation and the
   upload fallback, not a fake success.
6. Choose any `.txt` file → refused, with the reason.

One thing the sandbox cannot do: there is no browser here, so no screenshot was
taken. The page's behaviour is covered by the 50 DOM checks (no console errors, no
unexpected failed requests, and the download bytes start with `%PDF-`), but
please give it one look yourself.
