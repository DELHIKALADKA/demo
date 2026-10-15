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

`main` holds everything, and `main` is what Pages serves: the web app lives in `docs/`,
the folder Pages publishes. Changes reach `main` through pull requests, and the checks
below run on the pull request first, so nothing broken is merged into the live folder.

## The repository setting: `main` + `/docs`

> Repository → **Settings** → **Pages** → **Build and deployment** →
> **Source: Deploy from a branch** → **Branch: `main`**, folder **`/docs`** → Save.

That is how this repository is set up. To confirm it from a terminal:

```bash
gh api repos/DELHIKALADKA/demo/pages --jq '.build_type, .source'
# legacy
# {"branch":"main","path":"/docs"}
```

Do **not** switch the source to "GitHub Actions": no workflow in this repository
deploys. Changing the setting needs repository admin rights; a workflow cannot do it.

## How the deployment works

There is no build step of ours: Pages publishes `docs/` as it is committed.

1. A change is merged (or pushed) to `main`.
2. GitHub's own **`pages-build-deployment`** workflow publishes the `docs/` folder. It
   shows up in the Actions tab by itself; it is not a file in this repository. Its
   default Jekyll pass copies the app's files unchanged (none of them has front
   matter) and also renders `SHARING.md` as `SHARING.html`.
3. A minute or two later, <https://delhikaladka.github.io/demo/> serves the new files.

Because whatever is committed goes live, the repository's own workflow,
`.github/workflows/pages.yml` ("Check the GitHub Pages site (main + /docs)"), tests
`docs/` on every pull request into `main` and on every push to `main`:

1. **Build the ground-truth fixtures** (`engine/tests/make_fixtures.py`) — five videos
   whose page positions are known.
2. **Run the engine tests** (`node docs/test/run.mjs`): the same JavaScript detector the
   browser app uses, against those fixtures, at three sensitivities. 15/15 must pass.
3. **Check `docs/` as Pages serves it** (`python docs/build_site.py --check`) — the
   checks listed under "What is published".
4. **Drive the real page in headless Chrome** (`node docs/test/browser.mjs`): `docs/`
   served under `/demo/`, the sample lecture run end to end (pages found, a page
   previewed at full size, one un-ticked, a real PDF downloaded and read back, the same
   video chosen twice, a non-video refused), then the offline copy opened from disk
   and run the same way.

Its only permission is `contents: read`. It does not deploy, so a red ✗ on it means
"this would publish something broken", not "the deployment is misconfigured".

## What is published

The whole `docs/` folder, as committed:

```
index.html              the app
pagesnap-core.js        the detection engine, in JavaScript
pagenotes-pdf.js        the PDF writer
pagesnap-offline.html   the whole app in one file, for offline use (generated, see below)
sample/lecture.mp4      33 s sample video, the "try it" button
favicon.svg             tab icon
apple-touch-icon.png    Home Screen icon on iOS
SHARING.md              note for whoever the link is sent to (also as SHARING.html)
```

The development files in `docs/` (`serve.py`, `test/`, `build_single.py`,
`build_site.py`, `package.json`) are served as well, because Pages publishes the folder
as it is. They hold no secrets and the app never loads them.

`pagesnap-offline.html` is generated from `index.html` and the two modules by
`docs/build_single.py`, and Pages serves the committed copy. **After changing
`index.html`, `pagesnap-core.js` or `pagenotes-pdf.js`, run
`python3 docs/build_single.py` and commit the regenerated file** — the check fails the
pull request if you forget.

`python docs/build_site.py --check` fails if any of these is wrong:

- **no root-absolute asset paths.** The site lives at `/demo/`, not at the domain root,
  so every reference must be relative. This is the check that keeps it working from a
  subpath.
- **every asset the page asks for exists** — a missing file would otherwise surface as
  a 404 in a student's browser.
- **nothing calls a server that is not there** (`/api/`, an absolute `fetch()`, or a
  localhost URL).
- **the committed `pagesnap-offline.html` matches the sources**, is one self-contained
  file (no imports, no module script, nothing beside it that it points at) and still
  has every element the app uses.

For any *other* static host, `python3 docs/build_site.py` assembles the same app into
`_site/` (without the development files, plus a `404.html`) and runs the same checks on
that. GitHub Pages does not use `_site/`.

## Deploying a change

1. Open a pull request into `main`; the check workflow runs on it. Wait for it to pass.
2. Merge it. Pages starts **pages-build-deployment** for the new commit on `main`.
3. Confirm that run succeeded — Actions → **pages-build-deployment**, or:

   ```bash
   gh run list --workflow pages-build-deployment --limit 1
   ```

   Only then is the change live; reload the site to see it.

To republish without a new commit: Actions → **pages-build-deployment** → the latest
run → **Re-run all jobs**.

## Building and checking locally

```bash
python3 docs/serve.py                  # docs/ at http://localhost:5173/  (Windows: py)
python3 docs/build_site.py --check     # the same checks a pull request gets
python3 docs/build_single.py           # after editing the app: regenerate the offline copy
```

Run the app's own tests (no server needed):

```bash
python3 engine/tests/make_fixtures.py   # the five ground-truth videos  (Windows: py)
node docs/test/run.mjs                  # 15/15 expected
node docs/test/browser.mjs              # the real page in headless Chrome (Node 22+)
```

`browser.mjs` looks for Chrome or Chromium on `PATH` (or set
`PAGESNAP_CHROME=/path/to/chrome`) and prints SKIPPED if there is none. CI sets
`PAGESNAP_REQUIRE_BROWSER=1`, so there it cannot skip.

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
credentials; the check workflow uses only the built-in `GITHUB_TOKEN`, read-only.
