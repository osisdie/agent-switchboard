#!/usr/bin/env python3
"""tests/test_site.py — optional check of the screenshots and the static Pages site.

  python3 tests/test_site.py [-v]

1. scripts/screenshots.py renders every shot into a temp dir: WebP files, each under 200 KB.
2. scripts/build-site.py builds the site; it is served on 127.0.0.1 and opened headless:
   - demo.html boots the demo adapter by itself, makes no request beyond its own origin (no
     /api, no fonts, nothing third-party) and never calls claude.use, also while clicking
     around (card, decision answer, notifications, profile menu, theme);
   - index.html shows every screenshot and links the demo, the repo, README and contract.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it. PLAYWRIGHT_CHROMIUM_EXECUTABLE points at
a specific Chromium / headless shell when the one playwright expects is not installed.
"""
import functools
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERBOSE = "-v" in sys.argv[1:]
MAX_BYTES = 200 * 1024

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skipped: python playwright is not installed")
    sys.exit(0)

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


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-site-test.")
    httpd = None
    try:
        with sync_playwright() as p:
            try:
                p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None).close()
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0

        print("screenshots:")
        names = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "screenshots.py"), "--list"],
                               capture_output=True, text=True, check=True).stdout.split("\n")
        names = [n.split("\t")[0] for n in names if n]
        shots = os.path.join(tmp, "shots")
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "screenshots.py"), "--out", shots],
                           capture_output=True, text=True, timeout=600)
        check("the script runs", r.returncode == 0, r.stderr[-400:])
        want = sorted(n + ".webp" for n in names)
        got = sorted(os.listdir(shots)) if os.path.isdir(shots) else []
        check("every shot is written (%d)" % len(want), got == want and len(want) == 11, got)
        for f in got:
            with open(os.path.join(shots, f), "rb") as fh:
                head = fh.read(12)
            size = os.path.getsize(os.path.join(shots, f))
            check("  %s: WebP, %d KB" % (f, size // 1024), head[:4] == b"RIFF" and head[8:12] == b"WEBP" and size <= MAX_BYTES)
        # Reruns keep images that only differ by rasterization noise, and replace real changes.
        before = {f: open(os.path.join(shots, f), "rb").read() for f in got}
        shutil.copyfile(os.path.join(shots, "profile-menu.webp"), os.path.join(shots, "resources.webp"))
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "screenshots.py"), "--out", shots,
                            "--only", "overview-light,overview-dark,resources"], capture_output=True, text=True, timeout=300)
        check("rerun: unchanged views keep their file byte for byte",
              r.stdout.count("(unchanged)") == 2 and all(open(os.path.join(shots, f), "rb").read() == before[f]
                                                       for f in ("overview-light.webp", "overview-dark.webp")), r.stdout)
        check("rerun: a different image is replaced",
              open(os.path.join(shots, "resources.webp"), "rb").read() != before["profile-menu.webp"]
              and "resources" in r.stdout and "resources        1280x860  @1x" in r.stdout and "(unchanged)" not in r.stdout.split("resources ")[-1], r.stdout)
        # Same size, one digit different: a visible change must never pass as noise (1x and 2x).
        digit = "() => { const n = document.getElementById('needs-n'); n.textContent = String(Number(n.textContent) + 1); }"
        pct = "() => { const p = document.querySelector('#u-5h .u-p'); p.textContent = p.textContent.replace('91', '92'); }"
        for shot, js, what in (("overview-light", digit, "the Needs-you count"), ("usage-popover", pct, "91% -> 92%"),
                               ("phone-dark", pct, "91% -> 92% at 2x")):
            prev = open(os.path.join(shots, shot + ".webp"), "rb").read()
            r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "screenshots.py"), "--out", shots,
                                "--only", shot, "--eval", js], capture_output=True, text=True, timeout=300)
            check("rerun: one digit changed (%s) -> the file is replaced" % what,
                  r.returncode == 0 and "(unchanged)" not in r.stdout and open(os.path.join(shots, shot + ".webp"), "rb").read() != prev,
                  r.stdout + r.stderr[-300:])
        committed = sorted(os.listdir(os.path.join(REPO, "docs", "assets", "screenshots")))
        check("the committed set matches the script's list", committed == want, committed)

        print("site:")
        site = os.path.join(tmp, "site")
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build-site.py"), site],
                           capture_output=True, text=True)
        check("build-site runs", r.returncode == 0, r.stderr[-400:])
        for f in ["index.html", "demo.html", ".nojekyll", "icon.svg", "favicon.png"] + ["assets/screenshots/" + w for w in want]:
            check("  site/" + f, os.path.isfile(os.path.join(site, f)))

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=site))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/" % port

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            # A claude.ai-like host: the demo must not use it.
            ctx.add_init_script("window.__uses = []; window.claude = { use: (n) => { window.__uses.push(n); return Promise.resolve(null); } };")
            reqs, errors = [], []
            ctx.on("request", lambda rq: reqs.append(rq.url))
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(base + "demo.html")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            st = page.evaluate("""() => ({ demo: !document.getElementById('demo-tag').hidden,
              cards: document.querySelectorAll('article.scard').length, decisions: document.querySelectorAll('#dec-list > *').length })""")
            check("demo.html boots the demo adapter by itself", st["demo"] and st["cards"] >= 5 and st["decisions"] >= 3, st)
            page.click('article[aria-label="Session API"] .scard-toggle')
            page.click('#dec-d-2-demo button.option, #dec-d-2-demo .opts button')
            page.wait_for_timeout(300)
            page.keyboard.press("Escape")
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            page.keyboard.press("Escape")
            page.click("#acct-btn")
            page.click('label:has(input[name="sb-theme"][value="dark"])')
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            foreign = [u for u in reqs if not (u.startswith(base) or u.startswith("data:"))]
            check("demo: no request beyond its own origin", not foreign, foreign)
            check("demo: no request to /api", not [u for u in reqs if "/api/" in u], reqs)
            check("demo: only its own page loaded", sorted({u for u in reqs if u.startswith(base)}) == [base + "demo.html"], reqs)
            check("demo: claude.use never called", page.evaluate("() => window.__uses") == [], page.evaluate("() => window.__uses"))
            csp = page.evaluate("() => document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]').content")
            check("demo: CSP blocks connections", "connect-src 'none'" in csp, csp)

            reqs.clear()
            page.goto(base + "index.html")
            page.wait_for_load_state("networkidle")
            page.evaluate("() => Promise.all([...document.images].map((i) => { i.loading = 'eager'; return i.decode().catch(() => null); }))")
            ix = page.evaluate("""() => ({ imgs: [...document.querySelectorAll('.gallery img')].map((i) => [i.getAttribute('src'), i.naturalWidth > 0, !!i.alt]),
              links: [...document.querySelectorAll('a')].map((a) => a.getAttribute('href')) })""")
            check("index: every screenshot shows, with alt text",
                  len(ix["imgs"]) == len(want) and all(ok and alt for _src, ok, alt in ix["imgs"]), ix["imgs"])
            check("index: links to the demo, repo, README, contract",
                  "demo.html" in ix["links"] and any(l.endswith("agent-switchboard") for l in ix["links"])
                  and any(l.endswith("/README.md") for l in ix["links"]) and any(l.endswith("/docs/contract.md") for l in ix["links"]), ix["links"])
            foreign = [u for u in reqs if not (u.startswith(base) or u.startswith("data:"))]
            check("index: no request beyond its own origin", not foreign, foreign)
            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if httpd:
            httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
