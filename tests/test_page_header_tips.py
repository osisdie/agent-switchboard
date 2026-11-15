#!/usr/bin/env python3
"""tests/test_page_header_tips.py — optional headless check of the header popovers in
artifact/board.html: every tile, meter and pill in the header (demo tag, operator, owner away,
notifications, needs you, working, done, offline, Claude usage, system) has a role=tooltip
popover tied to it by aria-describedby, which opens on mouse hover, on keyboard focus and on a
tap, closes on Escape, on leaving it and on a tap outside, stays inside a 390 px screen without
side scrolling, shows the detail behind the number, and is in 繁體中文 when the page is. A link
tile only opens its popover on a tap, and still follows the link on a click. On the Artifact page
the operator popover says so.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_header_tips.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a stubbed claude.ai
viewer; never touches ~/.agent-switchboard.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PAGE = "file://" + os.path.join(REPO, "artifact", "board.html")
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


# box id -> (popover id, text the English popover shows)
TIPS = {
    "demo-tag": ("demo-pop", "Sample data"),
    "op-pill": ("op-pop", "Last heartbeat:"),
    "away-pill": ("away-pop", "Pending consensus:"),
    "notif-pill": ("notif-pop", "Slack · muted"),
    "needs-tile": ("needs-pop", "Oldest:"),
    "mt-working": ("working-pop", "DOCS"),
    "mt-done": ("done-pop", "Sessions:"),
    "mt-offline": ("offline-pop", "last seen"),
    "usage": ("usage-pop", "5-hour window:"),
    "sys": ("sys-pop", "Memory:"),
}
STATE_JS = """(box) => { const b = document.getElementById(box), id = b.getAttribute('aria-describedby');
  const p = id && document.getElementById(id); if (!p) return { id };
  const r = p.getBoundingClientRect();
  return { id, role: p.getAttribute('role'), open: !p.hidden && r.width > 0, text: p.innerText, left: r.left, right: r.right,
    vw: document.documentElement.clientWidth, sw: document.scrollingElement.scrollWidth,
    others: [...document.querySelectorAll('[role="tooltip"]')].filter((x) => x !== p && !x.hidden).map((x) => x.id) }; }"""
CLAUDE_STUB = """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const query = () => { const q = { orderBy: () => q, limit: () => q, onSnapshot(cb) { setTimeout(() => cb(snap([])), 0); return () => {}; } }; return q; };
  const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now() - 60000, approver_ids: ['u_x'] } : null;
    setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
            print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
            return 0
        errors = []

        def new_page(init=None, **kw):
            ctx = browser.new_context(**dict({"viewport": {"width": 1280, "height": 900}}, **kw))
            if init:
                ctx.add_init_script(init)
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
            return pg

        st = lambda pg, box: pg.evaluate(STATE_JS, box)  # noqa: E731

        print("demo, mouse and keyboard:")
        pg = new_page()
        pg.goto(PAGE + "?demo=1")
        pg.wait_for_selector("#needs-tile")
        for box, (pid, want) in TIPS.items():
            pg.mouse.move(5, 880)
            pg.hover("#" + box)
            s = st(pg, box)
            check("%s: hover opens #%s (role=tooltip, aria-describedby)" % (box, pid),
                  s.get("id") == pid and s.get("role") == "tooltip" and s.get("open") and not s.get("others"), s)
            check("%s: shows the detail (%r)" % (box, want), want in (s.get("text") or ""), s.get("text"))
            pg.mouse.move(5, 880)
            pg.wait_for_timeout(250)
            check("%s: leaving closes it" % box, not st(pg, box)["open"])
            pg.focus("#" + box)
            check("%s: focus opens it" % box, st(pg, box)["open"])
            pg.keyboard.press("Escape")
            check("%s: Escape closes it" % box, not st(pg, box)["open"])
            pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        # The needs popover links to the oldest wait; the tile itself still follows its link.
        pg.hover("#needs-tile")
        pg.click("#needs-pop-go")
        pg.wait_for_timeout(300)
        check("the oldest-wait link closes the popover and reveals a card",
              not st(pg, "needs-tile")["open"] and pg.evaluate("() => !!document.querySelector('.linked')"))
        pg.evaluate("() => { history.replaceState(null, '', location.pathname + location.search); scrollTo(0, 0); }")
        pg.click("#needs-tile")
        check("a click on the needs tile still goes to #decisions", pg.evaluate("() => location.hash") == "#decisions")
        pg.close()

        print("demo, touch at 390 px:")
        pg = new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        pg.goto(PAGE + "?demo=1")
        pg.wait_for_selector("#needs-tile")
        for box, (pid, _want) in TIPS.items():
            pg.tap("#" + box)
            pg.wait_for_timeout(50)
            s = st(pg, box)
            check("%s: a tap opens it, inside the screen, no side scroll" % box,
                  s["open"] and s["left"] >= 0 and s["right"] <= s["vw"] and s["sw"] <= s["vw"] and not s["others"], s)
            pg.tap("#" + box)
            pg.wait_for_timeout(50)
            check("%s: a second tap closes it" % box, not st(pg, box)["open"])
        pg.tap("#sys")
        pg.tap("h1")
        check("a tap outside closes it", not st(pg, "sys")["open"])
        check("a tap on a link tile does not follow the link", pg.evaluate("() => location.hash") == "")

        print("繁體中文:")
        pg.tap("#acct-btn")
        pg.tap('label:has(input[name="sb-lang"][value="zh-TW"])')
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(100)
        cjk = re.compile(r"[一-鿿]")
        english = ("Oldest:", "Waiting:", "Sessions:", "Memory:", "Last heartbeat:", "Pending consensus:", "Sample data", "Updated:")
        for box, (pid, _want) in TIPS.items():
            pg.tap("#" + box)
            pg.wait_for_timeout(50)
            s = st(pg, box)
            text = s.get("text") or ""
            check("%s: popover in 繁體中文, inside the screen" % box,
                  s["open"] and cjk.search(text) and not any(e in text for e in english) and s["right"] <= s["vw"] and s["sw"] <= s["vw"], text)
            pg.tap("h1")
        pg.close()

        print("Artifact page:")
        pg = new_page(CLAUDE_STUB)
        pg.goto(PAGE)
        pg.wait_for_selector("#op-pill.live", timeout=5000)
        pg.hover("#op-pill")
        s = st(pg, "op-pill")
        check("operator popover: Artifact mode and heartbeat", s["open"] and "claude.ai Artifact" in s["text"] and "1m" in s["text"], s)
        check("no away or notification pill without that data",
              pg.evaluate("() => document.getElementById('away-pill').hidden && document.getElementById('notif-pill').hidden"))
        pg.hover("#needs-tile")
        check("needs popover with nothing waiting", "Nothing is waiting." in st(pg, "needs-tile")["text"])
        pg.close()

        check("no page errors", not errors, errors)
        browser.close()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
