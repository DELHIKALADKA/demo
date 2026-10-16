# Deploying PageSnap to GitHub Pages

**Live URL:** <https://delhikaladka.github.io/demo/>

## What is deployed, and what cannot be

The repository holds two applications. Only one can run on GitHub Pages.

| | runs on Pages? | why |
|---|---|---|
| **`docs/` — the web app** | **yes** | It is a single static page. Every step (reading the video, finding the pages, writing the PDF) happens in the visitor's own browser with the page-detection engine ported to JavaScript. No backend is involved, so there is nothing for a static host to fail to run. |
| `prototype/` — the FastAPI app | **no** | It needs a Python server: yt-dlp for link imports, ffmpeg on the host, jobs in memory. GitHub Pages serves files only. It stays in the repository for running locally (`python prototype/server.py`). |

So the deployed site **cannot import a link** (Instagram, Facebook, YouTube) — not
because of a missing setting, but because a web page is not allowed to download video
from those sites, and no website can work around that. The app says so in the UI and
asks for a video file instead. **The phone app** (`android/`) and the **local
prototype** are the two places where link import works, because both have a real
downloader behind them.

## Which branch, and why

`main` holds everything. The application used to live only on a working branch; it is
now merged into `main`, and `main` is what Pages builds and serves.

## The one repository setting

Pages is currently configured as **"Deploy from a branch"**, which publishes the
repository root — and the repository root has no `index.html`, so the URL serves the
README. GitHub will not let a workflow change this setting; it needs repository admin
rights.

**Do this once:**

> Repository → **Settings** → **Pages** → **Build and deployment** → **Source** →
> choose **GitHub Actions**.

Nothing else in that panel needs changing. After that, every push to `main` redeploys
automatically, and the "Run workflow" button in the Actions tab deploys on demand.

Until that setting changes, the workflow's *build* job still runs the tests and builds
the site, and the *deploy* job fails on purpose with a message naming this exact
setting (rather than a cryptic 404).

### If you cannot switch the source to GitHub Actions

There is a second way, which needs no workflow at all: **Settings → Pages → Build and
deployment → Source: Deploy from a branch → Branch: `main`, folder: `/docs` → Save.**
Pages then serves the `docs/` folder as it is. The app itself is written with relative
paths, so it works either way — but be aware of what this costs:

* **the tests do not run** before publishing, so a broken change goes live;
* the published folder includes the test suite, the audit notes and the build scripts
  (`docs/test/`, `docs/AUDIT.md`, `docs/build_*.py`). Harmless, but public;
* the workflow's deploy job will keep failing (a red ✗ in the Actions tab) because it
  is set up for Actions-based deployment. Either ignore it, or switch the source to
  GitHub Actions when you can.

Prefer the Actions source: it tests the app, builds the single-file copy, and publishes
only the files a visitor needs.

## How the deployment works

`.github/workflows/pages.yml`, triggered by pushes to `main` and by
`workflow_dispatch`:

1. **Check out** the repository, set up Python 3.12 and Node 22, both with caching.
2. **Read the Pages configuration** (`actions/configure-pages`), so the build knows the
   real base path (`/demo/`) instead of assuming it.
3. **Build the ground-truth fixtures** (`engine/tests/make_fixtures.py`) — five videos
   whose page positions are known — and the test videos
   (`docs/test/make_media.py`) for every container the app makes a claim about.
4. **Run the engine tests** (`node docs/test/run.mjs`): the same JavaScript detector the
   browser app uses, against those fixtures at three sensitivities, plus the sample
   video the page offers, the PDF page order and the filename rules.
5. **Run the app in a browser** (`node docs/test/browser.mjs`): decoding, the full-size
   view, Start over, and the PDF that really downloads. It uses the Chrome the runner
   already has and skips itself, loudly, if there is none.
6. **Build the single-file copy** (`python docs/build_single.py`), which also fails if
   that build loses an element the app needs.
7. **Build the site** (`python docs/build_site.py`) into `_site/`, passing the base path
   reported by Pages.
8. **Upload** `_site/` as the Pages artifact (`actions/upload-pages-artifact`).
9. **Deploy** it (`actions/deploy-pages`), in the `github-pages` environment.

Permissions are `contents: read`, `pages: write`, `id-token: write` — nothing more. One
deployment runs at a time (`concurrency: pages`); an in-progress release is never
cancelled half-way.

## What the build produces

`docs/build_site.py` copies the runtime files, regenerates the single-file build, adds
the hosting extras, then **validates its own output** and fails if anything is wrong:

