# What was wrong, what changed, and how to check it

The detection engine (`engine/`) was **not touched**. Every change is in the
server and the page around it.

## Files changed

| file | change |
|---|---|
| `prototype/server.py` | rewritten: URL validation, honest error mapping, background jobs, stable page ids, PDF generation + verification, manual capture, re-analysis |
| `prototype/static/index.html` | rewritten against the new API; same visual style as before |
| `prototype/tests/test_prototype.py` | new — 243 checks against a real running server |
| `prototype/tests/ui_test.mjs` | new — 80 checks driving the real page in a DOM |
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

## Second pass: the download path, and link classification

Five more defects, all found by reading the code rather than by guessing at the
browser.

### 6. An expired link was blamed on the user's internet

`403 Forbidden` from a link whose token had run out — the classic lecture-recording
failure — was reported as *"The site could not be reached from this computer."*
So was `401`, and a deleted video (`410`). Telling a student to check their
connection when the link had simply expired sends them looking in the wrong place.

**Fix:** `401`, `403`, `expired`, `signature`, `token` and `session` are recognised
as **an expired or refused link**, with advice that names the real cause: *"open the
page while it still works and save the video, then choose the file here"*. `410`
joined `404` as **the video is gone**.

### 7. A web page was handed to the downloader and the error explained afterwards

Any link that was not obviously a media file went to yt-dlp, and whatever it said
became the message. An ordinary web page is now asked about first: the server is
queried for its `Content-Type` (headers only, never a download).

- `video/*` or `application/octet-stream` → fetch it as a file, even with no
  `.mp4` anywhere in the URL
- `text/*` or `…html` → refused as **a web page, not a video file**, and told to
  save the video and upload it
- anything else → the downloader's own reason, mapped as before

### 8. Redirects were followed blindly

A redirect is a second URL, and the address that was checked was only the first
one. Every hop is now checked with the same rule, so a public link cannot bounce
onto a private or link-local address.

### 9. A PDF failure showed advice about video links

The error helper ended every message with *"You can always choose a video from this
device instead"* — correct for a failed link, nonsense under a failed PDF download.
PDF actions now report without it.

### 10. The download could not tell a PDF from anything else

The page saved whatever bytes came back. An empty body, a truncated file, or an
error page served with a `200` would all have been saved as a `.pdf` that does not
open — the worst outcome, and a silent one.

**Fix:** the bytes are checked before they reach the save dialog — empty is
refused, and the file must begin with `%PDF-`. A missing or wrong MIME type is
corrected rather than trusted. The filename is forced to end in `.pdf`, the object
URL is revoked 20 seconds after the click (never immediately, which can cancel a
download that has not started), and while the file is stale the **preview** button
is disabled along with the download — the server refuses a stale file anyway, so
the button should not offer it.

## Third pass: the download bug, and YouTube Shorts

### 11. A title in Hindi, Punjabi or Chinese made the download answer 500

This was the download bug, and it is worth stating exactly because it looked like a
browser problem and was not one.

```
$ curl -X POST .../pdf -d '{"title": "\u0905\u0927\u094d\u092f\u093e\u092f 6"}'   # "chapter 6"
$ curl .../pdf?mode=download
HTTP 500
UnicodeEncodeError: 'latin-1' codec can't encode character '\u7b2c' in position 22
```

The PDF was generated perfectly every time. Sending it was what failed. `Starlette`
encodes response headers as latin-1, and the filename went into `Content-Disposition`
exactly as the user typed it — so any title outside latin-1 took the whole download
down. `Café Notes` worked (é fits in latin-1); `अध्याय ६` did not; the failure hit
English speakers with a stray curly quote too.

**Fix, in two parts:**

- The name now travels twice, as RFC 6266/5987 intends: `filename="Chapter-6.pdf"`
  (ASCII, always encodable) and `filename*=UTF-8''...` (percent-encoded, the real
  name). Every current browser prefers `filename*`, so the file saves as
  `अध्याय-६.pdf`; anything older gets the ASCII fallback.
- The name is no longer built by the engine's `safe_filename`, which keeps only
  `isalnum()` characters — Devanagari vowel signs are not alphanumeric, so it was
  silently mangling the word (`अध्याय` → `अधयय`). The server now strips only what
  genuinely breaks a filename (`/ \ : * ? " < > |`, control characters, leading and
  trailing dots and dashes).

Tested across nine titles: English, Hindi, Punjabi, accented Latin, CJK, quotes,
punctuation only, empty, and a path (`../etc/passwd` → `etcpasswd.pdf`). Every one
downloads, opens in `pypdf`, and has the pages that are currently selected.

### 12. A YouTube Short is its own kind of source, and says so

A Short is not a file and not a generic web page. Pasting
`https://www.youtube.com/shorts/Sh8EPts-DXk` now:

1. **Recognises** it as a YouTube Short and **extracts the video id** (`Sh8EPts-DXk`).
2. **Normalises** it to the canonical watch URL, so one code path handles a Short, a
   `/watch` link, a `youtu.be` link, a `/live` link and an embedded player — all of
   which are the same video at different addresses. The id is read out of the URL;
   **an embedded player is never treated as a downloadable file**, it is a page.
3. **Refuses, before any work, the things that are not a video**: playlists,
   channels (including `@handles`) and search pages each get a message naming what
   the link actually is.
4. **Fetches only through the official extractor** (yt-dlp). Nothing signs in as the
   user, nothing touches DRM, and no playback URL is scraped out of a player.
5. **Only reports ready when the file is on disk** and has passed the container
   check, exactly as before. A Short that cannot be fetched never reports success.
6. When it cannot be fetched, it says **why**, in those words:

   > You could not be reached from this machine… The video itself is fine — it plays
   > in your browser, and nothing about the link is wrong. This server simply cannot
   > get to YouTube from here. Save the video where it does play, then choose the
   > file here.

   And the local file upload is right there on the same screen.

**What could not be tested here, honestly:** this sandbox has no route to YouTube
(`TLS/SSL connection has been closed`), so a real Short has never been downloaded in
this environment. What *is* tested: the classification of sixteen URL forms, the
refusals, the video id, and — for the fetch path itself — a local page that embeds a
video, which yt-dlp resolves and downloads, after which the detector finds all four
pages. That is the same code path YouTube uses; the only untested link in the chain
is YouTube's own extractor, which needs a network that can reach it. On a machine
that can reach YouTube, the identical flow runs.

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
npm test                              # 243 API checks, then 80 browser checks
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

The download path is tested with the server deliberately broken underneath it: a
`200` carrying HTML instead of a PDF, a `409` refusal, and an empty body. Each one
must surface a readable error, must **not** save a file, must not offer video
advice, and the button must recover once the server is healthy again.

One thing the sandbox cannot do: there is no browser here, so no screenshot was
taken. The page's behaviour is covered by the 80 DOM checks (no console errors, no
unexpected failed requests, and the download bytes start with `%PDF-`), but
please give it one look yourself.
