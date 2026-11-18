#!/usr/bin/env python3
"""tests/test_page_reload.py — optional headless check of the header's reload button
(artifact/board.html): an installed app (a home-screen PWA) has no reload control of its own.
  - present at 360px (phone) and 1440px (desktop), light and dark: a 44px circle under the
    profile button, on top (not covered), inside the viewport, overlapping no other header item,
    adding no width, no horizontal scroll (at 1280px without the web fonts the header itself
    overflows a little, button or not, so the desktop check runs at 1440px);
  - labelled through tr(): "Reload" / "重新載入";
  - a click reloads the page; with unsent text (the message box) it asks first, in the page
    (no window.confirm): Cancel keeps the page and the text, Reload reloads.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0. CI does not run it.

  python3 tests/test_page_reload.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PAGE = "file://" + os.path.join(REPO, "artifact", "board.html") + "?demo=1"
VERBOSE = "-v" in sys.argv[1:]

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


GEOM_JS = """() => {
  const b = document.getElementById('reload-btn'); if (!b) return null;
  const r = b.getBoundingClientRect();
  const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const box = (el) => { const x = el.getBoundingClientRect(); return { l: x.left, t: x.top, r: x.right, b: x.bottom }; };
  const hit = (a, c) => a.l < c.r && c.l < a.r && a.t < c.b && c.t < a.b;
  const me = box(b);
  const others = [...document.querySelectorAll('.mast .brand-row > *, .mast .brand > p, .mast .meters > *, #acct-btn')]
    .filter((el) => el.offsetParent !== null && el.getBoundingClientRect().width > 0);
  const holder = document.getElementById('reload'); holder.style.display = 'none';
  const swWithout = document.documentElement.scrollWidth; holder.style.display = '';
  const acct = document.getElementById('acct-btn').getBoundingClientRect();
  return { w: r.width, h: r.height, swWithout, sameColumn: Math.abs(r.right - acct.right) < 1, inView: r.left >= 0 && r.right <= window.innerWidth && r.top >= 0,
    onTop: !!at && (at === b || b.contains(at)), label: b.getAttribute('aria-label'), title: b.getAttribute('title'),
    overlaps: others.filter((el) => hit(me, box(el))).map((el) => el.id || el.className),
    wide: document.documentElement.scrollWidth > window.innerWidth, bg: getComputedStyle(b).backgroundColor,
    fg: getComputedStyle(b).color, rect: [r.left, r.top, r.right, r.bottom], sw: document.documentElement.scrollWidth };
}"""


def main():
    errors = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
            print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
            return 0

        def new_page(width, scheme="light", lang=None):
            ctx = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=scheme)
            if lang:
                ctx.add_init_script("try { localStorage.setItem('switchboard.lang', %r); } catch (e) {}" % lang)
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("dialog", lambda d: (errors.append("a browser dialog: " + d.message), d.dismiss()))
            pg.goto(PAGE)
            pg.wait_for_selector("#reload-btn")
            pg.wait_for_function("() => document.querySelector('.secnav') && document.fonts.status === 'loaded'")
            pg.wait_for_timeout(800)   # the demo data renders after load
            return pg

        print("layout:")
        for width in (360, 1440):
            for scheme in ("light", "dark"):
                pg = new_page(width, scheme)
                g = pg.evaluate(GEOM_JS)
                name = "%dpx %s" % (width, scheme)
                check(name + ": a 44px button under the profile button, on top", g and g["w"] >= 44 and g["h"] >= 44
                      and g["sameColumn"] and g["onTop"], g)
                check(name + ": overlaps no header item, adds no width", g and not g["overlaps"] and g["sw"] == g["swWithout"], g)
                check(name + ": in view, no horizontal scroll", g["inView"] and not g["wide"], g)
                check(name + ": icon visible on its background", g and g["fg"] != g["bg"], g)
                pg.context.close()

        print("labels:")
        pg = new_page(360)
        g = pg.evaluate(GEOM_JS)
        check("aria-label and title 'Reload'", g["label"] == "Reload" and g["title"] == "Reload", g)
        pg.context.close()
        pg = new_page(360, lang="zh-TW")
        g = pg.evaluate(GEOM_JS)
        check("zh-TW: '重新載入'", g["label"] == "重新載入" and g["title"] == "重新載入", g)
        pg.context.close()

        print("reload:")
        pg = new_page(360)
        pg.evaluate("() => { window.__mark = 1; }")
        with pg.expect_navigation(timeout=5000):
            pg.click("#reload-btn")
        pg.wait_for_selector("#reload-btn")
        check("a click reloads the page", pg.evaluate("() => window.__mark === undefined"))
        pg.evaluate("() => { window.__mark = 1; }")
        pg.fill("#msg-input", "half-written note")
        pg.click("#reload-btn")
        pg.wait_for_timeout(300)
        ask = pg.evaluate("""() => ({ shown: !document.getElementById('reload-ask').hidden, mark: window.__mark,
          expanded: document.getElementById('reload-btn').getAttribute('aria-expanded'),
          focus: document.activeElement && document.activeElement.id,
          wide: document.documentElement.scrollWidth > window.innerWidth })""")
        check("unsent text: asks in the page first, nothing reloaded", ask == {"shown": True, "mark": 1, "expanded": "true", "focus": "reload-no", "wide": False}, ask)
        pg.click("#reload-no")
        kept = pg.evaluate("() => ({ hidden: document.getElementById('reload-ask').hidden, mark: window.__mark, text: document.getElementById('msg-input').value })")
        check("Cancel keeps the page and the text", kept == {"hidden": True, "mark": 1, "text": "half-written note"}, kept)
        pg.click("#reload-btn")
        pg.keyboard.press("Escape")
        check("Escape closes the question", pg.evaluate("() => document.getElementById('reload-ask').hidden"))
        pg.click("#reload-btn")
        with pg.expect_navigation(timeout=5000):
            pg.click("#reload-yes")
        pg.wait_for_selector("#reload-btn")
        check("Reload in the question reloads", pg.evaluate("() => window.__mark === undefined"))
        pg.context.close()

        check("no page errors, no browser dialogs", not errors, errors)
        browser.close()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