```
index.html              the app
pagesnap-core.js        the detection engine, in JavaScript
pagenotes-pdf.js        the PDF writer
pagesnap-offline.html   regenerated: the whole app in one file, for offline use
sample/lecture.mp4      33 s sample video, the "try it" button
favicon.svg             tab icon (any size, vector)
icon-192.png            192 px icon, from the same drawing
icon-512.png            512 px icon, for an installed app
apple-touch-icon.png    Home Screen icon on iOS
manifest.webmanifest    name, icon and colours for "Add to Home Screen"
SHARING.md              note for whoever the link is sent to
.nojekyll                tells Pages not to run Jekyll
404.html                sends stray paths back to the app
```

Development files (`serve.py`, `test/`, `build_single.py`, `build_site.py`,
`package.json`) are **not** published.

Checks run on every build, and the build **stops** if any of them fails:

- **no root-absolute asset paths.** The site lives at `/demo/`, not at the domain root,
  so every reference must be relative. `<script src="/pagesnap-core.js">`, `from '/x.js'`,
  `fetch('/x')` and `url(/x)` are all caught, because they work on localhost and 404 on
  Pages — the single most likely way to break only production.
- **every asset the page asks for exists in the output** — a missing file would
  otherwise surface as a 404 in a student's browser.
- **every module the page imports was published.** Adding a file to `docs/` and
  forgetting the build's list ships a page that cannot load; this is caught here.
- **nothing calls a server that is not there** (`/api/`, an absolute `fetch()`, or a
  localhost URL).
- **the manifest is valid JSON with relative paths and icons that exist.**

`docs/test/test_build_guards.py` breaks the output on purpose in each of those ways and
insists the build refuses it, so the checks cannot quietly stop working:

```bash
python3 docs/test/test_build_guards.py     # 20 checks
```

## Deploying a change

```bash
git push origin main        # the workflow runs, tests pass, the site updates
```

To deploy without a new commit: Actions → "Deploy PageSnap to GitHub Pages" →
**Run workflow**.

## The two environments, and why they are not the same

| | where | served at |
|---|---|---|
| development | the files you are editing in `docs/` | `http://localhost:5173/` |
| production | the built `_site/`, uploaded by the workflow | `https://delhikaladka.github.io/demo/` |

The difference that matters is the **path**: the site is at the root during development
and under `/demo/` in production. A reference that starts with `/` resolves against the
domain root, so it works perfectly on your machine and 404s for every visitor. Every
reference in this project is therefore relative (`./pagesnap-core.js`), the build refuses
to publish anything else, and the checks below prove it from the published folder.

## Building and checking locally

```bash
python3 docs/build_site.py                                  # -> _site/  (Windows: py)
python3 docs/serve.py --root _site --prefix /demo/           # exactly the production layout
# open http://localhost:5173/demo/
```

`--prefix` is the part that matters: without it you are testing a site served at the
root, which is not what visitors get. With it, a path that would break on Pages breaks
here too — try `/pagesnap-core.js` against that server and you will see the 404.

The whole published site is then checked in a real browser, from the built folder, under
the base path it will be served at:

```bash
node docs/test/site.mjs                 # builds _site/, then 40 checks in Chromium
node docs/test/site.mjs --base /demo/   # ...or any other base path
node docs/test/site.mjs --no-build      # check the _site/ that is already there
```

It fails on any request outside the base path, any response that is not 200, any
JavaScript error, and it runs the app end to end from there — sample video, PDF box,
download — then does the same at the domain root, because the build claims to work in
both places.

Run the app's own tests (no server needed):

```bash
python3 engine/tests/make_fixtures.py   # the five ground-truth videos  (Windows: py)
node docs/test/run.mjs                  # 170 checks expected
node docs/test/browser.mjs              # 143 checks, needs a browser
python3 docs/test/test_build_guards.py  # the build's own guards
```

On Windows use `py` rather than `python3` — see [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

Run the local server prototype (links, more options):

```bash
pip install -r requirements.txt
python prototype/server.py              # http://localhost:4567
```

## On an iPhone

Open <https://delhikaladka.github.io/demo/> in Safari:

- **Try it with a sample lecture video** works with no file of your own.
- **Choose a video** opens the Files app (Photos and screen recordings included).
- The page is dark, uses the whole screen, and its buttons are sized for a thumb.
- Share → **Add to Home Screen** gives it the PageSnap icon and its own window.

Everything runs on the phone: Safari reads the video, the detector finds the pages, and
the PDF is built there. With a long lecture the phone will work harder and take longer
than a laptop — that is the phone's own speed, not the website's.

## Secrets

There are none. The site is static, calls no API, and contains no keys, tokens or
credentials; the workflow uses only the built-in `GITHUB_TOKEN`.
