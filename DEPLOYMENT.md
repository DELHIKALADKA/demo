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

## How the deployment works

`.github/workflows/pages.yml`, triggered by pushes to `main` and by
`workflow_dispatch`:

1. **Check out** the repository, set up Python 3.12 with pip caching.
2. **Read the Pages configuration** (`actions/configure-pages`), so the build knows the
   real base path (`/demo/`) instead of assuming it.
3. **Build the ground-truth fixtures** (`engine/tests/make_fixtures.py`) — five videos
   whose page positions are known.
4. **Run the engine tests** (`node docs/test/run.mjs`): the same JavaScript detector the
   browser app uses, against those fixtures, at three sensitivities. 15/15 must pass.
5. **Build the site** (`python docs/build_site.py`) into `_site/`, passing the base path
   reported by Pages.
6. **Upload** `_site/` as the Pages artifact (`actions/upload-pages-artifact`).
7. **Deploy** it (`actions/deploy-pages`), in the `github-pages` environment.

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
favicon.svg             tab icon
apple-touch-icon.png    Home Screen icon on iOS
SHARING.md              note for whoever the link is sent to
.nojekyll                tells Pages not to run Jekyll
404.html                sends stray paths back to the app
```

Development files (`serve.py`, `test/`, `build_single.py`, `build_site.py`,
`package.json`) are **not** published.

Three checks run on every build:

- **no root-absolute asset paths.** The site lives at `/demo/`, not at the domain root,
  so every reference must be relative. This is the check that keeps it working from a
  subpath.
- **every asset the page asks for exists in the output** — a missing file would
  otherwise surface as a 404 in a student's browser.
- **nothing calls a server that is not there** (`/api/`, an absolute `fetch()`, or a
  localhost URL).

## Deploying a change

```bash
git push origin main        # the workflow runs, tests pass, the site updates
```

To deploy without a new commit: Actions → "Deploy PageSnap to GitHub Pages" →
**Run workflow**.

## Building and checking locally

```bash
python3 docs/build_site.py            # -> _site/
python3 -m http.server -d _site 8000  # serve it the way Pages would
# open http://localhost:8000/
```

Because the build uses relative paths, the same output works from a subpath as well:

```bash
python3 -m http.server -d . 8000      # then open http://localhost:8000/_site/
```

Run the app's own tests (no server needed):

```bash
python3 engine/tests/make_fixtures.py   # the five ground-truth videos
node docs/test/run.mjs                  # 15/15 expected
```

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
