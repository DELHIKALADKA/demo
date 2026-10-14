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
| `prototype/` | a browser UI over that engine — the whole flow, usable right now | working |
| `android/` | the phone app (Kotlin, Jetpack Compose) | engine ported; needs a first compile |

### Try it in one minute

```bash
cd engine
pip install numpy pillow reportlab imageio-ffmpeg
python -m pagesnap.cli tests/fixtures/lecture.mp4 -o notes.pdf --explain
```

```bash
cd prototype
pip install fastapi "uvicorn[standard]" python-multipart yt-dlp
python server.py --port 8000          # then open http://localhost:8000
```

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
