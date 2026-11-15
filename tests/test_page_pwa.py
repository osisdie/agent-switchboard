#!/usr/bin/env python3
"""tests/test_page_pwa.py — optional headless check of the board as a home-screen app: the head
tags board-serve adds, the web app manifest and its icons, the inline favicon on the Artifact page,
and the standalone layout on an iPhone-sized screen (status bar and home indicator insets).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_pwa.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. PWA_SCREENSHOT=<file.png> saves the standalone view.
Uses a real bin/board-serve on a temporary BOARD_HOME; never touches ~/.agent-switchboard.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERBOSE = "-v" in sys.argv[1:]
TOP, BOTTOM = 59, 34  # an iPhone with a Dynamic Island, portrait

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


HEAD_JS = """async () => {
  const inHead = (sel) => { const e = document.head.querySelector(sel); return e ? (e.getAttribute('href') || e.getAttribute('content')) : null; };
  const load = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok([i.naturalWidth, i.naturalHeight]); i.onerror = () => ok(null); i.src = src; });
  const svgIcon = inHead('link[rel="icon"][type="image/svg+xml"]');
  const out = {
    manifest: inHead('link[rel="manifest"]'), touch: inHead('link[rel="apple-touch-icon"]'),
    png: inHead('link[rel="icon"][type="image/png"]'), svgIcon: svgIcon ? svgIcon.slice(0, 19) : null,
    svgLoads: svgIcon ? await load(svgIcon) : null,
    capable: inHead('meta[name="apple-mobile-web-app-capable"]'), mcapable: inHead('meta[name="mobile-web-app-capable"]'),
    bar: inHead('meta[name="apple-mobile-web-app-status-bar-style"]'), title: inHead('meta[name="apple-mobile-web-app-title"]'),
    themes: [...document.head.querySelectorAll('meta[name="theme-color"]')].map((m) => m.media + '=' + m.content),
  };
  if (out.manifest) {
    const r = await fetch(out.manifest);
    out.mtype = r.headers.get('content-type');
    try { out.m = await r.json(); } catch (e) { out.m = null; }
    out.icons = out.m ? await Promise.all(out.m.icons.map(async (i) => i.purpose + ' ' + i.sizes + ' ' + JSON.stringify(await load(i.src)))) : [];
  }
  if (out.touch) out.touchSize = await load(out.touch);
  return out;
}"""

LAYOUT_JS = """() => {
  const top = (sel) => document.querySelector(sel).getBoundingClientRect().top;
  const before = getComputedStyle(document.body, '::before');
  return {
    standalone: matchMedia('(display-mode: standalone)').matches || document.documentElement.classList.contains('standalone'),
    insetTop: parseFloat(getComputedStyle(document.documentElement).paddingTop),
    insetBottom: parseFloat(getComputedStyle(document.documentElement).paddingBottom),
    h1: top('.brand h1'), acct: top('#acct-btn'), band: parseFloat(before.height), bandBg: before.backgroundColor,
    scrollW: document.scrollingElement.scrollWidth, vw: innerWidth,
  };
}"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-pwa-test.")
    srv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []
            ctx = browser.new_context(viewport={"width": 393, "height": 852}, device_scale_factor=1)
            # iOS's flag for a page opened from the home screen (Chromium cannot emulate display-mode).
            ctx.add_init_script("if (location.protocol === 'http:') Object.defineProperty(navigator, 'standalone', { get: () => true });")
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)

            print("Artifact page (file://, no board-serve):")
            page.goto("file://" + os.path.join(REPO, "artifact", "board.html") + "?demo=1")
            page.wait_for_timeout(400)
            h = page.evaluate(HEAD_JS)
            check("inline SVG favicon in the head, and it renders", h["svgIcon"] == "data:image/svg+xml," and h["svgLoads"] == [64, 64], h)
            check("no manifest, no touch icon (local-only)", h["manifest"] is None and h["touch"] is None, h)

            print("board-serve (local mode):")
            home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
            for d in (home, cfg, sess):
                os.makedirs(d)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess)
            env.pop("BOARD_PUBLIC_URL", None)
            env.pop("TMUX", None)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            base = "http://127.0.0.1:%d" % port
            page.goto(base + "/")
            page.wait_for_timeout(800)
            h = page.evaluate(HEAD_JS)
            check("head: manifest, touch icon, PNG and SVG favicons",
                  h["manifest"] == "/manifest.webmanifest" and h["touch"] == "/apple-touch-icon.png"
                  and h["png"] == "/favicon.png" and h["svgIcon"] == "data:image/svg+xml,", h)
            check("head: home-screen app metas",
                  (h["capable"], h["mcapable"], h["bar"], h["title"]) == ("yes", "yes", "black-translucent", "Switchboard"), h)
            check("head: theme-color for light and dark",
                  h["themes"] == ["(prefers-color-scheme: light)=#edf0f4", "(prefers-color-scheme: dark)=#0f141a"], h)
            m = h.get("m") or {}
            check("manifest parses (CSP allows it), served as application/manifest+json",
                  m.get("display") == "standalone" and m.get("start_url") == "/" and h.get("mtype") == "application/manifest+json", h)
            check("manifest icons all load at their sizes", h.get("icons") == [
                "any 192x192 [192,192]", "any 512x512 [512,512]", "maskable 192x192 [192,192]",
                "maskable 512x512 [512,512]", "any any [64,64]"], h.get("icons"))
            check("touch icon is 180x180", h.get("touchSize") == [180, 180], h)

            print("standalone on an iPhone-sized screen:")
            cdp = ctx.new_cdp_session(page)
            try:
                cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": TOP, "bottom": BOTTOM, "left": 0, "right": 0}})
                emulated = True
            except Exception as exc:  # noqa: BLE001 - an older Chromium: skip this part
                print("  (skipped: this Chromium cannot emulate safe-area insets: %s)" % str(exc).splitlines()[0][:120])
                emulated = False
            if emulated:
                page.wait_for_timeout(200)
                s = page.evaluate(LAYOUT_JS)
                check("standalone (navigator.standalone), insets applied", s["standalone"] and s["insetTop"] == TOP and s["insetBottom"] == BOTTOM, s)
                check("the header clears the status bar", s["h1"] >= TOP and s["acct"] >= TOP, s)
                check("a dark band behind the black-translucent status bar", s["band"] == TOP and s["bandBg"] == "rgb(24, 33, 44)", s)
                check("no sideways scroll", s["scrollW"] <= s["vw"], s)
                page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")  # no smooth scroll
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(300)
                nav = page.evaluate("() => document.getElementById('secnav').getBoundingClientRect().top")
                check("scrolled: the sticky nav sits below the status bar", nav >= TOP, nav)
                page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")
                page.wait_for_timeout(300)
                gap = page.evaluate("() => innerHeight - document.querySelector('.wrap').getBoundingClientRect().bottom")
                check("at the bottom: content ends above the home indicator", gap >= BOTTOM, gap)
                if os.environ.get("PWA_SCREENSHOT"):
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(200)
                    page.screenshot(path=os.environ["PWA_SCREENSHOT"])

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv:
            srv.terminate()
            srv.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
