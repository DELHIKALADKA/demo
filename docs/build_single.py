"""Pack the web app into ONE self-contained HTML file.

Why this exists: the app is three files (`index.html` + two ES modules), which is
right for hosting, but a browser refuses to load modules from a `file://` URL — so
double-clicking the HTML would fail, and you cannot put three files in a WhatsApp
message.

    python3 docs/build_single.py        ->  docs/pagesnap-offline.html

The result is one file with no imports, no network calls and no dependencies:
save it, mail it, drop it in a class group, or open it on a phone with no internet.
It is built from the same sources, so there is nothing to keep in step.
"""

from __future__ import annotations

import os
import re


def strip_module_syntax(source: str) -> str:
    """Turn an ES module into a plain script body (drop `export` keywords)."""
    source = re.sub(r"^export\s+(?=(const|let|var|function|class|async))", "", source, flags=re.M)
    source = re.sub(r"^export\s*\{[^}]*\}\s*;?\s*$", "", source, flags=re.M)
    return source


def build(directory: str) -> str:
    read = lambda name: open(os.path.join(directory, name), encoding="utf-8").read()  # noqa: E731

    html = read("index.html")
    core = strip_module_syntax(read("pagesnap-core.js"))
    pdf = strip_module_syntax(read("pagenotes-pdf.js"))
    media = strip_module_syntax(read("pagesnap-media.js"))

    script = re.search(r'<script type="module">(.*?)</script>', html, re.S)
    if not script:
        raise SystemExit("index.html has no <script type=\"module\"> block to replace")
    app = script.group(1)
    app = re.sub(r"^import\s+\{[^}]*\}\s+from\s+'[^']*';\s*$", "", app, flags=re.M)

    # Everything shares one scope, so no imports are needed at all.
    bundled = (
        "(() => {\n'use strict';\n\n"
        f"/* ---------- engine: finding the pages ---------- */\n{core}\n"
        f"/* ---------- writing the PDF ---------- */\n{pdf}\n"
        f"/* ---------- links and timecodes ---------- */\n{media}\n"
        f"/* ---------- the app ---------- */\n{app}\n"
        "})();\n"
    )

    out = html.replace(script.group(0), f"<script>\n{bundled}</script>")
    # The standalone offline file must not request a separate web manifest.
    out = re.sub(r'\s*<link\s+rel=["\']manifest["\'][^>]*>', '', out, flags=re.I)
    # The offline file travels alone, so the sample video is not beside it.
    #
    # This pattern has to stop at the end of the sample *row*. The earlier one ended at
    # the next `</div></div>`, which was several elements later: it deleted the error
    # box, both progress lines, the whole review card and the closing </section> of the
    # pick card. The single file then threw "Cannot set properties of null" on load and
    # did nothing at all when a video was chosen.
    replaced, count = re.subn(
        r'<div class="row" style="margin-top:12px">\s*'
        r'<button class="btn ghost" id="sample">.*?</button>\s*'
        r'<span class="hint"[^>]*>.*?</span>\s*</div>',
        '<div class="note">This is the offline copy: it works with no internet and no '
        'installation. Pick any video saved on this device and it will write the PDF here.</div>',
        out, flags=re.S,
    )
    if count != 1:
        raise SystemExit(f"BUILD PROBLEM: expected to replace the sample row once, did it {count} times")
    out = replaced
    out = out.replace(
        "<title>PageSnap — turn a lecture video into notes PDF</title>",
        "<title>PageSnap — video to notes PDF (works offline)</title>",
    )
    # the module-level top-level awaits are not allowed in a classic script, so the
    # sample button's fetch stays inside an async function (it already is)
    return out


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    html = build(here)
    target = os.path.join(here, "pagesnap-offline.html")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(html)

    size_kb = os.path.getsize(target) / 1024
    print(f"wrote {os.path.relpath(target)}  ({size_kb:.0f} KB, single file)")
    problems = []
    if "import " in html.split("<script>")[-1].split("</script>")[0]:
        problems.append("an import statement survived")
    if 'type="module"' in html:
        problems.append("still declares a module script")
    for name in ("function buildPdf", "function detect", "function sampleVideo",
                 "function classifyUrl", "function parseTimecode", "function encodeLossless"):
        if name not in html:
            problems.append(f"missing {name}")

    # Every element the app reaches for must still be here. Nothing caught this before:
    # the offline file was published missing ten of them, and it failed in the browser
    # rather than in the build.
    with open(os.path.join(here, "index.html"), encoding="utf-8") as fh:
        wanted = set(re.findall(r'id="([^"]+)"', fh.read()))
    have = set(re.findall(r'id="([^"]+)"', html))
    lost = sorted(wanted - have - {"sample"})       # the sample button is removed on purpose
    if lost:
        problems.append(f"the offline file lost these elements: {', '.join(lost)}")
    if "$(" not in html and "getElementById" not in html:
        problems.append("the app script is not in the file")
    if problems:
        raise SystemExit("BUILD PROBLEM: " + "; ".join(problems))
    print("checks passed: no imports, no modules, engine + pdf writer + media module present")


if __name__ == "__main__":
    main()
