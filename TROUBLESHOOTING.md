# If PageSnap is not running on localhost:4567

The most common cause by far is a missing Python package. PageSnap now checks for that
itself and prints exactly what to do, so **start by reading what the terminal says** —
every message below is designed to tell you the fix.

## The two-minute version

```bash
# 1. from the folder that contains requirements.txt (the PageSnap folder)
python -m pip install -r requirements.txt

# 2. start it
python prototype/server.py
```

It should print:

```
PageSnap is running - open http://localhost:4567 in your browser
```

If it prints something else, find your message below.

## "PageSnap cannot start yet: some Python packages are missing"

Exactly what it says, and the message includes the command to run. Copy the whole
`"…python" -m pip install -r requirements.txt` line it prints — that command uses the
same Python that is trying to run PageSnap, which avoids installing into a different
Python than the one you are running.

Two things that trip people up here:

- **Run it from the PageSnap folder** — the one containing `requirements.txt`. If you
  are somewhere else, `pip` cannot find the file. Check with `dir` (Windows) or `ls`.
- **Use the same Python.** If `python` starts PageSnap but `pip` installs somewhere
  else, use `python -m pip install …` rather than bare `pip`.

## "python is not recognised" / "command not found"

Try these, in order, until one prints a version like `Python 3.12.3`:

```bash
python --version        # Windows and macOS often
python3 --version       # macOS and Linux usually
py --version            # the Windows launcher
```

Whichever works is the command to use everywhere below — for example
`py prototype\server.py` on Windows. If none of them work, install Python from
<https://www.python.org/downloads/> and **on Windows tick "Add Python to PATH" in the
installer**, then open a *new* terminal.

On Windows, if `python` opens the Microsoft Store instead of running anything, that is
the Store's placeholder. Use `py` instead, or turn the placeholder off in
Settings → Apps → Advanced app settings → App execution aliases.

## "PageSnap is ALREADY running on http://localhost:4567"

That is good news: it is running. **Open http://localhost:4567 in your browser.** The
server is not starting a second copy, because a second copy on the same port would
fail. If you genuinely want two at once, start the second with `--port 4568`.

## "Port 4567 is being used by something else… started on 4568 instead"

Something else on your computer has that port. PageSnap moved to the next free one and
printed the address — use *that* address, not 4567. To control it yourself:

```bash
python prototype/server.py --port 4580
```

## Nothing appears in the terminal at all

- **Did you paste a multi-line block at once?** Some terminals only run the first
  line. Run one line at a time.
- **Is the terminal still "busy"?** A running server shows no prompt — that is normal,
  and it means it *is* running. Open the browser; press Ctrl+C to stop it.
- **Did you open the file instead of running the server?** Opening
  `prototype/static/index.html` by double-clicking will show a page that cannot work:
  it needs the server behind it. PageSnap will tell you this in a red banner ("The
  PageSnap server is not reachable"). Start the server as above and use
  http://localhost:4567 instead.

## The server says it is running, but the browser does not load

- Check the address: **http://localhost:4567** — `https://` will not work, and the port
  must match what the terminal printed.
- Check you are on the same computer as the server.
- Check the terminal for a new error after you try to load the page.
- A firewall prompt on first run (macOS and Windows): allow it for private networks.
- If you are on a remote machine or inside a container, `localhost` means *that*
  machine. Forward the port (for example `ssh -L 4567:localhost:4567 user@host`) or run
  PageSnap on the computer you are browsing from.
- To reach it from a phone on the same Wi-Fi, PageSnap already listens on all
  interfaces; use `http://<this-computer's-IP>:4567` and allow it through the firewall.

## "Address already in use" from an old run

You probably have an earlier PageSnap still running in another terminal window. Either
close that window, or start this one on another port:

```bash
python prototype/server.py --port 4568
```

## Still stuck? Get the details PageSnap needs

Run these and keep the output — it says more than any guess:

```bash
python --version
python -m pip --version
python -c "import fastapi, uvicorn, numpy, PIL, reportlab, imageio_ffmpeg; print('all good')"
python prototype/server.py
```

The third line either prints `all good` (so the packages are fine and the problem is
the port, the browser, or the address) or shows which package is missing.

## What PageSnap needs, precisely

- **Python 3.9 or newer.** That is the only thing to install by hand.
- Seven packages, all listed in `requirements.txt`, all installed by the one command
  above. No compiler, no build tools.
- **No ffmpeg to install** — `imageio-ffmpeg` brings its own binary.
- **No Node, no database, no account.** Node is only needed to run the optional browser
  test suite.
- Optional extras: `yt-dlp` (needed to import links; local files work without it) and
  `pypdf` (re-reads the generated PDF to verify it).
