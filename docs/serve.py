"""Serve the PageSnap web app — in development, and in production shape.

    python3 docs/serve.py --port 5173                     # the app you are editing
    python3 docs/serve.py --root _site --prefix /demo/    # the built site, as Pages serves it

A static server is all this app needs: no backend, no database, no upload
endpoint. The page-finding and the PDF are done in the visitor's browser.

Two details that are easy to get wrong:

* **Byte ranges.** The app finds page changes by seeking to exact timestamps, and
  a browser can only seek in a video if the server answers `Range` requests. Plain
  `SimpleHTTPRequestHandler` does not, which makes seeking fall back to downloading
  everything first — fine for a 200 KB sample, painful for a real lecture.
* **No caching.** Everything runs client-side, so a stale cached file just looks
  like a bug.

`--root` and `--prefix` exist so the production layout can be reproduced on this
machine before anything is published. GitHub Pages serves this project under a
subpath (`https://<user>.github.io/demo/`), which is a different situation from
`http://localhost:5173/`, and the difference is exactly the kind of thing that is
never noticed until it is live. So:

    python3 docs/serve.py --root _site --prefix /demo/     # -> http://localhost:5173/demo/

serves the built folder the way Pages does: the site lives under the prefix, a
directory serves its `index.html`, a missing path returns `404.html` if the build
has one, and directories are never listed. Anything outside the prefix is a 404,
which is what catches a root-absolute path such as `/pagesnap-core.js` — it would
work at the domain root and fail at `/<repo>/`.

(The Python prototype in ../prototype is the version WITH a server, because
downloading from a link needs one. This one deliberately has none.)
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import os
import re
import socketserver
from typing import Optional

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(http.server.SimpleHTTPRequestHandler):
    #: The path the site is served under: "/" for development, "/demo/" for this
    #: project on GitHub Pages. Set from the command line; always has a trailing slash.
    prefix = "/"

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".json": "application/json",
        ".webmanifest": "application/manifest+json",
    }
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, prefix: str = "/", **kwargs) -> None:
        self.prefix = prefix if prefix.endswith("/") else prefix + "/"
        super().__init__(*args, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Only shout about problems; a working page should be quiet.
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    # -- where the site lives ---------------------------------------------- #

    def translate_path(self, path: str):
        """Strip the prefix, so a subpath deployment behaves like the real one.

        Anything outside the prefix resolves to a name that cannot exist, which
        turns a root-absolute reference such as `/pagesnap-core.js` into a 404
        here — exactly as it would be on GitHub Pages.
        """
        if self.prefix != "/":
            clean = path.split("?", 1)[0].split("#", 1)[0]
            if not clean.startswith(self.prefix):
                return os.path.join(self.directory, ".outside-the-prefix")
            path = "/" + clean[len(self.prefix):]
        return super().translate_path(path)

    def serve_not_found(self):
        """Answer the way GitHub Pages does: the build's own 404.html, status 404."""
        page = os.path.join(self.directory, "404.html")
        if os.path.exists(page):
            body = open(page, "rb").read()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)
        self.send_error(404, "File not found")
        return None

    # -- the part that makes seeking work ---------------------------------- #

    def send_head(self):
        clean = self.path.split("?", 1)[0]
        if self.prefix != "/":
            # "/demo" -> "/demo/" (and only that: "/demo/" itself is the site), which
            # is what Pages does and what makes relative links inside the page resolve.
            if clean == self.prefix.rstrip("/"):
                self.send_response(301)
                self.send_header("Location", self.prefix)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            # A path outside the prefix is not part of this site at all. GitHub Pages
            # would answer with its 404 page, and so does this.
            if not clean.startswith(self.prefix):
                return self.serve_not_found()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not clean.endswith("/"):
                self.send_response(301)
                self.send_header("Location", clean + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            index = os.path.join(path, "index.html")
            if not os.path.exists(index):
                # No directory listings: GitHub Pages never lists a folder either.
                return self.serve_not_found()
            path = index

        try:
            file = open(path, "rb")
        except OSError:
            return self.serve_not_found()

        try:
            size = os.fstat(file.fileno()).st_size
            content_type = self.guess_type(path)
            start, end = self._parse_range(size)

            if start is None:                      # whole file
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return file

            length = end - start + 1
            self.send_response(206)                # partial content: seeking works
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            file.seek(start)
            self._remaining = length
            return file
        except Exception:
            file.close()
            raise

    def _parse_range(self, size: int) -> tuple[Optional[int], int]:
        header = self.headers.get("Range")
        if not header:
            return None, 0
        match = RANGE_RE.match(header.strip())
        if not match:
            return None, 0
        raw_start, raw_end = match.groups()
        if raw_start == "" and raw_end == "":       # "bytes=-" -> nonsense, send it all
            return None, 0
        if raw_start == "":                          # suffix range: last N bytes
            length = int(raw_end)
            return max(0, size - length), size - 1
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        if start >= size or start > end:
            self.send_error(416, "Requested range not satisfiable")
            raise ValueError("bad range")
        return start, min(end, size - 1)

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        self._remaining = None
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Serve the PageSnap web app")
    ap.add_argument("--port", type=int, default=5173)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--root", default=here,
                    help="folder to serve (default: this docs/ folder, the app you edit). "
                         "Use --root _site for the built site.")
    ap.add_argument("--prefix", default="/",
                    help="path the site is served under (default /). Use /demo/ to "
                         "reproduce GitHub Pages for this repository.")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"{args.root} is not a folder - build it first "
                         f"(python3 docs/build_site.py), or drop --root to serve docs/")
    prefix = args.prefix if args.prefix.startswith("/") else "/" + args.prefix
    if not prefix.endswith("/"):
        prefix += "/"

    handler = functools.partial(Handler, directory=root, prefix=prefix)
    url = f"http://{args.host}:{args.port}{prefix}"
    with Server((args.host, args.port), handler) as httpd:
        print(f"PageSnap serving {os.path.relpath(root)}/ on {url}  (Ctrl+C to stop)")
        if prefix != "/":
            print(f"  the same layout GitHub Pages uses - open {url} and not the bare port")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
