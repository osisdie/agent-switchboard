#!/usr/bin/env python3
"""tests/test_page_mnav.py — optional headless check of finding your way around artifact/board.html
on a phone (demo data):

  - 390 and 360 px wide: the bar with "Sections" is at the top from the first screen, 44 px tall
    or more, with the needs-you count; it opens a sheet over the page listing every section with
    its count; tapping one closes it and the heading lands below the bar; Escape and a tap
    outside close it; nothing past the right edge;
  - "↑" appears once you scroll down, and takes you back to the top (also "Top" in the sheet);
  - each section folds from a button beside its heading; the fold survives a reload, works with
    storage blocked, and a jump to a folded section opens it; "Collapse all sections" leaves
    Decisions open while something waits, "Expand all sections" opens everything; Decisions with
    pending items open again on load even if folded before;
  - long cards: a task's stage rail shows only its current stage with "Show all stages", a long
    decision context is clamped with "Show more";
  - zh-TW strings; the dark theme gives the sheet a solid background;
  - desktop (1280 px): the row of links is unchanged, no menu button, no "↑", no sheet tools.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_mnav.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_json(path, data):
    """Write atomically: board-serve polls these files and must never read half of one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(path + ".tmp", path)


def serve(tmp):
    """board-serve on a scratch BOARD_HOME (approver: local); returns (proc, url, home)."""
    home, cfg = os.path.join(tmp, "home"), os.path.join(tmp, "config")
    for d in (home, cfg, os.path.join(tmp, "sessions"), os.path.join(home, "decisions")):
        os.makedirs(d, exist_ok=True)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=os.path.join(tmp, "sessions"), ALERT_DESKTOP="0")
    for k in [x for x in env if x.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) and x != "ALERT_DESKTOP"] + ["TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"]:
        env.pop(k, None)
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d/" % port, home


def session(code, state, now):
    return {"code": code, "aliases": [code], "name": code, "state": state, "status": "waiting" if state == "needs-you" else "busy",
            "updated_at": now, "title": "work", "repo": "app", "branch": "main", "recent": [], "pending": None}


def decision(home, did, frm, status, now):
    write_json(os.path.join(home, "decisions", did + ".json"),
               {"id": did, "kind": "question", "from": frm, "question": "Ship %s?" % did, "options": ["yes", "no"],
                "status": status, "answer": None, "created_at": now - 60000, "updated_at": now})


def settle(pg):
    """Wait for a smooth scroll to finish (the page scrolls smoothly; a long jump takes a while)."""
    last = None
    for _ in range(40):
        pg.wait_for_timeout(150)
        y = pg.evaluate("() => scrollY")
        if y == last:
            return
        last = y


BAR_JS = """() => { const r = (el) => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom, left: b.left, right: b.right, h: b.height, w: b.width }; };
  const t = document.getElementById('nav-toggle'), n = document.getElementById('nav-toggle-n'), sheet = document.getElementById('nav-sheet');
  return { toggle: r(t), shown: getComputedStyle(t).display !== 'none', text: t.textContent, badge: n.hidden ? null : n.textContent,
    needs: document.getElementById('needs-n').textContent, nav: r(document.getElementById('secnav')),
    sheet: getComputedStyle(sheet).display, sheetBox: r(sheet), sheetBg: getComputedStyle(sheet).backgroundColor,
    open: document.getElementById('secnav').classList.contains('open'), expanded: t.getAttribute('aria-expanded'),
    sw: document.scrollingElement.scrollWidth, vw: innerWidth, vh: innerHeight, y: scrollY }; }"""
LINKS_JS = """() => [...document.querySelectorAll('#nav-list li')].filter((li) => !li.hidden).map((li) => { const a = li.querySelector('a'), b = a.getBoundingClientRect(), badge = a.querySelector('.badge');
  return { href: a.getAttribute('href'), badge: badge ? badge.textContent : null, h: b.height, right: b.right }; })"""
