#!/usr/bin/env python3
"""tests/test_site_build.py — stdlib check of scripts/build-site.py (no browser).

  python3 tests/test_site_build.py [-v]

Builds the Pages site into a temp dir and checks:
  - index.html and demo.html are written
  - the share preview (og-image.jpg) is a JPEG and is copied to the site root
  - index.html has Open Graph / Twitter card tags with absolute URLs
  - demo.html stays a stand-alone demo (board-mode=demo, connect-src 'none', noindex)

No playwright, so CI can run it. The optional headless pass is tests/test_site.py.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERBOSE = "-v" in sys.argv[1:]
SITE_URL = "https://osisdie.github.io/agent-switchboard"
passed = failed = 0


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        if VERBOSE:
            print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name)
        if detail is not None:
            print("       " + str(detail)[:400])


def attr(html, attr_name, value=None):
    """Return the content/property value of a meta tag, or whether it equals `value`."""
    # property="..." content="..." or name="..." content="..."
    pat = re.compile(r'<meta[^>]+(?:property|name)="%s"[^>]*>' % re.escape(attr_name), re.I)
    m = pat.search(html)
    if not m:
        return None if value is None else False
    c = re.search(r'\bcontent="([^"]*)"', m.group(0))
    got = c.group(1) if c else ""
    if value is None:
        return got
    return got == value


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-site-build.")
    try:
        site = os.path.join(tmp, "site")
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build-site.py"), site],
                           capture_output=True, text=True)
        check("build-site runs", r.returncode == 0, r.stderr or r.stdout)
        if r.returncode != 0:
            return 1
        for f in ("index.html", "demo.html", ".nojekyll", "icon.svg", "favicon.png",
                  "apple-touch-icon.png", "og-image.jpg"):
            check("  site/" + f, os.path.isfile(os.path.join(site, f)))

        og = os.path.join(site, "og-image.jpg")
        with open(og, "rb") as fh:
            head = fh.read(3)
        size = os.path.getsize(og)
        check("og-image.jpg is a JPEG between 20 KB and 300 KB",
              head == b"\xff\xd8\xff" and 20 * 1024 <= size <= 300 * 1024, size)

        with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
            index = fh.read()
        check("index: og:type is website", attr(index, "og:type", "website"))
        check("index: og:title is set", bool(attr(index, "og:title")))
        check("index: og:description is set", bool(attr(index, "og:description")))
        check("index: og:url is the Pages site", attr(index, "og:url", SITE_URL + "/"))
        check("index: og:image is an absolute Pages URL",
              attr(index, "og:image", SITE_URL + "/og-image.jpg"))
        check("index: og:image:width/height are 1200x630",
              attr(index, "og:image:width", "1200") and attr(index, "og:image:height", "630"))
        check("index: twitter:card is summary_large_image",
              attr(index, "twitter:card", "summary_large_image"))
        check("index: twitter:image matches og:image",
              attr(index, "twitter:image") == attr(index, "og:image"))
        check("index: links the demo, repo, README and contract",
              'href="demo.html"' in index
              and "https://github.com/osisdie/agent-switchboard" in index
              and "README.md" in index and "docs/contract.md" in index)
        check("index: mentions the pomodoro tally",
              "Pomodoro" in index and "25-minute" in index)
        # CSP on the landing page must not need third-party hosts (share tags are meta only).
        check("index: CSP still allows only self images and inline CSS",
              "img-src 'self'" in index and "default-src 'none'" in index)

        with open(os.path.join(site, "demo.html"), encoding="utf-8") as fh:
            demo = fh.read()
        check("demo: board-mode is demo", 'name="board-mode" content="demo"' in demo)
        check("demo: robots noindex", 'name="robots" content="noindex"' in demo)
        check("demo: CSP blocks connections", "connect-src &#x27;none&#x27;" in demo or "connect-src 'none'" in demo)
        check("demo: no Open Graph tags (the landing page is the share target)",
              "og:image" not in demo and "twitter:card" not in demo)
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    sys.exit(rc)
