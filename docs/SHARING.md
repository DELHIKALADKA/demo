# How to share this with other people

The app in this folder is a **static web app**: no server, no database, no build
step, no login. That is what makes it free to host and trivial to share — the
visitor's own phone or laptop does all the work, so nothing you host ever sees a
video, a screenshot or a PDF.

Three ways to share it, fastest first.

---

## 1. Send the single file (works with no internet afterwards)

```bash
python3 docs/build_single.py       # -> docs/pagesnap-offline.html  (40 KB)
```

`pagesnap-offline.html` is the entire app in one file. AirDrop it, email it, put
it in a class WhatsApp group, or hand it over on a pen drive. A student saves it,
opens it, picks a video, and gets their PDF — **no internet needed after the file
arrives** (and no reason to trust a website with their videos, since it never
leaves their phone).

Best for: showing a friend, or a class with patchy data.

## 2. Put it on a real link (GitHub Pages)

One-time setup, then the link never changes:

1. **The repository must be public** for free GitHub Pages. (Pages from a private
   repo needs a paid plan.) Settings → General → bottom → Change visibility.
   The source code being visible is normal for a student project, and it is how
   people can check that the app really does not upload anything.
2. **Settings → Pages → Source: Deploy from a branch**
   → Branch: `main`, folder: `/docs` → **Save**.
3. A minute later the link is live:
   `https://<your-username>.github.io/<repo-name>/`

Best for: a link you can paste in a group and forget about.

## 3. Or any other static host (no repo changes at all)

Drag the `docs/` folder onto:

* **Netlify Drop** — https://app.netlify.com/drop
* **Cloudflare Pages** — https://pages.cloudflare.com
* **Vercel** — https://vercel.com/new

You sign in once (free) and get a URL like `https://pagesnap.netlify.app`. Same
files, no GitHub involved.

Best for: keeping the code private while still having a public link.

---

## What to tell people when you share it

Two honest limits, so nobody is disappointed:

* **This web version takes a video that is already on their device.** It cannot
  download from an Instagram / Facebook / YouTube link — browsers are not allowed
  to do that, and no website can work around it. If links are the whole point, the
  phone app in `../android` is the version that does it (it runs the downloader on
  the device).
* **Long videos are slow in a browser**, because a browser can only seek through
  the file frame by frame. Under about 20 minutes is comfortable; beyond that the
  page says so and suggests the app.

## Checking it works before you share it

```bash
python3 docs/serve.py --port 5173     # then open http://localhost:5173
node docs/test/run.mjs                # the page-finding engine vs ground truth: 15/15
node docs/test/browser.mjs            # the real page, end to end, in headless Chrome
```

The page has a **"Try it with a sample lecture video"** button, so the first thing
anyone clicks shows the whole thing work in about twenty seconds.
