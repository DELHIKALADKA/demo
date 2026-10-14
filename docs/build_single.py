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
        f"/* ---------- the app ---------- */\n{app}\n"
        "})();\n"
    )

    out = html.replace(script.group(0), f"<script>\n{bundled}</script>")

    # The offline file travels alone, so the sample video is not beside it.
    out = re.sub(
        r'<div class="row" style="margin-top:12px">\s*<button class="btn ghost" id="sample">.*?</div>\s*</div>',
        '<div class="note">This is the offline copy: it works with no internet and no '
        'installation. Pick any video saved on this device and it will write the PDF here.</div>',
        out, flags=re.S,
    )
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
    for name in ("function buildPdf", "function detect", "function sampleVideo"):
        if name not in html:
            problems.append(f"missing {name}")
    if problems:
        raise SystemExit("BUILD PROBLEM: " + "; ".join(problems))
    print("checks passed: no imports, no modules, engine + pdf writer present")


if __name__ == "__main__":
    main()