FOLD_JS = """() => [...document.querySelectorAll('section.sec')].filter((s) => !s.hidden).map((s) => { const b = s.querySelector('.sec-head .sec-fold');
  return { id: s.id, folded: s.classList.contains('sec-folded'), btn: !!b, exp: b && b.getAttribute('aria-expanded'), label: b && b.getAttribute('aria-label'),
    h: Math.round(s.getBoundingClientRect().height), bh: b ? b.getBoundingClientRect().height : 0 }; })"""
TOP_JS = """() => { const b = document.getElementById('to-top'), r = b.getBoundingClientRect();
  return { hidden: b.hidden || getComputedStyle(b).display === 'none', w: r.width, h: r.height, right: r.right, bottom: r.bottom, vw: innerWidth, vh: innerHeight, y: scrollY, label: b.getAttribute('aria-label') }; }"""


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - no Chromium: skip, never block
            print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
            return 0
        errors = []

        def new_page(init=None, width=390, height=844, ctx=None):
            if ctx is None:
                ctx = browser.new_context(viewport={"width": width, "height": height})
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                if init:
                    ctx.add_init_script(init)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
            pg.goto(PAGE)
            pg.wait_for_timeout(600)
            return pg

        for w, hgt in ((390, 844), (360, 740)):
            print("phone %d px: the bar and the sheet:" % w)
            pg = new_page(width=w, height=hgt)
            r = pg.evaluate(BAR_JS)
            check("the Sections button is on the first screen, at the top", r["shown"] and r["toggle"]["top"] >= 0 and r["toggle"]["bottom"] <= 80, r)
            check("  it is a big enough tap target (44 px)", r["toggle"]["h"] >= 44, r["toggle"])
            check("  it says Sections and carries the needs-you count", "Sections" in r["text"] and r["badge"] == r["needs"] and r["needs"] != "0", r)
            check("  closed: no sheet, nothing past the edge", r["sheet"] == "none" and r["expanded"] == "false" and r["sw"] <= r["vw"], r)
            navh = r["nav"]["h"]
            pg.click("#nav-toggle")
            r = pg.evaluate(BAR_JS)
            links = pg.evaluate(LINKS_JS)
            check("open: the sheet shows, inside the screen, the bar's height unchanged",
                  r["open"] and r["expanded"] == "true" and r["sheet"] != "none" and r["sheetBox"]["left"] >= 0 and r["sheetBox"]["right"] <= r["vw"]
                  and r["sheetBox"]["bottom"] <= r["vh"] and abs(r["nav"]["h"] - navh) < 1 and r["sw"] <= r["vw"], r)
            hrefs = [x["href"] for x in links]
            check("  every section listed, Messages among them, each with its count",
                  all(x in hrefs for x in ("#decisions", "#lifecycle", "#board", "#messages", "#approvals"))
                  and all(x["badge"] not in (None, "") for x in links if x["href"] != "#tickets"), links)
            check("  rows are 44 px tap targets and fit", all(x["h"] >= 44 and x["right"] <= w for x in links), links)
            tools = pg.evaluate("() => ['nav-top', 'nav-fold-all', 'nav-unfold-all'].map((id) => { const b = document.getElementById(id).getBoundingClientRect(); return [b.width > 0, b.height, b.bottom]; })")
            check("  Top, Collapse all, Expand all in the sheet, in view without scrolling it", all(v and hh >= 44 and bot <= r["vh"] for v, hh, bot in tools), tools)
            pg.click("#nav-list a[href='#messages']")
            settle(pg)
            r = pg.evaluate("""() => ({ open: document.getElementById('secnav').classList.contains('open'), head: document.getElementById('h-messages').getBoundingClientRect().top,
              nav: document.getElementById('secnav').getBoundingClientRect().bottom, cur: (document.querySelector('#nav-list a[aria-current="true"]') || {}).hash })""")
            check("tap Messages: the sheet closes, the heading lands just below the bar", not r["open"] and r["nav"] - 1 <= r["head"] <= r["nav"] + 60, r)
            check("  Messages is the current section", r["cur"] == "#messages", r)
            pg.click("#nav-toggle")
            pg.keyboard.press("Escape")
            r = pg.evaluate("() => ({ open: document.getElementById('secnav').classList.contains('open'), focus: document.activeElement.id })")
            check("Escape closes the sheet and focus returns to the button", not r["open"] and r["focus"] == "nav-toggle", r)
            # A decision's answer button lies under the open sheet's backdrop: the tap only closes.
            pg.evaluate("""() => { const b = document.querySelector('#dec-d-2-demo .opts button'); document.documentElement.style.scrollBehavior = 'auto';
              window.scrollBy(0, b.getBoundingClientRect().top - (innerHeight - 60)); document.documentElement.style.scrollBehavior = ''; }""")
            pg.wait_for_timeout(200)
            CARD_JS = """() => { const c = document.getElementById('dec-d-2-demo'), b = c.querySelector('.opts button').getBoundingClientRect();
              return { sig: c.dataset.sig, text: c.innerText, x: b.left + b.width / 2, y: b.top + b.height / 2 }; }"""
            c0 = pg.evaluate(CARD_JS)
            y0 = pg.evaluate("() => scrollY")
            pg.click("#nav-toggle")
            r = pg.evaluate("() => document.getElementById('nav-sheet').getBoundingClientRect().bottom")
            hit = pg.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.id || e.className : null; }", [c0["x"], c0["y"]])
            check("  opening (and focusing) the menu does not scroll the page", pg.evaluate("() => scrollY") == y0, [y0, pg.evaluate("() => scrollY")])
            check("  below the open sheet, a backdrop is what a tap hits", hit == "nav-backdrop", hit)
            pg.mouse.click(c0["x"], c0["y"])
            pg.wait_for_timeout(300)
            c1 = pg.evaluate(CARD_JS)
            check("a tap outside closes it", not pg.evaluate("() => document.getElementById('secnav').classList.contains('open')"))
            check("  and does not press the answer button under it", c0["y"] > r and c1["sig"] == c0["sig"] and c1["text"] == c0["text"], [c0["y"], r, c0["text"][-120:], c1["text"][-120:]])
            pg.mouse.click(c1["x"], c1["y"])
            pg.wait_for_timeout(300)
            check("  (the same tap with the sheet closed does answer it)", pg.evaluate("() => !document.getElementById('dec-d-2-demo')"))
            pg.close()
            pg = new_page(width=w, height=hgt)
            pg.click("#nav-toggle")
            pg.focus("#nav-unfold-all")
            pg.keyboard.press("Tab")
            r = pg.evaluate("() => ({ open: document.getElementById('secnav').classList.contains('open'), inSheet: document.getElementById('nav-sheet').contains(document.activeElement), at: document.activeElement.id })")
            check("Tab out of the sheet closes it", not r["open"] and not r["inSheet"], r)
            pg.click("#nav-toggle")
            pg.click("#nav-unfold-all")
            r = pg.evaluate("() => ({ open: document.getElementById('secnav').classList.contains('open'), focus: document.activeElement.id })")
            check("Expand all closes the sheet and focus returns to the button", not r["open"] and r["focus"] == "nav-toggle", r)

            print("phone %d px: back to the top:" % w)
            pg.evaluate("() => window.scrollTo({ top: 0, behavior: 'instant' })")
            pg.wait_for_timeout(200)
            check("at the top: no ↑ button", pg.evaluate(TOP_JS)["hidden"])
            pg.evaluate("() => window.scrollTo({ top: 4000, behavior: 'instant' })")
            pg.wait_for_timeout(200)
            r = pg.evaluate(TOP_JS)
            check("scrolled down: ↑ shows, 44 px or more, inside the screen", not r["hidden"] and r["w"] >= 44 and r["h"] >= 44 and r["right"] <= r["vw"] and r["bottom"] <= r["vh"], r)
            pg.click("#to-top")
            settle(pg)
            r = pg.evaluate(TOP_JS)
            check("  it goes back to the top and hides", r["y"] == 0 and r["hidden"], r)
            pg.evaluate("() => window.scrollTo({ top: 5000, behavior: 'instant' })")
            pg.click("#nav-toggle")
            pg.click("#nav-top")
            settle(pg)
            check("  Top in the sheet does the same", pg.evaluate("() => scrollY") == 0)
            pg.close()

        print("phone: folding sections:")
        pg = new_page()
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("every section has a fold button, open by default, 44 px on a phone",
              all(x["btn"] and x["exp"] == "true" and not x["folded"] and x["bh"] >= 44 for x in f.values()), f)
        before = f["lifecycle"]["h"]
        pg.click("#lifecycle .sec-fold")
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("folding Lifecycle leaves its heading only", f["lifecycle"]["folded"] and f["lifecycle"]["exp"] == "false" and f["lifecycle"]["h"] < 120 < before
              and "Expand" in f["lifecycle"]["label"], f["lifecycle"])
        r = pg.evaluate("() => { const s = document.querySelector('#lifecycle .sec-sum'); return { shown: getComputedStyle(s).display !== 'none', text: s.textContent, tasks: getComputedStyle(document.getElementById('tasks')).display }; }")
        check("  its count stays beside the heading; the tasks are hidden", r["shown"] and r["text"] == pg.evaluate("() => document.getElementById('nav-life').textContent") and r["tasks"] == "none", r)
        pg.reload()
        pg.wait_for_timeout(600)
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("  still folded after a reload", f["lifecycle"]["folded"] and not f["board"]["folded"], f["lifecycle"])
        pg.click("#nav-toggle")
        pg.click("#nav-list a[href='#lifecycle']")
        settle(pg)
        r = pg.evaluate("() => ({ folded: document.getElementById('lifecycle').classList.contains('sec-folded'), head: document.getElementById('h-lifecycle').getBoundingClientRect().top, nav: document.getElementById('secnav').getBoundingClientRect().bottom })")
        check("  jumping to it from the menu opens it, heading below the bar", not r["folded"] and r["nav"] - 1 <= r["head"] <= r["nav"] + 60, r)
        pg.click("#nav-toggle")
        pg.click("#nav-fold-all")
        settle(pg)
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("Collapse all sections: all folded but Decisions (8 waiting)", not f["decisions"]["folded"] and all(x["folded"] for k, x in f.items() if k != "decisions"), f)
        r = pg.evaluate("() => ({ y: scrollY, h: document.scrollingElement.scrollHeight })")
        check("  the page gets short and goes back to the top", r["y"] == 0 and r["h"] < 9000, r)
        pg.click("#decisions .sec-fold")
        check("  Decisions can still be folded by hand", pg.evaluate("() => document.getElementById('decisions').classList.contains('sec-folded')"))
        pg.reload()
        pg.wait_for_timeout(600)
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("  after a reload, pending decisions open Decisions again; the rest stay folded",
              not f["decisions"]["folded"] and f["board"]["folded"] and f["messages"]["folded"], f)
        pg.click("#nav-toggle")
        pg.click("#nav-unfold-all")
        f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
        check("Expand all sections opens everything", not any(x["folded"] for x in f.values()), f)
        r = pg.evaluate("() => document.querySelector('#needs-tile').getAttribute('href')")
        pg.click("#messages .sec-fold")
        pg.evaluate("() => { location.hash = ''; location.hash = '#messages'; }")
        settle(pg)
        check("a #messages link opens a folded Messages", not pg.evaluate("() => document.getElementById('messages').classList.contains('sec-folded')"))
        pg.click("#lifecycle .sec-fold")
        pg.goto("about:blank")
        pg.goto(PAGE + "#lifecycle")
        pg.wait_for_timeout(800)
        settle(pg)
        r = pg.evaluate("() => ({ folded: document.getElementById('lifecycle').classList.contains('sec-folded'), head: document.getElementById('h-lifecycle').getBoundingClientRect().top, nav: document.getElementById('secnav').getBoundingClientRect().bottom })")
        check("opening the page at #lifecycle (folded before) opens it and lands on it", not r["folded"] and r["nav"] - 1 <= r["head"] <= r["nav"] + 60, r)
        pg.close()

        print("phone: storage blocked:")
        pg = new_page("Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } });")
        pg.click("#board .sec-fold")
        check("folding still works for this page view", pg.evaluate("() => document.getElementById('board').classList.contains('sec-folded')"))
        pg.click("#nav-toggle")
        pg.click("#nav-fold-all")
        check("  Collapse all too", pg.evaluate("() => document.getElementById('messages').classList.contains('sec-folded')"))
        pg.close()

        print("phone: long cards:")
        pg = new_page()
        r = pg.evaluate("""() => [...document.querySelectorAll('#tasks .tcard')].map((c) => ({ h: Math.round(c.getBoundingClientRect().height),
          stages: [...c.querySelectorAll('.rail .stage')].filter((s) => s.getBoundingClientRect().height > 0).length,
          btn: (() => { const b = c.querySelector('.rail-more'); return b ? { text: b.textContent, h: b.getBoundingClientRect().height, exp: b.getAttribute('aria-expanded') } : null; })() }))""")
        check("task cards show only the current stage (at most one), with Show all stages",
              r and all(x["stages"] <= 1 and x["btn"] and "Show all stages" in x["btn"]["text"] and x["btn"]["h"] >= 44 and x["btn"]["exp"] == "false" for x in r), r)
        check("  so a task card is far shorter (under 600 px, about 900 with the whole rail)", all(x["h"] < 600 for x in r), r)
        pg.click("#tasks .tcard:first-child .rail-more")
        r = pg.evaluate("() => { const c = document.querySelector('#tasks .tcard'), b = c.querySelector('.rail-more'); return { stages: [...c.querySelectorAll('.rail .stage')].filter((s) => s.getBoundingClientRect().height > 0).length, text: b.textContent, exp: b.getAttribute('aria-expanded') }; }")
        check("  Show all stages opens all five", r["stages"] == 5 and r["exp"] == "true" and "fewer" in r["text"], r)
        r = pg.evaluate("""() => { const c = document.getElementById('dec-d-2-demo'), p = c.querySelector('.ctx'), b = c.querySelector(':scope > .more');
          return { clamp: p.classList.contains('clamp'), ph: p.getBoundingClientRect().height, sh: p.scrollHeight, btn: b ? b.textContent : null, opts: !!c.querySelector('.opts button') }; }""")
        check("a long decision context is clamped with Show more; the answer buttons stay", r["clamp"] and r["sh"] > r["ph"] and r["btn"] == "Show more" and r["opts"], r)
        pg.click("#dec-d-2-demo > .more")
        r = pg.evaluate("() => { const p = document.querySelector('#dec-d-2-demo .ctx'); return { clamp: p.classList.contains('clamp'), text: document.querySelector('#dec-d-2-demo > .more').textContent }; }")
        check("  Show more opens it", not r["clamp"] and r["text"] == "Show less", r)
        pg.focus("#dec-d-2-demo > .more")
        pg.evaluate("() => window.dispatchEvent(new StorageEvent('storage', { key: 'switchboard.lang', newValue: 'zh-TW' }))")
        pg.wait_for_timeout(300)
        r = pg.evaluate("() => ({ focus: document.activeElement.dataset.focus || document.activeElement.id || document.activeElement.tagName, text: document.activeElement.textContent, rebuilt: !document.querySelector('#dec-d-2-demo > .more[data-focus]') ? null : true })")
        check("  a re-render (language switch) keeps the keyboard focus on it", r["focus"] == "more:ctx:d-2-demo" and r["text"] == "收起", r)
        pg.close()

        print("phone: zh-TW and dark:")
        pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); localStorage.setItem('switchboard.theme', 'dark'); } catch (e) {}")
        r = pg.evaluate(BAR_JS)
        check("the button reads 區段", "區段" in r["text"], r["text"])
        pg.click("#nav-toggle")
        r = pg.evaluate("""() => ({ fold: document.getElementById('nav-fold-all').textContent + '|' + document.getElementById('nav-fold-all').getAttribute('aria-label'),
          unfold: document.getElementById('nav-unfold-all').textContent + '|' + document.getElementById('nav-unfold-all').getAttribute('aria-label'),
          top: document.getElementById('nav-top').textContent, up: document.getElementById('to-top').getAttribute('aria-label'),
          sec: document.querySelector('#board .sec-fold').getAttribute('aria-label'), rail: document.querySelector('.rail-more').textContent,
          bg: getComputedStyle(document.getElementById('nav-sheet')).backgroundColor, panel: getComputedStyle(document.documentElement).getPropertyValue('--panel').trim() })""")
        check("  sheet tools, ↑, fold buttons and the rail button in zh-TW",
              r["fold"] == "全部收合|收合所有區段" and r["unfold"] == "全部展開|展開所有區段" and "回到頂端" in r["top"] and r["up"] == "回到頂端"
              and r["sec"] == "收合「看板」區段" and "顯示全部階段" in r["rail"], r)
        check("  dark theme: the sheet is solid (the panel colour)", r["bg"] not in ("rgba(0, 0, 0, 0)", "transparent") and r["panel"] == "#161d25", r)
        pg.close()

        print("phone landscape 800x360: the keyboard through a sheet that scrolls:")
        pg = new_page(width=800, height=360)
        pg.click("#nav-toggle")
        r = pg.evaluate("() => { const s = document.getElementById('nav-sheet'); return { sh: s.scrollHeight, ch: s.clientHeight, y: scrollY }; }")
        check("the sheet is taller than the screen allows, so it scrolls", r["sh"] > r["ch"], r)
        SEEN_JS = """() => { const s = document.getElementById('nav-sheet').getBoundingClientRect(), a = document.activeElement, b = a.getBoundingClientRect();
          return { id: a.id || a.getAttribute('href'), inSheet: document.getElementById('nav-sheet').contains(a), seen: b.top >= s.top - 1 && b.bottom <= s.bottom + 1 }; }"""
        pg.focus("#nav-unfold-all")
        y0 = pg.evaluate("() => scrollY")
        bad, steps = [], 0
        for _ in range(14):
            pg.keyboard.press("Shift+Tab")
            pg.wait_for_timeout(60)
            v = pg.evaluate(SEEN_JS)
            if v["id"] == "nav-toggle":
                break
            steps += 1
            if not (v["inSheet"] and v["seen"]):
                bad.append(v)
        check("  Shift+Tab up through every row: each focused row is inside the sheet's visible area", steps >= 10 and not bad, [steps, bad[:3]])
        pg.focus("#nav-list a[href='#decisions']")
        for _ in range(14):
            pg.keyboard.press("Tab")
            pg.wait_for_timeout(60)
            v = pg.evaluate(SEEN_JS)
            if not v["inSheet"]:
                break
            if not v["seen"]:
                bad.append(v)
        check("  Tab down through it too", not bad, bad[:3])
        check("  and the page itself never scrolls", pg.evaluate("() => scrollY") == y0)
        pg.close()

        print("desktop 1280 px:")
        pg = new_page(width=1280, height=900)
        r = pg.evaluate("""() => ({ toggle: getComputedStyle(document.getElementById('nav-toggle')).display, list: getComputedStyle(document.getElementById('nav-list')).display,
          tools: getComputedStyle(document.getElementById('nav-tools')).display, navTop: document.getElementById('secnav').getBoundingClientRect().top,
          deck: document.getElementById('deck').getBoundingClientRect().bottom, rail: getComputedStyle(document.querySelector('.rail-more')).display,
          stages: [...document.querySelectorAll('#tasks .tcard:first-child .rail .stage')].filter((s) => s.getBoundingClientRect().height > 0).length,
          links: [...document.querySelectorAll('#nav-list a')].filter((a) => a.getBoundingClientRect().height > 0).length,
          ctx: document.querySelector('#dec-d-2-demo .ctx').getBoundingClientRect().height, ctxFull: document.querySelector('#dec-d-2-demo .ctx').scrollHeight,
          more: getComputedStyle(document.querySelector('#dec-d-2-demo > .more')).display })""")
        check("the row of links as before: no menu button, no sheet tools, below the Deck",
              r["toggle"] == "none" and r["list"] == "flex" and r["tools"] == "none" and r["navTop"] > r["deck"] and r["links"] >= 8, r)
        check("  task rails show all five stages, no Show all stages", r["rail"] == "none" and r["stages"] == 5, r)
        check("  a long decision context shows in full, no Show more", r["ctx"] >= r["ctxFull"] - 1 and r["more"] == "none", r)
        pg.evaluate("() => window.scrollTo({ top: 3000, behavior: 'instant' })")
        pg.wait_for_timeout(200)
        check("  no floating ↑ on desktop", pg.evaluate(TOP_JS)["hidden"])
        pg.close()

        print("board-serve, phone: what waits on you is never folded away:")
        tmp = tempfile.mkdtemp(prefix="switchboard-mnav.")
        srv = None
        try:
            srv, url, home = serve(tmp)
            now = int(time.time() * 1000)
            # WAIT needs you without a board decision (it lives only in Board); d-1 waits in Decisions.
            write_json(os.path.join(home, "snapshot.json"), {"generated_at": now, "decisions": [], "tasks": [], "sessions": [
                session("WAIT", "needs-you", now), session("API", "working", now), session("WEB", "working", now)]})
            decision(home, "d-1", "API", "pending", now)
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(url)
            pg.wait_for_function("() => document.getElementById('nav-toggle-n').textContent === '2' && !!document.getElementById('dec-d-1')", timeout=15000)
            pg.click("#nav-toggle")
            pg.click("#nav-fold-all")
            f = {x["id"]: x for x in pg.evaluate(FOLD_JS)}
            check("Collapse all keeps Board open while a session there needs you (and Decisions)",
                  not f["board"]["folded"] and not f["decisions"]["folded"] and f["messages"]["folded"], f)
            pg.click("#board .sec-fold")
            pg.click("#decisions .sec-fold")
            r = pg.evaluate("() => ({ hot: document.querySelector('#board .sec-sum').classList.contains('hot'), folded: document.getElementById('board').classList.contains('sec-folded') })")
            check("  folded by hand, Board's count is highlighted", r["folded"] and r["hot"], r)
            # d-1 answered and d-2 asked in the same update: still one pending, but a new one.
            decision(home, "d-1", "API", "relayed", now)
            decision(home, "d-2", "WEB", "pending", now)
            pg.wait_for_function("() => !!document.getElementById('dec-d-2') && !document.getElementById('dec-d-1')", timeout=15000)
            pg.wait_for_timeout(200)
            check("  one answered and a new one asked at once: Decisions opens again",
                  not pg.evaluate("() => document.getElementById('decisions').classList.contains('sec-folded')"))
            pg.click("#decisions .sec-fold")
            # WAIT goes back to work and NEXT starts to need you: still one, but a new one.
            write_json(os.path.join(home, "snapshot.json"), {"generated_at": now + 1, "decisions": [], "tasks": [], "sessions": [
                session("WAIT", "working", now), session("API", "working", now), session("WEB", "working", now), session("NEXT", "needs-you", now)]})
            pg.wait_for_function("() => document.body.textContent.includes('NEXT')", timeout=15000)
            pg.wait_for_timeout(200)
            r = pg.evaluate("() => ({ board: document.getElementById('board').classList.contains('sec-folded'), dec: document.getElementById('decisions').classList.contains('sec-folded') })")
            check("  a new session that needs you opens Board again (Decisions, nothing new, stays folded)", not r["board"] and r["dec"], r)
            pg.close()
            ctx.close()
        finally:
            if srv:
                srv.terminate()
                srv.wait()
            shutil.rmtree(tmp, ignore_errors=True)

        check("no page errors", not errors, errors[:5])
        browser.close()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
