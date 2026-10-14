#!/usr/bin/env python3
"""Assemble the deployable site into `_site/`, and check it before anyone sees it.

The app in `docs/` is already static, so there is no bundler to run - but "the folder
we develop in" and "the folder we publish" are not the same thing:

  * development files (this script, `serve.py`, the test runner, `package.json`) must
    not be published;
  * the single-file build (`pagesnap-offline.html`) must be regenerated from the current
    sources rather than trusted from git, so the published copy can never be stale;
  * every asset the page asks for has to actually exist in the output - a 404 only
    shows up when a student hits it, which is too late.

So this script copies the runtime files, regenerates the single-file build, writes the
small hosting extras, then validates the result and fails loudly if anything is wrong.

    python3 docs/build_site.py                 # -> _site/
    python3 docs/build_site.py --out /tmp/x    # somewhere else

Every path inside the app is relative (`./pagesnap-core.js`), which is what makes it
work under a project subpath such as https://<user>.github.io/demo/ - the validation
step fails the build if an absolute path ever creeps back in.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Published as-is, keeping their relative layout.
RUNTIME_FILES = [
    "index.html",
    "pagesnap-core.js",
    "pagenotes-pdf.js",
    "favicon.svg",
    "apple-touch-icon.png",
    "sample/lecture.mp4",
    "SHARING.md",
]
#: Built here rather than copied, so the published copy always matches the sources.
GENERATED = ["pagesnap-offline.html"]
#: Present in docs/ for development only - deliberately not published.
DEV_ONLY = ["serve.py", "build_single.py", "build_site.py", "package.json", "test"]

#: Referenced from HTML as attributes or from JavaScript as fetch/import targets.
REF = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']|fetch\(\s*['"]([^'"]+)['"]""")
ABSOLUTE = re.compile(r"""^(?:src|href)\s*=\s*["'](/[^/]|//)""")


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    log("")
    log(f"BUILD FAILED: {message}")
    raise SystemExit(1)


def copy_runtime(out_dir: str) -> list[str]:
    copied = []
    for relative in RUNTIME_FILES:
        source = os.path.join(HERE, relative)
        if not os.path.exists(source):
            fail(f"docs/{relative} is missing - the app needs it")
        target = os.path.join(out_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative)
    return copied


def build_single_file(out_dir: str) -> str:
    """Regenerate the one-file offline build with the project's own script."""
    sys.path.insert(0, HERE)
    try:
        import build_single                                   # type: ignore
    except ImportError as exc:                                # pragma: no cover
        fail(f"could not import build_single.py ({exc})")
    html = build_single.build(HERE)
    target = os.path.join(out_dir, "pagesnap-offline.html")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(html)
    # The whole point of that file is that it needs nothing else.
    body = html.split("<script>")[-1].split("</script>")[0]
    if re.search(r"^\s*import\s", body, re.M):
        fail("the single-file build still contains an import statement")
    if 'type="module"' in html:
        fail("the single-file build still declares a module script")
    return "pagesnap-offline.html"


def write_hosting_extras(out_dir: str, base_path: str) -> list[str]:
    """`.nojekyll`, plus a 404 that sends people to the app root.

    GitHub Pages answers unknown paths under the project with `/404.html` from the
    root of the published site. Relative links inside it would resolve against the
    path that was asked for, so this one uses the site's own base path.
    """
    written = []
    with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")                     # presence is the instruction
    written.append(".nojekyll")

    with open(os.path.join(out_dir, "404.html"), "w", encoding="utf-8") as fh:
        fh.write(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PageSnap — page not found</title>
<meta http-equiv="refresh" content="2; url={base_path}">
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;text-align:center;
       background:#0d1220;color:#eef1fa;
       font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
  a{{color:#8aa9f7}}
</style>
</head>
<body>
  <div>
    <h1 style="font-size:20px;margin:0 0 8px">That page is not here</h1>
    <p>Taking you to <a href="{base_path}">PageSnap</a>…</p>
  </div>
</body>
</html>
""")
    written.append("404.html")
    return written


def validate(out_dir: str) -> None:
    """Check the built site the way the browser will: does everything it asks for exist?"""
    index = os.path.join(out_dir, "index.html")
    if not os.path.exists(index):
        fail("_site/index.html is missing - GitHub Pages would serve a 404")

    html = open(index, encoding="utf-8").read()

    # 1. no root-absolute asset paths: the site lives at /<repo>/, not at the domain root
    for line_number, line in enumerate(html.splitlines(), 1):
        if ABSOLUTE.search(line) and "http" not in line:
            fail(f"absolute path on line {line_number} of index.html would break under a "
                 f"subpath: {line.strip()[:90]}")

    # 2. every asset the page asks for is really in the output
    wanted, missing = set(), []
    for match in REF.finditer(html):
        reference = (match.group(1) or match.group(2) or "").strip()
        if not reference or reference.startswith(("http", "//", "#", "data:", "mailto:")):
            continue
        if "$" in reference:
            continue        # a template placeholder such as src="${page.thumb}":
                            # the value is built at run time, so there is no file to find
        wanted.add(reference)
        path = reference.split("?")[0].split("#")[0]
        if path.startswith("./"):
            path = path[2:]
        if not os.path.exists(os.path.join(out_dir, path)):
            missing.append(reference)
    if missing:
        fail("index.html asks for files that are not in the build: "
             + ", ".join(sorted(missing)))

    # 3. nothing in the published page should call a server that is not there
    for pattern, why in [(r"fetch\(\s*['\"]/", "an absolute fetch() needs a server"),
                         (r"/api/", "an /api/ call needs a backend"),
                         (r"localhost|127\.0\.0\.1", "a localhost URL in a published page")]:
        if re.search(pattern, html):
            fail(f"the published page contains {why}")

    # 4. the modules are real ES modules and parse as such
    for name in ("pagesnap-core.js", "pagenotes-pdf.js"):
        source = open(os.path.join(out_dir, name), encoding="utf-8").read()
        if not re.search(r"^export\s", source, re.M) and "export {" not in source:
            fail(f"{name} exports nothing - index.html imports from it")

    log(f"  validated {len(wanted)} referenced asset(s); none missing")


def tree(out_dir: str) -> None:
    total = 0
    for folder, _, files in sorted(os.walk(out_dir)):
        for name in sorted(files):
            path = os.path.join(folder, name)
            size = os.path.getsize(path)
            total += size
            relative = os.path.relpath(path, out_dir)
            log(f"    {size / 1024:8.1f} KB  {relative}")
    log(f"    {'-' * 34}")
    log(f"    {total / 1024:8.1f} KB  total")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deployable PageSnap site")
    parser.add_argument("--out", default=os.path.join(ROOT, "_site"),
                        help="output directory (default: _site at the repository root)")
    parser.add_argument("--base-path", default="/demo/",
                        help="the path the site is published under (default /demo/)")
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    log(f"Building the PageSnap site from docs/ into {os.path.relpath(out_dir, ROOT)}/")

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    copied = copy_runtime(out_dir)
    log(f"  copied {len(copied)} runtime file(s)")
    built = build_single_file(out_dir)
    log(f"  rebuilt {built} from the current sources")
    extras = write_hosting_extras(out_dir, args.base_path)
    log(f"  wrote {', '.join(extras)}")
    log(f"  left out development files: {', '.join(DEV_ONLY)}")

    validate(out_dir)
    log("")
    log("Built site:")
    tree(out_dir)
    log("")
    log(f"Done. Publish {os.path.relpath(out_dir, ROOT)}/ under base path {args.base_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
