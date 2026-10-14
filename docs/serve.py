"""Serve the PageSnap web app.

    python3 web/serve.py --port 5173

A static server is all this app needs: no backend, no database, no upload
endpoint. The page-finding and the PDF are done in the visitor's browser.

Two details that are easy to get wrong:

* **Byte ranges.** The app finds page changes by seeking to exact timestamps, and
  a browser can only seek in a video if the server answers `Range` requests. Plain
  `SimpleHTTPRequestHandler` does not, which makes seeking fall back to downloading
  everything first — fine for a 200 KB sample, painful for a real lecture.
* **No caching.** Everything runs client-side, so a stale cached file just looks
  like a bug.

(The Python prototype in ../prototype is the version WITH a server, because
downloading from a link needs one. This one deliberately has none.)
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import socketserver
from typing import Optional

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".json": "application/json",
    }
    protocol_version = "HTTP/1.1"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Only shout about problems; a working page should be quiet.
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    # -- the part that makes seeking work ---------------------------------- #

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            file = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

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
    args = ap.parse_args()

    os.chdir(here)
    with Server((args.host, args.port), Handler) as httpd:
        print(f"PageSnap web app on http://{args.host}:{args.port}/  (Ctrl+C to stop)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
