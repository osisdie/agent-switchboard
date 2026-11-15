#!/usr/bin/env python3
"""tests/test_page_header_tips.py — optional headless check of the header popovers in
artifact/board.html: every tile, meter and pill in the header (demo tag, operator, owner away,
notifications, needs you, working, done, offline, Claude usage, system) has a role=tooltip
popover tied to it by aria-describedby (a role=group disclosure with aria-expanded and
aria-controls when it holds buttons), which opens on mouse hover, on keyboard focus and on a
tap, closes on Escape, on leaving it and on a tap outside, stays inside a 390 px screen without
side scrolling, shows the detail behind the number, and is in 繁體中文 when the page is. A link
tile only opens its popover on a tap (its popover links on), and still follows the link on a
click or Enter. Escape with the focus inside a popover puts it back on the box. No popover shows a
session card's own text (what it is doing, its prompt). On the Artifact page the operator
popover says so. The System tile says why its level is what it is: the cause on the level pill,
the row that drives it highlighted with a one-line reason, a since-boot OOM count neutral and only
a recent OOM kill alarming (en / 繁體中文, dark theme, 390 px).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_header_tips.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a stubbed claude.ai
viewer; never touches ~/.agent-switchboard.
"""
import json
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


# box id -> (popover id, text the English popover shows, holds buttons)
TIPS = {
    "demo-tag": ("demo-pop", "Sample data", False),
    "op-pill": ("op-pop", "Last heartbeat:", False),
    "away-pill": ("away-pop", "Pending consensus:", True),
    "notif-pill": ("notif-pop", "Slack · muted", True),
    "needs-tile": ("needs-pop", "Oldest:", True),
    "mt-working": ("working-pop", "DOCS", False),
    "mt-done": ("done-pop", "Sessions:", False),
    "mt-offline": ("offline-pop", "last seen", False),
    "usage": ("usage-pop", "5-hour window:", False),
    "sys": ("sys-pop", "Memory:", True),
}
# The demo session cards' own text: what a session is doing and its prompt never go in a popover.
CARD_TEXT = ("The migration is ready", "Add cursor pagination", "Two teammates are drafting", "Write the v2 upgrade guide")
STATE_JS = """(box) => { const b = document.getElementById(box);
  const id = b.getAttribute('aria-describedby') || b.getAttribute('aria-controls');
  const p = id && document.getElementById(id); if (!p) return { id };
  const r = p.getBoundingClientRect();
  return { id, role: p.getAttribute('role'), open: !p.hidden && r.width > 0, text: p.innerText, left: r.left, right: r.right,
    described: b.getAttribute('aria-describedby'), expanded: b.getAttribute('aria-expanded'), controls: b.getAttribute('aria-controls'),
    labelledby: p.getAttribute('aria-labelledby'), buttons: p.querySelectorAll('button, a, input').length,
    vw: document.documentElement.clientWidth, sw: document.scrollingElement.scrollWidth,
    others: [...document.querySelectorAll('.u-pop')].filter((x) => x !== p && !x.hidden).map((x) => x.id) }; }"""
