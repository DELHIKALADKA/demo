# PageSnap

**A video goes in, a PDF of its pages comes out.** One screenshot per page of
notes — never one per frame, never mid-stroke, never mid-fade. Built for students
who are sent a reel or a lecture video and told "notes are in the video".

Everything happens on the device: **no cloud, no account, no database, no
operator.**

```
   a link (Instagram / Facebook / YouTube)   or   a video from the phone
                          ↓
        the video is downloaded / opened on the device
                          ↓
   the app watches it at 3 frames a second and finds the moments
   where the page CHANGES and has FINISHED being written
                          ↓
   full-resolution screenshots, one per page  →  tick / untick, rename
                          ↓
                     a PDF you can keep
```

## What is in this repository

| folder | what it is | state |
|---|---|---|
| `engine/` | the detection engine + PDF writer + CLI, in Python | **tested**, 15/15 fixture combinations pass |
| `prototype/` | a *server-backed* browser UI over that engine — the whole flow, including link downloads | working |
| `docs/` | the **shareable web app**: no server at all, runs on the visitor's device | live if Pages is on |
| `android/` | the phone app (Kotlin, Jetpack Compose) | sources kept; not being built |

### Try it in one minute

```bash
cd engine
pip install numpy pillow reportlab imageio-ffmpeg
python -m pagesnap.cli tests/fixtures/lecture.mp4 -o notes.pdf --explain
```

```bash
pip install -r requirements.txt       # everything, including the engine's needs
python prototype/server.py            # then open http://localhost:4567
```

Needs Python 3.9 or newer and nothing else — no compiler, and no ffmpeg to install,
because `imageio-ffmpeg` brings its own. It works from any directory, and on any
port: `--port 4570` if 4567 is busy.

The prototype is a phone-shaped app: **Home · Create · Library · Settings** with a
bottom navigation bar, dark by default and a light theme in Settings. The workflow is
**paste a link *or* choose a video → detect → review → reorder / remove / add a page by
timestamp → Create PDF → preview → download → edit and regenerate.** Nothing needs
re-uploading to regenerate, and every PDF you make is kept in `~/PageSnap` so the
Library can hand it back later.

The interface was redesigned in one pass without touching the detection engine or the
processing logic; see [`prototype/REDESIGN.md`](prototype/REDESIGN.md) for what
changed, what was tested, and what is still imperfect.

#### What it will and will not fetch

| | |
|---|---|
| **Always works** | a video file on your own device |
| **Works** | a direct link to a video file (`https://…/lecture.mp4`), and any site yt-dlp can read without a login |
| **Usually needs a login** | Instagram and Facebook posts. It says so plainly and points you at the file upload — it will not sign in as you, and it will not touch DRM, paywalls or live streams |
| **Refused on purpose** | anything that is not a video. FFmpeg will "play" a plain text file as a 640×400 `tty` stream, so the check is on the *container*, not on whether pixels came out |

Every refusal states the real reason and offers the upload fallback. A link that
cannot be reached fails within seconds with a connection error instead of hanging.

#### Regenerating

Editing the selection (remove, restore, reorder, capture) marks the existing PDF
**stale**: the panel says why, the download button is disabled, and the single
button becomes *Regenerate PDF*. The served file is built from the current
selection and order, and is re-read and verified before it is offered.

```bash
cd prototype
npm install jsdom          # once, for the browser-layer test
npm test                   # the API suite, then the browser-layer suite
python tests/test_prototype.py    # 82 checks against a real running server
node tests/ui_test.mjs            # 50 checks driving the real page in a DOM
```

The API suite starts the real server, reads the real PDF bytes with `pypdf` and
compares each embedded page against the page image it came from. The browser suite
loads the actual page and clicks through the whole workflow, asserting there are no
console errors and no unexpected failed requests.

### Host it as a public link

`docs/` is a complete, self-contained web app: no backend, no database, no build
step. Any static host will serve it — GitHub Pages (Settings → Pages → branch +
`/docs`), Netlify Drop, Cloudflare Pages, or a college server. Because the page
does all the work locally, hosting costs nothing and scales to any number of
students.

```bash
python3 docs/serve.py --port 5173         # run it locally
node docs/test/run.mjs                    # the browser engine vs the same fixtures
python3 docs/build_single.py              # -> docs/pagesnap-offline.html (one file, 40 KB)
```

`pagesnap-offline.html` is the whole app in a single file: no imports, no network,
no install. Save it, mail it, or drop it in a class group — a student can open it
on a phone with no internet and still get their notes out of a video.

## Why "find the page changes" is harder than it sounds

Off-the-shelf scene detection looks for hard cuts. A lecture video has three kinds
of change, and only the first one is handled by that:

1. **Hard cut** — the slide jumps to the next slide → you want a page.
2. **Transition** — a fade or wipe lasting half a second → you want **one** page,
   captured *after* the fade finishes, never in the middle of it.
3. **Progressive change** — the teacher keeps writing on the *same* page for
   twenty seconds → you want **one** page, captured when the writing stops.

So a page is not "a moment where pixels changed a lot". A page is:

> **a stretch of video that held still long enough to be read.**

Everything follows from that: look for transitions, then take the picture at the
moment the frame *settles*. Writing never settles, so it never fires the trigger
by itself. A fade only settles once it is over, so you get the finished slide.

The five rules (implemented identically in Python and Kotlin):

| rule | what it does |
|---|---|
| **R1 cut** | a sudden, large change → a new page just appeared |
| **R2 drift** | the picture drifted far from the last page, is still for a longer window, **and has stopped gaining ink** → a page was being filled in and is finished |
| **R3 settle** | whichever fired, capture at the first moment the picture holds still |
| **R4 quality** | inside that still stretch, keep the *sharpest* sample, at full resolution |
| **R5 fold** | a capture that a later one merely *extends*, with nothing sudden in between, was the same page half-finished → keep the fuller one |

Rule R2's "stopped gaining ink" is the one that took real work: writing and a
wobbling hand-held camera look identical sample by sample (0.007–0.024 versus
0.009–0.033 per step), but a page being written on only ever *gains* ink
(+0.24 … +1.5 per window) while wobble cancels out (≈ +0.13) and a finished page
gains exactly 0.000. All of the numbers are measured, not guessed; see
[`engine/tests/README.md`](engine/tests/README.md).

## Testing

```bash
cd engine
python tests/make_fixtures.py     # five videos with KNOWN ground truth
python tests/test_detect.py -v    # 5 fixtures x 3 sensitivities
```

The fixtures include a page written live for nine seconds, a writing pause that
must not become its own page, a slow scroll that must not become twelve pages, and
a phone-filmed-screen copy of the lecture with wobble, blur and exposure flicker.
A capture passes only if it lands in that page's *acceptable window* — from "fully
readable" until "leaves the screen".

## Privacy, by construction

There is no server to send anything to. The engine opens a local file and writes a
local PDF; the prototype runs on your own computer; the phone app keeps notes in
`filesDir/notes/` and writes the PDF to `Downloads/PageSnap`. Nothing is uploaded,
nothing is logged, and deleting a note deletes the only copy.
