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

import base64
import os
import re

#: Any script block, classic or module - for looking at the markup alone.
SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.S)


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

    # Adjust the markup on its own first, so no pattern below can reach into the code.
    markup = html.replace(script.group(0), "<!--PAGESNAP-APP-->")

    # The offline file travels alone, so the sample video is not beside it. Replace
    # exactly the sample button's row - nothing after it.
    markup = replace_once(
        r'<div class="row" style="margin-top:12px">\s*<button class="btn ghost" id="sample">[^<]*</button>'
        r'\s*<span[^>]*>[^<]*</span>\s*</div>',
        '<div class="note">This is the offline copy: it works with no internet and no '
        'installation. Pick any video saved on this device and it will write the PDF here.</div>',
        markup, "the sample button's row",
    )
    # The hosted page warns people who open it from disk to use this file instead;
    # inside this file, which is meant to be opened from disk, that warning is wrong.
    markup = replace_once(r"<script>\s*if \(location\.protocol === 'file:'\).*?</script>\s*", "",
                          markup, "the open-from-disk warning script")
    markup = re.sub(r"<!-- If someone saves this page.*?-->\s*", "", markup, flags=re.S)
    # Nothing may point at a file beside it: carry the tab icon inside the file, and
    # drop the Home Screen icon (only the hosted page can be added to a Home Screen).
    icon = base64.b64encode(read("favicon.svg").encode("utf-8")).decode("ascii")
    markup = markup.replace('href="./favicon.svg"', f'href="data:image/svg+xml;base64,{icon}"')
    markup = re.sub(r"<!-- Icons:.*?-->\s*", "", markup, flags=re.S)
    markup = re.sub(r'<link rel="apple-touch-icon"[^>]*>\s*', "", markup)
    markup = markup.replace(
        "<title>PageSnap — turn a lecture video into notes PDF</title>",
        "<title>PageSnap — video to notes PDF (works offline)</title>",
    )
    # the module-level top-level awaits are not allowed in a classic script, so the
    # sample button's fetch stays inside an async function (it already is)
    return markup.replace("<!--PAGESNAP-APP-->", f"<script>\n{bundled}</script>")


def replace_once(pattern: str, replacement: str, text: str, what: str) -> str:
    """Replace one known piece of index.html, or stop: a silent miss ships a broken file."""
    result, count = re.subn(pattern, lambda _: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"build_single.py could not find {what} in index.html; "
                         "update its pattern to match the page")
    return result


def problems(html: str, index_html: str) -> list[str]:
    """Everything that would make the offline copy fail, in plain words (empty = fine)."""
    found = []
    code = html.split("<script>")[-1].split("</script>")[0]
    if re.search(r"^\s*import\s", code, re.M):
        found.append("an import statement survived")
    if 'type="module"' in html:
        found.append("it still declares a module script")
    for name in ("function buildPdf", "function detect", "function sampleVideo"):
        if name not in html:
            found.append(f"missing {name}")
    if "location.protocol === 'file:'" in html:
        found.append("it still carries the hosted page's 'open me from a web link' warning")

    markup = SCRIPT_BLOCK.sub("", html)
    ids = lambda text: set(re.findall(r'\bid="([^"]+)"', SCRIPT_BLOCK.sub("", text)))  # noqa: E731
    gone = ids(index_html) - ids(html) - {"sample"}
    if gone:
        found.append("elements the app needs are gone: #" + ", #".join(sorted(gone)))
    outside = [ref for ref in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', markup)
               if not ref.startswith(("data:", "blob:", "http:", "https:", "#", "mailto:"))]
    if outside:
        found.append("it points at files that do not travel with it: " + ", ".join(outside))
    return found


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    html = build(here)
    target = os.path.join(here, "pagesnap-offline.html")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(html)

    size_kb = os.path.getsize(target) / 1024
    print(f"wrote {os.path.relpath(target)}  ({size_kb:.0f} KB, single file)")
    with open(os.path.join(here, "index.html"), encoding="utf-8") as fh:
        found = problems(html, fh.read())
    if found:
        raise SystemExit("BUILD PROBLEM: " + "; ".join(found))
    print("checks passed: no imports, no modules, engine + pdf writer present, every element "
          "the app uses is there, and it points at no file beside it")


if __name__ == "__main__":
    main()
