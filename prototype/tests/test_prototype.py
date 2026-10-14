"""End-to-end tests for the PageSnap prototype.

These are not mocks. The suite starts the real server on a spare port, uploads a
real video, and checks the real bytes that come back:

* the detector still finds the same pages as before any of this was touched,
* the PDF is a genuine PDF, verified by `pypdf`, with the right page count,
* reordering really reorders, removing really removes, restoring really restores,
* a PDF generated before an edit is *refused* afterwards (no silent stale file),
* a hand-captured page lands in the PDF at the position the user put it,
* invalid and unsupported sources fail with a clear reason and never a fake success,
* the server never answers 500 and never logs a traceback.

    python3 prototype/tests/test_prototype.py

Exit code 0 = everything passed.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FIXTURES = os.path.join(ROOT, "engine", "tests", "fixtures")
ENGINE = os.path.join(ROOT, "engine")
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ROOT, "prototype"))

from PIL import Image                                   # noqa: E402
from pypdf import PdfReader                             # noqa: E402
import server as snapserver                             # noqa: E402  (the server itself,
#                                                         for the checks that need to call
#                                                         a real function with real input)

PASSED: List[str] = []
FAILED: List[str] = []
SERVER_LOG = None


# --------------------------------------------------------------------------- #
# test plumbing
# --------------------------------------------------------------------------- #

def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(condition)


def request(method: str, url: str, *, data: bytes = None, json_body: dict = None,
            headers: Dict[str, str] = None, timeout: float = 120.0
            ) -> Tuple[int, bytes, Dict[str, str]]:
    body = data
    head = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode()
        head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def header(headers: Dict[str, str], name: str, default: str = "") -> str:
    """HTTP header lookup, case-insensitively (servers may lowercase names)."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return default


def get_json(base: str, path: str) -> Dict[str, Any]:
    status, body, _ = request("GET", base + path)
    return json.loads(body.decode() or "{}") if body else {}


def multipart(fields: Dict[str, str], file_field: str, filename: str, content: bytes):
    boundary = "----pagesnap" + uuid.uuid4().hex
    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_video(base: str, path: str, **fields) -> Tuple[int, Dict[str, Any]]:
    with open(path, "rb") as fh:
        content = fh.read()
    body, content_type = multipart(fields, "video", os.path.basename(path), content)
    status, raw, _ = request("POST", base + "/api/upload", data=body,
                             headers={"Content-Type": content_type})
    return status, json.loads(raw.decode() or "{}")


