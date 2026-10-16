#!/usr/bin/env python3
"""Prove that the production build refuses to ship a broken path.

`build_site.py` checks the built folder before it is published - no root-absolute
paths, every referenced asset present, a valid manifest with relative paths. That
check is the only thing standing between a working localhost and a live site full
of 404s, so this file tests the check itself: it breaks the output on purpose, in
each of the ways that actually happen, and insists that the build stops.

    python3 docs/test/test_build_guards.py

A guard that cannot fail is not a guard, and a guard that is never run is not a
guard either.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

DOCS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(DOCS)
sys.path.insert(0, DOCS)

import build_site  # noqa: E402  (after the path is set up)

BASE = "/demo/"
passed = 0
failed = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f"\n         {detail}" if detail else ""))


def build_into(target: str) -> None:
    result = subprocess.run(
        [sys.executable, os.path.join(DOCS, "build_site.py"), "--out", target,
         "--base-path", BASE],
        cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit("the clean build failed, so the guards cannot be tested")


def validate(target: str) -> tuple[bool, str]:
    """Run the build's own validation, and report whether it stopped the build."""
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            build_site.validate(target, BASE)
        return True, buffer.getvalue()
    except SystemExit as stop:
        return stop.code == 0, buffer.getvalue()


def tamper(source: str, replace: tuple[str, str], where: str | None = None) -> str:
    """Copy the clean build, break exactly one thing in it, and return the copy."""
    target = tempfile.mkdtemp(prefix="pagesnap-broken-")
    shutil.copytree(source, target, dirs_exist_ok=True)
    path = os.path.join(target, where or "index.html")
    body = open(path, encoding="utf-8").read()
    before, after = replace
    if before not in body:
        raise SystemExit(f"cannot tamper: {before!r} is not in {path}")
    open(path, "w", encoding="utf-8").write(body.replace(before, after, 1))
    return target


def expect_refused(source: str, replace: tuple[str, str], label: str,
                   needle: str = "", where: str | None = None) -> None:
    target = tamper(source, replace, where)
    try:
        allowed, output = validate(target)
        check(not allowed, label,
              "the build accepted it" if allowed else "")
        if needle and not allowed:
            check(needle.lower() in output.lower(), f"  and said why ({needle})",
                  output.strip()[-300:])
    finally:
        shutil.rmtree(target, ignore_errors=True)


def main() -> int:
    print("\n  === the clean build passes its own checks ===")
    site = tempfile.mkdtemp(prefix="pagesnap-site-")
    try:
        build_into(site)
        allowed, output = validate(site)
        check(allowed, "the production build passes validation", output.strip()[-300:])
        check(os.path.exists(os.path.join(site, "manifest.webmanifest")),
              "the manifest is published")
        check(os.path.exists(os.path.join(site, "icon-512.png")),
              "the icons are published")

        print("\n  === the guards that catch a broken path ===")
        expect_refused(site, ('<script type="module">',
                              '<script src="/pagesnap-core.js"></script>\n'
                              '<script type="module">'),
                       "a root-absolute script is refused", "domain root")
        expect_refused(site, ('<link rel="icon" href="./favicon.svg"',
                              '<link rel="icon" href="/favicon.svg"'),
                       "a root-absolute icon is refused", "domain root")
        expect_refused(site, ("from './pagesnap-core.js'", "from '/pagesnap-core.js'"),
                       "a root-absolute module import is refused", "module import")
        expect_refused(site, ('href="./apple-touch-icon.png"',
                              'href="./not-in-the-build.png"'),
                       "a referenced file that is not in the build is refused",
                       "not in the build")
        expect_refused(site, ("from './pagesnap-media.js'", "from './missing-module.js'"),
                       "an import of a module that was not published is refused",
                       "imports files that are not in the build")

        print("\n  === the guards that catch a broken manifest ===")
        expect_refused(site, ('"start_url": "./"', '"start_url": "/demo/"'),
                       "an absolute start_url is refused", "not a relative path",
                       where="manifest.webmanifest")
        expect_refused(site, ('"src": "./icon-192.png"',
                              '"src": "https://example.com/icon.png"'),
                       "a remote icon is refused", "not a relative path",
                       where="manifest.webmanifest")
        expect_refused(site, ('"src": "./icon-192.png"', '"src": "./icon-1024.png"'),
                       "a manifest icon that is missing is refused", "not in the build",
                       where="manifest.webmanifest")

        print("\n  === and a manifest that is not JSON at all ===")
        broken = tamper(site, ('{', 'not json at all'), where="manifest.webmanifest")
        try:
            allowed, output = validate(broken)
            check(not allowed, "unparseable manifest JSON is refused",
                  "the build accepted it" if allowed else "")
        finally:
            shutil.rmtree(broken, ignore_errors=True)
    finally:
        shutil.rmtree(site, ignore_errors=True)

    print(f"\n  {passed} checks, {failed} failures.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