CLAUDE_STUB = """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const query = () => { const q = { orderBy: () => q, limit: () => q, onSnapshot(cb) { setTimeout(() => cb(snap([])), 0); return () => {}; } }; return q; };
  const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now() - 60000, approver_ids: ['u_x'] } : null;
    setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""


# meta/resources as the operator mirrors it: load critical (6.93 per cpu), memory fine, 2 OOM kills
# since boot, none recent.
RES_LOAD = {"enabled": True, "platform": "linux", "level": "critical", "worst": "load",
            "levels": {"mem": "ok", "swap": "ok", "load": "critical", "disk:disk1": "ok"},
            "cpu": {"pct": 95, "ncpu": 4, "load": [27.72, 20.1, 15.3], "load_per_cpu": 6.93},
            "mem": {"available_pct": 35, "available_mb": 5800, "total_mb": 16000},
            "swap": {"used_pct": 5}, "oom_kills": 2, "oom_recent": False,
            "disks": [{"id": "disk1", "free_pct": 40, "free_mb": 50000, "labels": ["$BOARD_HOME"]}],
            "sessions": [], "heavy_running": 0, "operator": {"watch": True},
            "thresholds": {"load_per_cpu": [1.5, 3.0], "mem_available_pct": [15, 8]}}
RES_OOM = dict(RES_LOAD, worst="oom", oom_kills=3, oom_recent=True, levels=dict(RES_LOAD["levels"], oom="critical"))
# The level is held while the value is already back under it (hysteresis): easing, not a contradiction.
RES_EASE = dict(RES_LOAD, cpu=dict(RES_LOAD["cpu"], load=[11.2, 14.0, 15.0], load_per_cpu=2.8))
RES_MEM_EASE = dict(RES_LOAD, level="high", worst="mem", levels=dict(RES_LOAD["levels"], mem="high", load="ok"),
                    mem={"available_pct": 10, "available_mb": 1600, "total_mb": 16000})
# Thresholds that are not two finite numbers: the defaults are used, nothing of them is shown.
RES_BAD_THR = dict(RES_LOAD, thresholds={"load_per_cpu": ["<b>x</b>", None], "mem_available_pct": "8"})
SYS_JS = """() => { const t = document.getElementById('sys'), o = document.getElementById('sys-oom'), w = document.getElementById('sys-why');
  const ld = document.getElementById('sys-load'), clear = (c) => c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
  return { pill: document.getElementById('sys-lvl').textContent, why: w.hidden ? null : w.textContent,
    load: ld ? { drive: ld.classList.contains('drive'), weight: getComputedStyle(ld).fontWeight, color: getComputedStyle(ld).color } : null,
    memDrive: document.getElementById('sys-mem').classList.contains('drive'), lineColor: getComputedStyle(document.getElementById('sys-line')).color,
    oom: { text: o.textContent, label: o.getAttribute('aria-label'), hot: o.classList.contains('hot'), filled: !clear(getComputedStyle(o).backgroundColor) },
    vw: document.documentElement.clientWidth, sw: document.scrollingElement.scrollWidth, right: t.getBoundingClientRect().right }; }"""


def res_stub(res):
    return CLAUDE_STUB.replace("const d = path === 'meta/operator' ? { heartbeat_at: Date.now() - 60000, approver_ids: ['u_x'] } : null;",
                               "const d = path === 'meta/operator' ? { heartbeat_at: Date.now() - 60000, approver_ids: ['u_x'] }"
                               " : path === 'meta/resources' ? Object.assign(%s, { at: Date.now() - 5000 }) : null;" % json.dumps(res))


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
        seen = []
        for box, (pid, want, inter) in TIPS.items():
            pg.mouse.move(5, 880)
            pg.hover("#" + box)
            s = st(pg, box)
            if inter:
                check("%s: hover opens #%s, a disclosure (role=group, aria-expanded, aria-controls, no tooltip)" % (box, pid),
                      s.get("id") == pid and s.get("role") == "group" and s.get("expanded") == "true" and s.get("controls") == pid
                      and s.get("labelledby") == box and not s.get("described") and s.get("buttons") > 0 and s.get("open") and not s.get("others"), s)
            else:
                check("%s: hover opens #%s, a text-only tooltip (role=tooltip, aria-describedby)" % (box, pid),
                      s.get("id") == pid and s.get("role") == "tooltip" and s.get("described") == pid and s.get("expanded") is None
                      and s.get("buttons") == 0 and s.get("open") and not s.get("others"), s)
            check("%s: shows the detail (%r)" % (box, want), want in (s.get("text") or ""), s.get("text"))
            seen.append(s.get("text") or "")
            pg.mouse.move(5, 880)
            pg.wait_for_timeout(250)
            check("%s: leaving closes it" % box, not st(pg, box)["open"] and st(pg, box).get("expanded") in (None, "false"))
            pg.focus("#" + box)
            check("%s: focus opens it" % box, st(pg, box)["open"])
            pg.keyboard.press("Escape")
            check("%s: Escape closes it" % box, not st(pg, box)["open"])
            pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        check("no popover shows a session card's own text (doing, prompt)",
              not any(c in t for t in seen for c in CARD_TEXT), [c for t in seen for c in CARD_TEXT if c in t])
        check("notification pill named for screen readers", pg.get_attribute("#notif-pill", "aria-label") == "Notifications: 1 muted",
              pg.get_attribute("#notif-pill", "aria-label"))
        # Escape with the focus inside a popover: closed, focus back on its box, not reopened.
        pg.focus("#needs-tile")
        pg.focus("#needs-pop-go")
        pg.keyboard.press("Escape")
        check("Escape inside a popover: closed, focus back on its box",
              not st(pg, "needs-tile")["open"] and pg.evaluate("() => document.activeElement.id") == "needs-tile", st(pg, "needs-tile"))
        pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        # Moved on resize: opened by focus at 1280, narrowed to 420.
        pg.focus("#sys")
        pg.set_viewport_size({"width": 420, "height": 900})
        pg.wait_for_timeout(100)
        s = st(pg, "sys")
        check("an open popover is placed again on resize", s["open"] and s["left"] >= 0 and s["right"] <= s["vw"], s)
        pg.set_viewport_size({"width": 1280, "height": 900})
        pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        pg.mouse.move(5, 880)
        pg.wait_for_timeout(200)
        # Enter on the link tiles still follows their links.
        for box, hsh in (("needs-tile", "#decisions"), ("sys", "#resources")):
            pg.evaluate("() => { history.replaceState(null, '', location.pathname + location.search); scrollTo(0, 0); }")
            pg.focus("#" + box)
            pg.keyboard.press("Enter")
            check("Enter on #%s still goes to %s" % (box, hsh), pg.evaluate("() => location.hash") == hsh)
            pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        pg.mouse.move(5, 880)
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
        for box, (pid, _want, _inter) in TIPS.items():
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
        # A slow tap (finger down 600 ms) opens the popover and still does not follow the link.
        pg.evaluate("""async () => { const b = document.getElementById('sys'), o = { pointerType: 'touch', bubbles: true, isPrimary: true };
          b.dispatchEvent(new PointerEvent('pointerdown', o)); await new Promise((r) => setTimeout(r, 600));
          b.dispatchEvent(new PointerEvent('pointerup', o)); b.click(); }""")
        check("a slow tap opens the popover and does not follow the link",
              st(pg, "sys")["open"] and pg.evaluate("() => location.hash") == "", pg.evaluate("() => location.hash"))
        pg.tap("h1")
        # ...its popover links on: System -> Resources, Needs you -> Decisions.
        for box, link, head in (("sys", "#sys-pop-sec", "h-resources"), ("needs-tile", "#needs-pop-sec", "h-decisions")):
            pg.evaluate("() => scrollTo(0, 0)")
            pg.tap("#" + box)
            pg.wait_for_timeout(50)
            pg.tap(link)
            pg.wait_for_timeout(300)
            check("tap %s, then its %s link: popover closed, at the section" % (box, link),
                  not st(pg, box)["open"] and pg.evaluate("() => document.activeElement.id") == head, pg.evaluate("() => document.activeElement.id"))

        print("繁體中文:")
        pg.tap("#acct-btn")
        pg.tap('label:has(input[name="sb-lang"][value="zh-TW"])')
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(100)
        cjk = re.compile(r"[一-鿿]")
        english = ("Oldest:", "Waiting:", "Sessions:", "Memory:", "Last heartbeat:", "Pending consensus:", "Sample data", "Updated:",
                   "Resources →", "Decisions →")
        check("notification pill name in 繁體中文", (pg.get_attribute("#notif-pill", "aria-label") or "").startswith("通知："),
              pg.get_attribute("#notif-pill", "aria-label"))
        for box, (pid, _want, _inter) in TIPS.items():
            pg.evaluate("() => scrollTo(0, 0)")
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
        pg.click("#needs-pop-sec")
        pg.wait_for_timeout(300)
        check("...still links on to Decisions", not st(pg, "needs-tile")["open"] and pg.evaluate("() => document.activeElement.id") == "h-decisions")
        pg.close()

        print("System tile says why:")
        for scheme, vp, lang in (("light", {"width": 1280, "height": 900}, "en"), ("dark", {"width": 390, "height": 844}, "zh-TW")):
            for res in (RES_LOAD, RES_OOM):
                pg = new_page(res_stub(res), viewport=vp, color_scheme=scheme, has_touch=vp["width"] < 400)
                pg.goto(PAGE)
                pg.wait_for_selector("#sys:not([hidden])", timeout=5000)
                if lang == "zh-TW":
                    pg.click("#acct-btn")
                    pg.click('label:has(input[name="sb-lang"][value="zh-TW"])')
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(100)
                y = pg.evaluate(SYS_JS)
                tag = "%s, %s, %dpx, %s" % (lang, scheme, vp["width"], "load" if res is RES_LOAD else "recent OOM")
                zh = lang == "zh-TW"
                if res is RES_LOAD:
                    check("%s: the level pill names the cause" % tag, y["pill"] == ("危急 · CPU 負載" if zh else "critical · CPU load"), y["pill"])
                    check("%s: the Load row drives it (highlighted), memory does not" % tag,
                          y["load"]["drive"] and int(y["load"]["weight"]) >= 700 and y["load"]["color"] != y["lineColor"] and not y["memDrive"], y)
                    check("%s: one line why, per cpu against the threshold" % tag, y["why"] == (
                        "負載 27.7 / 4 cpu = 每顆 cpu 6.93（高，門檻 ≥ 3.0）" if zh else "Load 27.7 / 4 cpu = 6.93 per cpu (high ≥ 3.0)"), y["why"])
                    check("%s: a since-boot OOM count is neutral and says so" % tag, not y["oom"]["hot"] and not y["oom"]["filled"]
                          and y["oom"]["text"] == ("開機以來 OOM 2" if zh else "OOM 2 since boot")
                          and y["oom"]["label"] == ("開機以來 2 次 OOM kill" if zh else "2 OOM kills since boot"), y["oom"])
                else:
                    check("%s: a recent OOM kill alarms" % tag, y["oom"]["hot"] and y["oom"]["filled"] and y["oom"]["text"] == "OOM 3", y["oom"])
                    check("%s: ...and is the reason" % tag, y["pill"] == ("危急 · OOM kill" if zh else "critical · OOM kill")
                          and y["why"] == ("最近 10 分鐘內有 OOM kill" if zh else "an OOM kill in the last 10 minutes") and not y["load"]["drive"], y)
                check("%s: no side scroll, tile on screen" % tag, y["sw"] <= y["vw"] and y["right"] <= y["vw"], y)
                if vp["width"] < 400:
                    pg.tap("#sys")
                else:
                    pg.hover("#sys")
                pg.wait_for_timeout(100)
                t = st(pg, "sys")
                first = (t.get("text") or "").split("\n")[0]
                if res is RES_LOAD:
                    want = ("原因：負載 27.7 / 4 cpu = 每顆 cpu 6.93（高，門檻 ≥ 3.0）；連續 2 次取樣都在 high" if zh
                            else "Why: Load 27.7 / 4 cpu = 6.93 per cpu (high ≥ 3.0); high for 2 samples in a row")
                else:
                    want = "原因：最近 10 分鐘內有 OOM kill" if zh else "Why: an OOM kill in the last 10 minutes"
                check("%s: the popover starts with why" % tag, first == want, first)
                check("%s: ...and says what the levels mean" % tag, ("等級：" if zh else "Levels: warn and high") in t["text"]
                      and t["right"] <= t["vw"] and t["sw"] <= t["vw"], t)
                pg.close()

        print("System reason: easing, bad thresholds:")
        for res, lang, want in ((RES_EASE, "en", "Load 11.2 / 4 cpu = 2.80 per cpu (high from ≥ 3.0, easing)"),
                                (RES_EASE, "zh-TW", "負載 11.2 / 4 cpu = 每顆 cpu 2.80（高，門檻 ≥ 3.0，正在回落）"),
                                (RES_MEM_EASE, "en", "Memory 10% available (high from ≤ 8%, easing)"),
                                (RES_LOAD, "en", "Load 27.7 / 4 cpu = 6.93 per cpu (high ≥ 3.0)"),
                                (RES_BAD_THR, "en", "Load 27.7 / 4 cpu = 6.93 per cpu (high ≥ 3.0)")):
            pg = new_page(res_stub(res))
            pg.goto(PAGE)
            pg.wait_for_selector("#sys:not([hidden])", timeout=5000)
            if lang == "zh-TW":
                pg.click("#acct-btn")
                pg.click('label:has(input[name="sb-lang"][value="zh-TW"])')
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(100)
            y = pg.evaluate(SYS_JS)
            pg.hover("#sys")
            pg.wait_for_timeout(100)
            t = st(pg, "sys")["text"] or ""
            label = "bad thresholds" if res is RES_BAD_THR else "held level" if res is not RES_LOAD else "past the threshold"
            check("%s (%s): %s" % (label, lang, want), y["why"] == want and want in t, y["why"])
            if res is RES_BAD_THR:
                check("bad thresholds: no NaN, no markup shown", "NaN" not in t + y["why"] and "<b>" not in t + pg.content().split("<script")[0], t)
            pg.close()

        print("Deck SYSTEM key: a since-boot OOM count is neutral:")
        for res, hot in ((RES_LOAD, False), (RES_OOM, True)):
            pg = new_page(res_stub(res))
            pg.goto(PAGE)
            pg.wait_for_selector(".dkey .k-oom", timeout=5000)
            k = pg.evaluate("""() => { const o = document.querySelector('.dkey .k-oom'), c = getComputedStyle(o).backgroundColor;
              return { hot: o.classList.contains('hot'), filled: !(c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c)), title: o.title }; }""")
            check("deck OOM %s: %s" % ("recent" if hot else "since boot", "filled" if hot else "outlined, not filled"),
                  k["hot"] == hot and k["filled"] == hot and k["title"] == ("3 OOM kills since boot, one in the last 10 minutes" if hot else "2 OOM kills since boot"), k)
            pg.close()

        check("no page errors", not errors, errors)
        browser.close()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