def wait_ready(base: str, jid: str, timeout: float = 180.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    state: Dict[str, Any] = {}
    while time.time() < deadline:
        state = get_json(base, f"/api/jobs/{jid}")
        if state.get("status") in ("ready", "failed"):
            return state
        time.sleep(0.4)
    return state


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# PDF inspection
# --------------------------------------------------------------------------- #

def pdf_pages(data: bytes) -> List[Image.Image]:
    """Every embedded image in the PDF, in page order, at a comparable size."""
    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages:
        images = list(page.images)
        if not images:
            out.append(None)          # type: ignore[arg-type]
            continue
        img = images[0].image.convert("RGB").resize((64, 36), Image.LANCZOS)
        out.append(img)
    return out


def image_distance(a: Image.Image, b: Image.Image) -> float:
    if a is None or b is None:
        return 1.0
    pa, pb = a.load(), b.load()
    total = 0
    for y in range(a.height):
        for x in range(a.width):
            total += sum(abs(pa[x, y][i] - pb[x, y][i]) for i in range(3))
    return total / (a.width * a.height * 3 * 255)


def page_image(base: str, jid: str, uid: str) -> Image.Image:
    status, raw, _ = request("GET", f"{base}/api/jobs/{jid}/page/{uid}")
    return Image.open(io.BytesIO(raw)).convert("RGB").resize((64, 36), Image.LANCZOS)


def match_order(base: str, jid: str, data: bytes, expected_uids: List[str]) -> Tuple[bool, str]:
    """Does the PDF's page order match the uids the user arranged?"""
    actual = pdf_pages(data)
    if len(actual) != len(expected_uids):
        return False, f"PDF has {len(actual)} page(s), expected {len(expected_uids)}"
    for index, uid in enumerate(expected_uids):
        want = page_image(base, jid, uid)
        got = actual[index]
        if got is None:
            return False, f"page {index + 1} has no image"
        distance = image_distance(want, got)
        if distance > 0.06:
            return False, (f"page {index + 1} does not match uid {uid} "
                           f"(distance {distance:.3f})")
    return True, ""


# --------------------------------------------------------------------------- #
# the suite
# --------------------------------------------------------------------------- #

def run(base: str) -> None:
    lecture = os.path.join(FIXTURES, "lecture.mp4")
    static = os.path.join(FIXTURES, "static.mp4")
    for path in (lecture, static):
        if not os.path.exists(path):
            print(f"Missing fixture {path}. Build them first:\n"
                  f"  cd engine && python3 tests/make_fixtures.py")
            sys.exit(2)

    # ---- 1. the detector still works on an uploaded file ------------------ #
    print("\n[1] local video upload + detection")
    status, started = upload_video(base, lecture, sensitivity="balanced",
                                   title="Test Notes", image_format="jpeg", quality="92",
                                   max_width="2200")
    check("upload accepted (200)", status == 200, f"got {status}: {started}")
    jid = started.get("id", "")
    state = wait_ready(base, jid)
    check("job finished ready", state.get("status") == "ready",
          f"status={state.get('status')} error={state.get('error')}")
    pages = state.get("pages", [])
    check("detector still finds 4 pages", len(pages) == 4, f"found {len(pages)}")
    times = [round(p["time"], 1) for p in pages]
    check("page timestamps unchanged (1.0/13.0/16.0/25.0)",
          times == [1.0, 13.0, 16.0, 25.0], f"got {times}")
    check("every page has a stable uid", all(p.get("uid") for p in pages))
    check("every page is selected on arrival", all(not p["removed"] for p in pages))
    check("jobs advertise capture support", state.get("can_capture") is True)

    original_uids = [p["uid"] for p in pages]

    # ---- 2. thumbnails and full-size pages actually serve ----------------- #
    print("\n[2] page images")
    ok_thumbs = True
    for page in pages:
        code, raw, headers = request("GET", base + page["thumbnail"])
        if code != 200 or not raw.startswith(b"\xff\xd8"):
            ok_thumbs = False
            break
    check("all thumbnails serve as JPEG", ok_thumbs)
    code, raw, _ = request("GET", base + pages[0]["full"])
    check("full-size page serves as PNG", code == 200 and raw.startswith(b"\x89PNG"),
          f"code={code}")

    # ---- 3. generate a 4-page PDF ---------------------------------------- #
    print("\n[3] generate PDF (4 pages)")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf", json_body={"title": "Test Notes"})
    state = json.loads(raw.decode())
    check("generate returns 200", code == 200, f"got {code}: {raw[:200]}")
    pdf = state.get("pdf", {})
    check("server reports the PDF ready", pdf.get("status") == "ready", str(pdf.get("error")))
    check("server verified the file", pdf.get("verified") is True, str(pdf.get("verify_note")))
    check("page count is 4", pdf.get("page_count") == 4, str(pdf.get("page_count")))
    check("filename follows the title", pdf.get("filename") == "Test-Notes.pdf",
          str(pdf.get("filename")))

    code, data, headers = request("GET", f"{base}/api/jobs/{jid}/pdf")
    check("download serves the PDF", code == 200 and data.startswith(b"%PDF-"),
          f"code={code}")
    check("bytes match what was generated", len(data) == pdf.get("byte_size"),
          f"{len(data)} vs {pdf.get('byte_size')}")
    disposition = header(headers, "Content-Disposition")
    check("attachment filename is set", "Test-Notes.pdf" in disposition, disposition)
    check("download is served as application/pdf",
          header(headers, "Content-Type").startswith("application/pdf"),
          header(headers, "Content-Type"))
    check("the attachment filename ends in .pdf", ".pdf\"" in disposition.replace("'", "\""),
          disposition)
    reader = PdfReader(io.BytesIO(data))
    check("pypdf reads 4 pages", len(reader.pages) == 4, str(len(reader.pages)))
    check("PDF title metadata is the chosen name",
          (reader.metadata.title or "") == "Test Notes",
          str(reader.metadata.title if reader.metadata else None))
    ok, why = match_order(base, jid, data, original_uids)
    check("pages appear in the original order", ok, why)
    sizes = {im.image.size for page in reader.pages for im in page.images}
    check("full-resolution images are embedded, not thumbnails",
          all(w >= 1000 for (w, h) in sizes), str(sizes))
    box = reader.pages[0].mediabox
    check("A4 page geometry", abs(float(box.width) - 842) < 2 and abs(float(box.height) - 595) < 2,
          f"{box.width}x{box.height}")

    # ---- 4. remove, restore, reorder -------------------------------------- #
    print("\n[4] editing: remove / restore / reorder")
    removed_uids = [original_uids[1], original_uids[3]]
    state = get_json(base, f"/api/jobs/{jid}")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pages",
                           json_body={"order": original_uids, "removed": removed_uids})
    state = json.loads(raw.decode())
    check("remove accepted", code == 200, raw[:200])
    check("2 pages selected, 2 removed",
          state["selected_count"] == 2 and state["removed_count"] == 2,
          f"{state['selected_count']}/{state['removed_count']}")
    check("PDF is now flagged stale", state["pdf"]["stale"] is True)

    code, raw, _ = request("GET", f"{base}/api/jobs/{jid}/pdf")
    check("stale PDF is refused, not silently served", code == 409,
          f"got {code}")
    check("refusal explains why", b"changed" in raw.lower(), raw[:160].decode(errors="replace"))

    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf")
    state = json.loads(raw.decode())
    check("regenerate after removal succeeds", code == 200, raw[:160])
    check("regenerated PDF has 2 pages", state["pdf"]["page_count"] == 2,
          str(state["pdf"]["page_count"]))
    code, data2, _ = request("GET", f"{base}/api/jobs/{jid}/pdf")
    ok, why = match_order(base, jid, data2, [original_uids[0], original_uids[2]])
    check("removed pages are really gone and the rest kept", ok, why)

    reversed_order = list(reversed(original_uids))
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pages",
                           json_body={"order": reversed_order, "removed": []})
    check("restore + reorder accepted", code == 200, raw[:160])
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf")
    state = json.loads(raw.decode())
    check("regenerated PDF has 4 pages again", state["pdf"]["page_count"] == 4)
    code, data3, _ = request("GET", f"{base}/api/jobs/{jid}/pdf")
    ok, why = match_order(base, jid, data3, reversed_order)
    check("PDF order follows the user's order", ok, why)

    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pages",
                           json_body={"order": original_uids, "removed": []})
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf")
    code, data4, _ = request("GET", f"{base}/api/jobs/{jid}/pdf")
    ok, why = match_order(base, jid, data4, original_uids)
    check("order restored to the original", ok, why)

    # ---- 5. manual capture ------------------------------------------------ #
    print("\n[5] capture a page by hand at a timestamp")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/capture",
                           json_body={"time": 20.0})
    check("capture accepted", code == 200, raw[:200])
    payload = json.loads(raw.decode())
    manual_uid = payload.get("uid")
    state = payload.get("job", {})
    check("job now has 5 pages", state.get("page_count") == 5, str(state.get("page_count")))
    manual = next((p for p in state["pages"] if p["uid"] == manual_uid), None)
    check("captured page is flagged manual", bool(manual and manual["manual"]))
    check("captured page records its timestamp",
          bool(manual and abs(manual["time"] - 20.0) < 0.02), str(manual and manual["time"]))

    moved_order = original_uids[:2] + [manual_uid] + original_uids[2:]
    request("POST", f"{base}/api/jobs/{jid}/pages",
            json_body={"order": moved_order, "removed": []})
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf")
    state = json.loads(raw.decode())
    check("PDF with the manual page has 5 pages", state["pdf"]["page_count"] == 5,
          str(state["pdf"]["page_count"]))
    code, data5, _ = request("GET", f"{base}/api/jobs/{jid}/pdf")
    ok, why = match_order(base, jid, data5, moved_order)
    check("manual page sits where the user put it", ok, why)

    # the captured frame really is the frame at that second
    code, raw, _ = request("GET", f"{base}/api/jobs/{jid}/page/{manual_uid}")
    captured = Image.open(io.BytesIO(raw)).convert("RGB").resize((64, 36), Image.LANCZOS)
    from pagesnap.ffmpegio import grab_frame
    reference_raw = grab_frame(lecture, 20.0, "png")
    reference = Image.open(io.BytesIO(reference_raw)).convert("RGB").resize((64, 36), Image.LANCZOS)
    check("captured image matches the video at that second",
          image_distance(captured, reference) < 0.02,
          f"distance {image_distance(captured, reference):.4f}")

    bad_cases = [("past the end", {"time": 9999}), ("negative", {"time": -5}),
                 ("not a number", {"time": "later"}), ("missing", {})]
    for label, body in bad_cases:
        code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/capture", json_body=body)
        check(f"capture rejected: {label}", 400 <= code < 500, f"got {code}")

    # ---- 6. single-page PDF ---------------------------------------------- #
    print("\n[6] single-page video")
    status, started2 = upload_video(base, static, sensitivity="balanced",
                                    title="One Page", image_format="jpeg", quality="92",
                                    max_width="2200")
    jid2 = started2.get("id", "")
    state2 = wait_ready(base, jid2)
    check("static video yields exactly 1 page", len(state2.get("pages", [])) == 1,
          f"found {len(state2.get('pages', []))}")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid2}/pdf")
    state2 = json.loads(raw.decode())
    check("single-page PDF generated", code == 200 and state2["pdf"]["page_count"] == 1,
          raw[:200])
    code, data6, _ = request("GET", f"{base}/api/jobs/{jid2}/pdf")
    check("single-page file is a real PDF",
          code == 200 and len(PdfReader(io.BytesIO(data6)).pages) == 1)

    # a one-page PDF after removing that page must refuse, not produce an empty file
    request("POST", f"{base}/api/jobs/{jid2}/pages", json_body={"removed": [p["uid"] for p in state2["pages"]]})
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid2}/pdf")
    check("all-removed is refused with a clear message", code == 400 and b"tick" in raw.lower(),
          f"code={code} {raw[:160]}")

    # ---- 7. contact sheet preview ---------------------------------------- #
    print("\n[7] preview of the selected pages")
    code, raw, headers = request("GET", f"{base}/api/jobs/{jid}/sheet")
    check("contact sheet renders as PNG", code == 200 and raw.startswith(b"\x89PNG"),
          f"code={code}")
    sheet = Image.open(io.BytesIO(raw))
    check("contact sheet is a real image", sheet.width > 500 and sheet.height > 200,
          f"{sheet.width}x{sheet.height}")

    # ---- 8. bad and unsupported sources ---------------------------------- #
    print("\n[8] invalid and unsupported sources")
    bad_urls = [
        ("empty", "", "paste"),
        ("not a url", "not a url at all", "web address"),
        ("wrong scheme", "ftp://example.com/video.mp4", "http"),
        ("file scheme", "file:///etc/passwd", "http"),
    ]
    for label, url, expect_word in bad_urls:
        body, content_type = multipart({"url": url}, "unused", "x", b"")
        code, raw, _ = request("POST", base + "/api/fetch", data=body,
                               headers={"Content-Type": content_type}, timeout=60)
        payload = json.loads(raw.decode() or "{}")
        ok = 400 <= code < 500 and payload.get("error")
        check(f"rejected cleanly: {label}", ok, f"code={code} body={raw[:120]}")
        if expect_word:
            check(f"  ↳ reason mentions '{expect_word}'",
                  expect_word in (payload.get("error", "") + payload.get("advice", "")).lower(),
                  str(payload)[:200])

    # a host that cannot be reached: the request returns immediately (so the UI can
    # show progress), and the *job* must then fail with a reason - not hang forever.
    body, content_type = multipart({"url": "http://127.0.0.1:9/nothing-here.mp4"}, "unused", "x", b"")
    code, raw, _ = request("POST", base + "/api/fetch", data=body,
                           headers={"Content-Type": content_type}, timeout=30)
    check("unreachable link accepted as a job", code == 200, f"got {code}: {raw[:120]}")
    jid_dead = json.loads(raw.decode()).get("id") if code == 200 else None
    if jid_dead:
        start = time.time()
        state_dead = wait_ready(base, jid_dead, timeout=90)
        check("unreachable link fails with a reason, and reasonably fast",
              state_dead.get("status") == "failed" and state_dead.get("error"),
              f"status={state_dead.get('status')} after {time.time() - start:.1f}s "
              f"error={state_dead.get('error')}")
        check("  ↳ the reason is about the connection",
              any(word in (state_dead.get("error", "") + state_dead.get("error_advice", "")).lower()
                  for word in ("reach", "connect", "network", "refused", "download")),
              str(state_dead.get("error"))[:160])

    # an HTML page that is reachable but is not a video - the prototype's own page,
    # whatever port it happens to be on, so this test never depends on a fixed port
    body, content_type = multipart({"url": base + "/"}, "unused", "x", b"")
    try:
        code, raw, _ = request("POST", base + "/api/fetch", data=body,
                               headers={"Content-Type": content_type}, timeout=120)
        jid3 = json.loads(raw.decode()).get("id")
        if jid3:
            state3 = wait_ready(base, jid3, timeout=120)
            check("an HTML link fails with an explanation, not a fake success",
                  state3.get("status") == "failed" and state3.get("error"),
                  str(state3)[:240])
    except Exception as exc:                                     # noqa: BLE001
        check("an HTML link fails with an explanation, not a fake success", False, str(exc))

    # uploading something that is not a video must not pretend to work
    body, content_type = multipart({}, "video", "notes.txt", b"this is not a video" * 500)
    code, raw, _ = request("POST", base + "/api/upload", data=body,
                           headers={"Content-Type": content_type})
    payload = json.loads(raw.decode() or "{}")
    check("non-video upload rejected up front", code == 400 and payload.get("error"),
          f"code={code} {raw[:160]}")
    check("non-video upload explains itself",
          "not a video" in payload.get("error", "").lower(), str(payload)[:200])

    # Regression: ffmpeg will happily "play" a text file as a `tty` stream (ANSI art,
    # 640x400), so a decoder-only check once let a .txt through as a 1-page video.
    # The container has to be inspected, not just the fact that pixels came out.
    for label, filename, blob in [
        ("text file with a .txt name", "notes.txt", b"chapter six\n" * 400),
        ("text file renamed to .mp4", "lecture.mp4", b"chapter six\n" * 400),
    ]:
        body, content_type = multipart({}, "video", filename, blob)
        code, raw, _ = request("POST", base + "/api/upload", data=body,
                               headers={"Content-Type": content_type})
        payload = json.loads(raw.decode() or "{}")
        check(f"{label} is refused", code == 400, f"code={code} {raw[:120]}")
        check(f"  ↳ and says why ({label})",
              payload.get("kind") == "not_a_video" and payload.get("advice"),
              str(payload)[:200])

    # Regression: a form field that never arrived must still produce our own clear
    # message, not FastAPI's raw validation envelope ({"detail":[...]}).
    code, raw, _ = request("POST", base + "/api/fetch", data=b"", timeout=20,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    payload = json.loads(raw.decode() or "{}")
    check("a request with no link gets a plain, readable error",
          code in (400, 422) and payload.get("error") and not isinstance(payload.get("detail"), list),
          f"code={code} {raw[:200]}")
    check("  ↳ and it tells the user what to do",
          bool(payload.get("advice")) and "link" in json.dumps(payload).lower(),
          str(payload)[:200])

    # ---- 9. a real direct link ------------------------------------------- #
    print("\n[9] importing a direct video link")
    sample = os.path.join(ROOT, "docs", "sample", "lecture.mp4")
    if os.path.exists(sample):
        # serve the sample over HTTP the way a real host would
        port = free_port()
        server = subprocess.Popen([sys.executable, os.path.join(ROOT, "docs", "serve.py"),
                                   "--port", str(port), "--host", "127.0.0.1"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            up = False
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                    up = True
                    break
                except Exception:                                # noqa: BLE001
                    time.sleep(0.3)
            check("local test host started", up)
            if up:
                link = f"http://127.0.0.1:{port}/sample/lecture.mp4"
                body, content_type = multipart({"url": link}, "unused", "x", b"")
                code, raw, _ = request("POST", base + "/api/fetch", data=body,
                                       headers={"Content-Type": content_type})
                check("direct link accepted", code == 200, f"{code} {raw[:160]}")
                jid4 = json.loads(raw.decode()).get("id")
                state4 = wait_ready(base, jid4, timeout=180)
                check("link job reached ready", state4.get("status") == "ready",
                      f"{state4.get('status')} {state4.get('error')}")
                check("link import found the 4 pages", len(state4.get("pages", [])) == 4,
                      str(len(state4.get("pages", []))))
                code, raw, _ = request("POST", f"{base}/api/jobs/{jid4}/pdf")
                state4 = json.loads(raw.decode())
                check("PDF from a link import works", code == 200 and state4["pdf"]["page_count"] == 4,
                      raw[:200])
        finally:
            server.terminate()
            server.wait(timeout=10)
    else:
        check("local test host started", False, "docs/sample/lecture.mp4 missing")

    # ---- 10. how a link is classified ------------------------------------ #
    print("\n[10] link classification: video file, web page, expired link")

    # These run against the real mapping function, with the exact strings the
    # downloader produces, because getting these wrong sends a student hunting for a
    # problem with their internet that does not exist.
    print("      (what the user is told, per failure)")
    for label, text, expected in [
        ("a link whose token ran out", "ERROR: [generic] Unable to download webpage: "
                                       "HTTP Error 403: Forbidden", "expired_link"),
        ("a link that needs a login", "ERROR: [generic] Unable to download webpage: "
                                      "HTTP Error 401: Unauthorized", "expired_link"),
        ("a video that was deleted", "ERROR: [generic] Unable to download webpage: "
                                     "HTTP Error 410: Gone", "unavailable"),
        ("a link that 404s", "ERROR: [generic] Unable to download webpage: "
                             "HTTP Error 404: Not Found", "unavailable"),
        ("a refused connection", "ERROR: [generic] Unable to download webpage: "
                                 "<urlopen error [Errno 111] Connection refused>", "network"),
        ("a private post", "ERROR: [instagram] abc: This video is private", "needs_login"),
        ("a DRM stream", "ERROR: [vimeo] 1: This video is DRM protected", "drm"),
        ("a live stream", "ERROR: [twitch] abc: This live stream is not available",
         "live_stream"),
    ]:
        problem = snapserver.explain_ytdlp(Exception(text))
        check(f"{label} is reported as '{expected}'", problem.kind == expected,
              f"got '{problem.kind}': {problem.message}")
        check(f"  ↳ and has something useful to say ({label})",
              len(problem.message) > 20 and problem.advice is not None, str(problem))

    # The guard itself: every hop of a redirect is checked with the same rule as the
    # original link, so a public URL cannot bounce us onto a private address.
    for label, url, expect in [
        ("the cloud metadata address", "http://169.254.169.254/latest/meta-data/",
         "not allowed"),
        ("a non-http redirect target", "file:///etc/passwd", "only http and https"),
    ]:
        try:
            snapserver._assert_allowed(url, redirecting=True)
            check(f"a redirect to {label} is refused", False, "it was allowed")
        except snapserver.SourceProblem as problem:
            check(f"a redirect to {label} is refused", expect in problem.message,
                  problem.message)
    snapserver._assert_allowed("http://127.0.0.1:4567/sample.mp4", redirecting=True)
    check("an ordinary redirect target is still allowed", True)

    # A small host that serves the same video under several disguises.
    sample = os.path.join(ROOT, "docs", "sample", "lecture.mp4")
    if os.path.exists(sample):
        with open(sample, "rb") as fh:
            clip = fh.read()

        class LinkHost(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):            # keep the test output clean
                pass

            def _reply(self, code, content_type, body=b"", extra=None, head_only=False):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body and not head_only:
                    self.wfile.write(body)

            def do_GET(self):                        # noqa: N802
                self._route(False)

            def do_HEAD(self):                       # noqa: N802
                self._route(True)

            def _route(self, head_only):
                path = self.path.split("?")[0]
                if path == "/clip":                  # a direct video, no .mp4 in the path
                    self._reply(200, "video/mp4", clip, head_only=head_only)
                elif path == "/article.html":        # the page a video sits on
                    self._reply(200, "text/html; charset=utf-8",
                                b"<html><body><h1>Lecture 6</h1><p>watch below</p>"
                                b"</body></html>", head_only=head_only)
                elif path == "/expired":             # a link whose token ran out
                    self._reply(403, "text/html", b"<html>expired</html>",
                                head_only=head_only)
                elif path == "/moved":
                    self._reply(302, "text/html", b"",
                                {"Location": "http://169.254.169.254/latest/meta-data/"},
                                head_only=head_only)
                else:
                    self._reply(404, "text/plain", b"nothing here", head_only=head_only)

        host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LinkHost)
        threading.Thread(target=host.serve_forever, daemon=True).start()
        link_port = host.server_address[1]
        print(f"      (test host on 127.0.0.1:{link_port})")

        def import_link(url, timeout=120):
            body, content_type = multipart({"url": url}, "unused", "x", b"")
            code, raw, _ = request("POST", base + "/api/fetch", data=body,
                                   headers={"Content-Type": content_type}, timeout=30)
            if code != 200:
                return code, json.loads(raw.decode() or "{}"), {}
            return code, json.loads(raw.decode()), wait_ready(
                base, json.loads(raw.decode()).get("id", ""), timeout=timeout)

        try:
            # 0. what the URL actually serves, asked before any download
            check("a video URL is identified as video/mp4",
                  snapserver._probe_content_type(f"http://127.0.0.1:{link_port}/clip")
                  == "video/mp4", "wrong content type")
            check("a web page URL is identified as html",
                  snapserver._probe_content_type(f"http://127.0.0.1:{link_port}/article.html")
                  .startswith("text/html"), "wrong content type")
            try:
                snapserver._probe_content_type(f"http://127.0.0.1:{link_port}/expired")
                check("an expired link is recognised while probing", False, "no error raised")
            except snapserver.SourceProblem as problem:
                check("an expired link is recognised while probing",
                      problem.kind == "expired_link", f"{problem.kind}: {problem.message}")
            try:
                snapserver._probe_content_type(f"http://127.0.0.1:{link_port}/moved")
                check("a redirect to a private address is refused while probing", False,
                      "the redirect was followed")
            except snapserver.SourceProblem as problem:
                check("a redirect to a private address is refused while probing",
                      "not allowed" in problem.message, problem.message)

            # 1. a direct video file whose URL has no media extension at all
            code, started, state = import_link(f"http://127.0.0.1:{link_port}/clip")
            check("a direct video link with no file extension is imported",
                  state.get("status") == "ready", f"{code} {state.get('status')} "
                  f"{state.get('error')}")
            check("  ↳ and its pages are detected",
                  len(state.get("pages", [])) == 4, str(len(state.get("pages", []))))

            # 2. a web page must be refused as a web page, not guessed at
            code, started, state = import_link(f"http://127.0.0.1:{link_port}/article.html")
            check("an ordinary web page is refused",
                  state.get("status") == "failed", f"{state.get('status')} {state.get('error')}")
            check("  ↳ and it says the link is a web page, not a video",
                  state.get("error_kind") == "web_page"
                  and "web page" in (state.get("error") or "").lower(),
                  f"{state.get('error_kind')}: {state.get('error')}")
            check("  ↳ and offers the local upload instead",
                  "choose" in (state.get("error_advice") or "").lower(),
                  str(state.get("error_advice"))[:140])
            check("  ↳ and nothing was detected from it",
                  not state.get("pages"), str(state.get("pages"))[:80])

            # 3. an expired link is an expired link, not a network problem
            code, started, state = import_link(f"http://127.0.0.1:{link_port}/expired")
            check("an expired link fails", state.get("status") == "failed",
                  f"{state.get('status')}")
            check("  ↳ and is not blamed on the connection",
                  state.get("error_kind") == "expired_link"
                  and "internet" not in (state.get("error") or " ").lower()
                  and "reached" not in (state.get("error") or " ").lower(),
                  f"{state.get('error_kind')}: {state.get('error')}")
            check("  ↳ and explains that time-limited links expire",
                  "expire" in (state.get("error") or "").lower()
                  or "no longer valid" in (state.get("error") or "").lower(),
                  str(state.get("error"))[:140])

            # 4. a redirect to a private address is refused, not followed
            code, started, state = import_link(f"http://127.0.0.1:{link_port}/moved")
            check("a link that redirects to a private address fails",
                  state.get("status") == "failed", f"{state.get('status')} "
                  f"{state.get('error')}")
            check("  ↳ and says so, instead of downloading from it",
                  state.get("error_kind") in ("bad_url", "network", "not_a_video"),
                  f"{state.get('error_kind')}: {state.get('error')}")
        finally:
            host.shutdown()
            host.server_close()
    else:
        check("a direct video link with no file extension is imported", False,
              "docs/sample/lecture.mp4 missing")

    # ---- 11. what a link is, before anything is fetched ------------------- #
    print("\n[11] YouTube Shorts and other link kinds")

    shorts = "https://www.youtube.com/shorts/Sh8EPts-DXk"
    for label, url, kind, video_id in [
        ("the task's Shorts URL", shorts, "youtube_short", "Sh8EPts-DXk"),
        ("a bare youtube.com Shorts URL", "https://youtube.com/shorts/Sh8EPts-DXk",
         "youtube_short", "Sh8EPts-DXk"),
        ("a mobile Shorts URL with a query string",
         "http://m.youtube.com/shorts/Sh8EPts-DXk?feature=share",
         "youtube_short", "Sh8EPts-DXk"),
        ("a Shorts URL with a trailing slash", shorts + "/", "youtube_short",
         "Sh8EPts-DXk"),
        ("a normal watch URL", "https://www.youtube.com/watch?v=Sh8EPts-DXk",
         "youtube_video", "Sh8EPts-DXk"),
        ("a watch URL inside a playlist",
         "https://www.youtube.com/watch?v=Sh8EPts-DXk&list=PLxyz&index=3",
         "youtube_video_with_playlist", "Sh8EPts-DXk"),
        ("a youtu.be short link", "https://youtu.be/Sh8EPts-DXk?si=abc",
         "youtube_video", "Sh8EPts-DXk"),
        ("an embedded player", "https://www.youtube.com/embed/Sh8EPts-DXk?rel=0",
         "youtube_embed", "Sh8EPts-DXk"),
        ("an embedded player on the nocookie host",
         "https://www.youtube-nocookie.com/embed/Sh8EPts-DXk", "youtube_embed",
         "Sh8EPts-DXk"),
        ("a live URL", "https://www.youtube.com/live/Sh8EPts-DXk", "youtube_live",
         "Sh8EPts-DXk"),
        ("a playlist", "https://www.youtube.com/playlist?list=PLabc",
         "youtube_playlist", ""),
        ("a channel handle", "https://www.youtube.com/@SomeChannel", "youtube_channel", ""),
        ("a channel id", "https://www.youtube.com/channel/UCabc123", "youtube_channel", ""),
        ("a search page", "https://www.youtube.com/results?search_query=physics+notes",
         "youtube_search", ""),
        ("an ordinary file link", "https://example.com/lecture.mp4", "direct_file", ""),
        ("an ordinary web link", "https://example.com/watch?id=42", "web_link", ""),
    ]:
        info = snapserver.classify_source(url)
        check(f"{label} is classified as {kind}", info.kind == kind,
              f"got {info.kind} for {url}")
        if video_id:
            check(f"  ↳ its video id is read out ({label})", info.video_id == video_id,
                  f"got {info.video_id!r}")
            check(f"  ↳ and it is fetched by id, not as the page it came from ({label})",
                  info.canonical.endswith("watch?v=" + video_id), info.canonical)
        else:
            check(f"  ↳ and it is not treated as a video ({label})", not info.video_id,
                  info.video_id)

    # An embedded player is a page. Nothing may hand it to the file downloader.
    embed_url = "https://www.youtube.com/embed/Sh8EPts-DXk"
    embed = snapserver.classify_source(embed_url)
    check("an embedded player is not treated as a downloadable file",
          embed.kind != "direct_file" and not snapserver._looks_like_media(embed_url),
          f"kind={embed.kind}")

    # playlists, channels and search pages are refused before any work starts
    for label, url, word in [
        ("a playlist", "https://www.youtube.com/playlist?list=PLabc", "playlist"),
        ("a channel", "https://www.youtube.com/@SomeChannel", "channel"),
        ("a search page", "https://www.youtube.com/results?search_query=notes", "search"),
    ]:
        body, content_type = multipart({"url": url}, "unused", "x", b"")
        code, raw, _ = request("POST", base + "/api/fetch", data=body,
                               headers={"Content-Type": content_type}, timeout=30)
        payload = json.loads(raw.decode() or "{}")
        check(f"{label} is refused up front", code == 400 and not payload.get("id"),
              f"code={code} {raw[:140]}")
        check(f"  ↳ with a reason that names it ({label})",
              word in (payload.get("error", "") + payload.get("advice", "")).lower(),
              str(payload)[:160])

    # the real URL from the task, against the real YouTube
    print("      (the Shorts URL, against youtube.com)")
    body, content_type = multipart({"url": shorts}, "unused", "x", b"")
    code, raw, _ = request("POST", base + "/api/fetch", data=body,
                           headers={"Content-Type": content_type}, timeout=30)
    check("the Shorts URL is accepted as a job", code == 200, f"{code} {raw[:160]}")
    started = json.loads(raw.decode() or "{}")
    check("the job says it is a YouTube Short",
          started.get("source", {}).get("kind") == "youtube_short",
          str(started.get("source")))
    check("and carries the video id",
          started.get("source", {}).get("video_id") == "Sh8EPts-DXk",
          str(started.get("source")))
    if started.get("id"):
        state_yt = wait_ready(base, started["id"], timeout=240)
        if state_yt.get("status") == "ready":
            # This machine can reach YouTube: the video really was fetched.
            print("      this machine reached YouTube - the video was fetched")
            check("the Short was fetched and its pages detected",
                  len(state_yt.get("pages", [])) > 0, str(state_yt.get("pages"))[:120])
            check("  ↳ and the job is genuinely ready for detection",
                  state_yt.get("can_capture") is True, str(state_yt.get("can_capture")))
        else:
            # It could not: the message has to say so honestly, and never fake success.
            print(f"      youtube.com is not reachable from this machine "
                  f"({state_yt.get('error_kind')}) - checking what the user is told")
            check("a failed fetch never claims the video is ready",
                  state_yt.get("status") == "failed" and not state_yt.get("pages"),
                  f"{state_yt.get('status')} pages={len(state_yt.get('pages') or [])}")
            said = f"{state_yt.get('error', '')} {state_yt.get('error_advice', '')}".lower()
            check("it explains that the video plays but cannot be fetched here",
                  "cannot" in said and ("browser" in said or "could not be reached" in said),
                  said[:200])
            check("  ↳ and it does not blame the user's connection",
                  "check the connection" not in said and "check your internet" not in said,
                  said[:200])
            check("  ↳ and it offers the local file instead",
                  "choose the file here" in said or "upload" in said, said[:200])

    # The platform path itself, end to end: a *page* that embeds a video, which is
    # what yt-dlp does for YouTube. This proves the integration, not the network.
    with open(os.path.join(ROOT, "docs", "sample", "lecture.mp4"), "rb") as fh:
        embedded_clip = fh.read()
    page_html = (b"<!doctype html><html><head><title>Lecture 6 - Physics</title></head>"
                 b"<body><video controls src='/media/lecture.mp4'></video></body></html>")

    class PageHost(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):                 # keep the output clean
            pass

        def do_GET(self):                             # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/watch"):
                body, ctype = page_html, "text/html; charset=utf-8"
            elif path == "/media/lecture.mp4":
                body, ctype = embedded_clip, "video/mp4"
            else:
                body, ctype = b"", "text/plain"
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    page_host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PageHost)
    threading.Thread(target=page_host.serve_forever, daemon=True).start()
    page_port = page_host.server_address[1]
    try:
        body, content_type = multipart({"url": f"http://127.0.0.1:{page_port}/watch?v=abc"},
                                       "unused", "x", b"")
        code, raw, _ = request("POST", base + "/api/fetch", data=body,
                               headers={"Content-Type": content_type}, timeout=30)
        check("a page that embeds a video is accepted", code == 200, f"{code} {raw[:140]}")
        jid_page = json.loads(raw.decode() or "{}").get("id")
        state_page = wait_ready(base, jid_page, timeout=240) if jid_page else {}
        check("the extractor resolved the page's video and it was detected",
              state_page.get("status") == "ready" and len(state_page.get("pages", [])) == 4,
              f"{state_page.get('status')} {len(state_page.get('pages') or [])} pages "
              f"{state_page.get('error')}")
        check("  ↳ the title came from the page",
              "Lecture 6" in (state_page.get("title") or ""), str(state_page.get("title")))
        code, raw, _ = request("POST", f"{base}/api/jobs/{jid_page}/pdf")
        check("  ↳ and a PDF from it verifies",
              code == 200 and json.loads(raw.decode())["pdf"]["verified"] is True,
              raw[:160])
    finally:
        page_host.shutdown()
        page_host.server_close()

    # ---- 12. the filename on the download --------------------------------- #
    print("\n[12] download filenames, including other scripts")

    # A title in Devanagari used to raise UnicodeEncodeError inside the header and
    # answer 500: generation worked, the download did not. Every title here must come
    # back as a real PDF, with the pages that are currently selected in it.
    state_now = get_json(base, f"/api/jobs/{jid}")
    expected_pages = state_now.get("selected_count") or 0
    print(f"      (job {jid[:8]} currently has {expected_pages} page(s) selected)")
    check("there is something selected to export", expected_pages > 0,
          str(state_now.get("selected_count")))

    for label, title, expect_name in [
        ("plain english", "Chapter 6", "Chapter-6.pdf"),
        ("hindi", "अध्याय 6", "अध्याय-6.pdf"),
        ("punjabi", "ਪਾਠ 6", "ਪਾਠ-6.pdf"),
        ("accented latin", "Café Notes", "Café-Notes.pdf"),
        ("cjk", "第6章", "第6章.pdf"),
        ("quotes", 'My "notes"', "My-notes.pdf"),
        ("punctuation only", "!!!", "!!!.pdf"),   # legal, and it is what they typed
        ("empty", "   ", "notes.pdf"),
        ("a path", "../etc/passwd", "etcpasswd.pdf"),
    ]:
        code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pdf",
                               json_body={"title": title})
        generated = json.loads(raw.decode()) if code == 200 else {}
        name = generated.get("pdf", {}).get("filename")
        check(f"{label}: the PDF is generated", code == 200, f"code={code} {raw[:120]}")
        check(f"  ↳ the name keeps the user's own script ({label})", name == expect_name,
              f"got {name!r}, wanted {expect_name!r}")
        ascii_name = generated.get("pdf", {}).get("filename_ascii", "")
        check(f"  ↳ and there is an ASCII fallback for the header ({label})",
              ascii_name.isascii() and ascii_name.endswith(".pdf"), repr(ascii_name))
        code, data, headers = request("GET", f"{base}/api/jobs/{jid}/pdf")
        check(f"  ↳ the download works ({label})",
              code == 200 and data.startswith(b"%PDF-"), f"HTTP {code}")
        if code == 200:
            pages_in_file = len(PdfReader(io.BytesIO(data)).pages)
            check(f"  ↳ the file opens and has the selected pages ({label})",
                  pages_in_file == expected_pages,
                  f"{pages_in_file} pages, expected {expected_pages}")
            disposition = header(headers, "Content-Disposition")
            star = urllib.parse.unquote(disposition.split("filename*=UTF-8''")[-1])
            check(f"  ↳ filename* carries the real name ({label})", star == expect_name,
                  f"{star!r} in {disposition!r}")
            check(f"  ↳ the plain filename stays ASCII ({label})",
                  disposition.split('filename="')[1].split('"')[0].isascii(), disposition)

    # ---- 11. server hygiene ---------------------------------------------- #
    print("\n[13] server behaviour")
    code, raw, _ = request("GET", base + "/api/jobs/does-not-exist")
    check("unknown job -> 404", code == 404, f"got {code}")
    code, raw, _ = request("GET", base + "/api/jobs/does-not-exist/pdf")
    check("unknown job PDF -> 404", code == 404, f"got {code}")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pages", json_body={"order": "nope"})
    check("bad edit payload -> 422, not 500", code == 422, f"got {code}")
    code, raw, _ = request("POST", f"{base}/api/jobs/{jid}/pages",
                           json_body={"order": ["bogus-uid"], "removed": ["bogus-uid"]})
    check("unknown uids are ignored, not fatal", code == 200, f"got {code}")
    code, raw, _ = request("GET", base + "/api/sources")
    sources = json.loads(raw.decode())
    check("sources endpoint describes what works",
          "link" in sources and "file" in sources and sources["file"]["works"] is True)


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = os.path.join(ROOT, "prototype", "tests", ".server.log")

    print(f"Starting the prototype server on port {port} …")
    with open(log_path, "w") as log:
        server = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "prototype", "server.py"),
             "--port", str(port), "--host", "127.0.0.1"],
            stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            ready = False
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(base + "/api/sources", timeout=2)
                    ready = True
                    break
                except Exception:                                # noqa: BLE001
                    if server.poll() is not None:
                        break
                    time.sleep(0.4)
            if not ready:
                print("server did not start; log follows")
                print(open(log_path).read()[-3000:])
                return 2

            run(base)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    # the server must not have logged an unhandled exception anywhere
    log_text = open(log_path).read()
    print("\n[14] server log")
    check("no tracebacks in the server log", "Traceback (most recent call last)" not in log_text,
          log_text[-1500:] if "Traceback" in log_text else "")
    check("no 500 responses in the log", " 500 " not in log_text,
          "\n".join(re.findall(r".* 500 .*", log_text))[:800])
    if re.findall(r".* (4\d\d) .*", log_text):
        print("      (4xx responses are expected: they are the rejected sources)")

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nfailures:")
        for item in FAILED:
            print("  - " + item)
        print(f"\nserver log: {log_path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
