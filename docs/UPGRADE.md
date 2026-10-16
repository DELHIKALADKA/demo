# What PageSnap does now, and where the line is

This page is written for whoever picks the project up next, and for anyone who wants to
know what is real before trusting it.

## The two environments

| | files | served at | how it is checked |
|---|---|---|---|
| development | `docs/`, as you edit them | `http://localhost:5173/` | `browser.mjs` |
| production | the built `_site/` | `https://delhikaladka.github.io/demo/` | `site.mjs` |

The app lives at the **root** in development and under `/demo/` in production, so every
reference inside it is relative and the build refuses to publish a root-absolute one.
`python3 docs/serve.py --root _site --prefix /demo/` reproduces the production layout on
this machine, which is the only reliable way to notice the difference before it is live.

## The architecture (unchanged in shape)

```
docs/                     the static app - this is what GitHub Pages publishes
  index.html              the whole workflow: pick, find, review, export, capture
  pagesnap-core.js        page detection (3 fps sampling + distance rules). Untouched.
  pagenotes-pdf.js        the PDF writer, written from scratch, no dependencies
  pagesnap-media.js       NEW: link recognition and timecode parsing (pure, no DOM)
  build_site.py           builds the deployable _site/, and checks its own paths
  build_single.py         builds the one-file offline copy
  manifest.webmanifest    name, icons and colours for "Add to Home Screen"
  test/run.mjs            engine, sample, PDF, links, timecodes      (170 checks)
  test/browser.mjs        the real app in a real browser             (143 checks)
  test/site.mjs           the built site under /demo/, as Pages serves it  (39 checks)
  test/test_build_guards.py  breaks the build on purpose, expects refusal  (20 checks)
  test/make_media.py      builds the test videos on demand
  test/pdfprobe.py        reads a produced PDF back with pypdf + Pillow

prototype/                the FastAPI + yt-dlp server. NOT published on Pages.
engine/                   Python reference detector. Unchanged.
android/                  wrapper app. Unchanged.
```

Nothing loads from a CDN, nothing needs a build step, and no key or token is in any
frontend file — the app has no server to talk to.

## What runs in the browser, and what cannot

| Job | Where it runs | Why |
|---|---|---|
| Finding pages in a video you picked | Browser | The file is read locally; nothing is uploaded. |
| Capturing a frame at a timecode | Browser | Same file, seeked with `<video>` and drawn to a canvas. |
| Reordering, removing, replacing pictures | Browser | They are already in memory. |
| Writing the PDF | Browser | `pagenotes-pdf.js` assembles the bytes. |
| Recognising a pasted link | Browser | Pure string work (`pagesnap-media.js`). |
| **Fetching media from YouTube / Instagram / Facebook** | **Not the browser** | A page cannot read another site cross-origin. Instagram, Facebook and YouTube also require a signed-in, non-automated viewer for most media, and their URLs expire within minutes. |

**This is why the link box answers instead of downloading.** It names the platform, the
content type, and what that actually yields (a YouTube video yields *frames*, not post
photographs), then says plainly that this page cannot fetch it and what to do instead:
save the media and upload the file, which then runs entirely on the device.

### Where remote extraction does exist

`prototype/server.py` already does it for **video**, with `yt-dlp`, and it refuses the
cases it cannot honour: DRM-protected video, live streams, private posts, and anything
that needs a login. It runs on your own machine (`python prototype/server.py`), never on
GitHub Pages. If you ever deploy it publicly:

* keep it a separate service from the static site — do not put it behind the same origin;
* set its limits (`MAX_VIDEO_BYTES`, the 20-minute cap, the job store's TTL) and add a
  request timeout and a per-IP rate limit;
* allow only the hostnames you intend to support, and follow redirects only to those
  hostnames — an open fetcher is an SSRF hole;
* keep `yt-dlp` up to date; platforms change constantly and it is the thing that breaks;
* put no credentials in the frontend, and if you add an official API (YouTube Data API,
  Instagram Graph API), keep its key server-side and follow that platform's terms.

**Photo posts and carousels are not implemented for remote links.** yt-dlp does not
return them, and scraping them means defeating the platform's access controls, which
this project does not do. They need the save-and-upload route above.

## What was added in this upgrade

| Feature | State |
|---|---|
| PDF box: selected count, reorder, remove | Working, tested |
| Paper: A4 / Letter / image-size pages | Working, tested |
| Orientation: match picture / portrait / landscape | Working, tested |
| Fit: fit-to-page / actual size (never enlarged) / fill | Working, tested |
| Lossless embedding, transparency kept via `/SMask` | Working, tested (needs `CompressionStream`) |
| Generation progress and errors, no silent omission | Working, tested |
| Capture a frame at `H:MM.mmm`, with preview | Working, tested |
| Replace one picture, keeping the others, with undo | Working, tested |
| Source resolution shown per picture, with a low-resolution warning | Working, tested |
| Link recognition for YouTube / Instagram / Facebook, with honest limits | Working, tested |
| **Remote fetching of photo posts and carousels** | **Not implemented** — see above |
| **Fetching anything from a link inside the static page** | **Not possible** — browser security |

### The two limits worth knowing about the PDF

* **JPEG (the default) is re-encoded once**, when the frame is captured from the canvas.
  That is unavoidable: a `<video>` frame reaches JavaScript only through a canvas, and
  canvas output is JPEG or PNG. What is *not* done is a second re-encode — those bytes
  go into the PDF as they are (`/DCTDecode`), so the file holds exactly what the video
  gave, at the video's own pixel size (capped at 2200 px wide to keep a phone alive).
* **Transparency only survives the lossless setting.** JPEG has no alpha channel. If you
  need a transparent image to stay transparent, choose *Lossless* in the PDF box.

## Commands

```bash
# the tests (no browser needed - engine, sample video, PDF writer, links, timecodes)
python3 engine/tests/make_fixtures.py     # once: the ground-truth videos
python3 docs/test/make_media.py           # once: the test videos
node docs/test/run.mjs                    # 170 checks

# the app in a real browser (needs a Chrome/Chromium; skips loudly without one)
PAGESNAP_CHROME=/path/to/chrome node docs/test/browser.mjs    # 143 checks

# the two builds, and the build's own guards
python3 docs/build_single.py              # -> docs/pagesnap-offline.html
python3 docs/build_site.py --base-path /demo/   # -> _site/
python3 docs/test/test_build_guards.py    # 20 checks: broken paths must be refused

# the published site, served the way GitHub Pages serves it, in a real browser
python3 docs/serve.py --root _site --prefix /demo/   # http://localhost:5173/demo/
node docs/test/site.mjs                   # 40 checks, both layouts

# the server prototype, for link imports (needs Python + the requirements)
python3 prototype/server.py               # http://localhost:4567
```

## Deploying without touching `main` directly

The GitHub Pages site is `main` + `/docs`, built by `.github/workflows/pages.yml`. Work
on a branch and open a pull request:

```bash
git switch -c my-change
# ... edit, then:
node docs/test/run.mjs && python3 docs/build_single.py && python3 docs/build_site.py --base-path /demo/
git commit -am "what changed and why"
git push -u origin my-change
gh pr create --base main --head my-change --title "…"
gh pr merge <number> --merge        # merge only once the workflow is green
```

The workflow runs the whole battery (fixtures, videos, `run.mjs`, `browser.mjs`, the
single-file build) before it builds and publishes, so a red run never reaches the site.
