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

Python is not on this computer's PATH, so the command does not exist yet. The command
name differs by system, so find yours first — try these until one prints a version like
`Python 3.12.3`:

```bash
python --version        # Windows and macOS often
python3 --version       # macOS and Linux usually
py --version            # the Windows launcher — try this first on Windows
```

Whichever works is the command to use everywhere below.

### On Windows: use `py`

`py` is the Windows Python launcher. It is on PATH whenever Python was installed
properly, so it is the most reliable name:

```bat
py --version
```

Then, from the PageSnap folder:

```bat
py -m pip install -r requirements.txt
py prototype\server.py
```

**Or just double-click `run-pagesnap.bat` in the PageSnap folder.** It does all three
steps for you: it finds Python (trying `py` first), installs the packages into a
private `.venv` folder so nothing is installed system-wide, starts the server in its
own window, and opens your browser at <http://localhost:4567>. Keep that
"PageSnap server" window open while you use PageSnap; closing it stops the server.

### If `py` is not found either, Python is not installed

1. Go to <https://www.python.org/downloads/> and download the Windows installer.
2. **In the installer, tick "Add python.exe to PATH"** at the bottom of the first
   screen. This is the step that is missed most often, and it is the reason `python`
   is not recognised.
3. Finish the install, then **open a brand-new terminal** (or double-click
   `run-pagesnap.bat` again). An already-open terminal keeps its old PATH and will
   still say "not recognised".

From PowerShell you can also install it in one line:

```powershell
winget install -e --id Python.Python.3.12
```

### If `python` opens the Microsoft Store

That is the Store's placeholder rather than Python, and it means Python is not
installed. Either use `py` (if that works, Python *is* installed and only the
placeholder is in the way) or turn the placeholder off in
**Settings → Apps → Advanced app settings → App execution aliases** and switch off the
`python.exe` entries there.

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
