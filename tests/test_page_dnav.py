#!/usr/bin/env python3
"""tests/test_page_dnav.py — optional headless check of finding your way around artifact/board.html
on a wide screen (demo data):

  - 1024, 1280 and 1600 px wide: once you scroll, the bar stays at the top with "Sections" at its
    left (with the needs-you count) and the row of links beside it; nothing past the right edge,
    open or closed;
  - "Sections" opens a dropdown anchored under the button (not full width, inside the screen,
    scrolling on its own when the screen is short) listing every section with its count, plus
    Top, Collapse all and Expand all; Escape, a click outside and tabbing out close it (Escape
    returns focus to the button); a link jumps (the heading lands below the bar), closes it and
    marks the section current in the row, which keeps it in view;
  - "Collapse all" folds everything but what waits on you (Decisions, Board with a session that
    needs you, board-serve data); "Expand all" opens everything;
  - "↑" appears once you scroll down and takes you back to the top;
  - zh-TW strings; the dark theme gives the dropdown a solid background;
  - phones (390 px) keep their own sheet and no row (tests/test_page_mnav.py has the rest).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_dnav.py [-v]

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
  const t = document.getElementById('nav-toggle'), n = document.getElementById('nav-toggle-n'), sheet = document.getElementById('nav-sheet'), row = document.getElementById('nav-row');
  const first = [...row.querySelectorAll('a')].find((a) => a.getBoundingClientRect().width > 0);
  return { toggle: r(t), shown: getComputedStyle(t).display !== 'none', text: t.textContent, badge: n.hidden ? null : n.textContent,
    needs: document.getElementById('needs-n').textContent, nav: r(document.getElementById('secnav')), row: getComputedStyle(row).display, rowFirst: first ? r(first) : null,
    rowMore: row.classList.contains('more-r'), rowLinks: [...row.querySelectorAll('a')].length,
    sheet: getComputedStyle(sheet).display, sheetBox: r(sheet), sheetBg: getComputedStyle(sheet).backgroundColor, sh: sheet.scrollHeight, ch: sheet.clientHeight,
    open: document.getElementById('secnav').classList.contains('open'), expanded: t.getAttribute('aria-expanded'), focus: document.activeElement && document.activeElement.id,
    sw: document.scrollingElement.scrollWidth, vw: innerWidth, vh: innerHeight, y: scrollY }; }"""
LINKS_JS = """() => [...document.querySelectorAll('#nav-list li')].filter((li) => !li.hidden).map((li) => { const a = li.querySelector('a'), b = a.getBoundingClientRect(), badge = a.querySelector('.badge');
  return { href: a.getAttribute('href'), badge: badge ? badge.textContent : null, hot: !!badge && badge.classList.contains('hot'), h: b.height }; })"""
FOLD_JS = """() => Object.fromEntries([...document.querySelectorAll('section.sec')].filter((s) => !s.hidden).map((s) => [s.id, s.classList.contains('sec-folded')]))"""
TOP_JS = """() => { const b = document.getElementById('to-top'), r = b.getBoundingClientRect();
  return { hidden: b.hidden || getComputedStyle(b).display === 'none', w: r.width, right: r.right, bottom: r.bottom, vw: innerWidth, vh: innerHeight, y: scrollY }; }"""
TOOLS = ("nav-top", "nav-fold-all", "nav-unfold-all")


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - no Chromium: skip, never block
            print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
            return 0
        errors = []

        def new_page(init=None, width=1280, height=900):
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

        def scroll_to(pg, y):
            pg.evaluate("(y) => window.scrollTo({ top: y, behavior: 'instant' })", y)
            pg.wait_for_timeout(250)

        for w in (1024, 1280, 1600):
            print("desktop %d px:" % w)
            pg = new_page(width=w)
            r = pg.evaluate(BAR_JS)
            check("nothing past the right edge at the top of the page", r["sw"] <= r["vw"], r)
            scroll_to(pg, 2500)
            r = pg.evaluate(BAR_JS)
            check("  scrolled: the bar stays at the top", abs(r["nav"]["top"]) <= 1, r["nav"])
            check("  Sections sits at its left with the needs-you count, the row of links beside it",
                  r["shown"] and "Sections" in r["text"] and r["badge"] == r["needs"] and r["row"] == "flex"
                  and r["rowFirst"] and r["toggle"]["right"] <= r["rowFirst"]["left"] and r["rowLinks"] >= 9, r)
            pg.click("#nav-toggle")
            pg.wait_for_timeout(100)
            r = pg.evaluate(BAR_JS)
            links = pg.evaluate(LINKS_JS)
            check("  it opens a dropdown under the button, not full width, inside the screen",
                  r["open"] and r["expanded"] == "true" and r["sheet"] == "flex" and r["sheetBox"]["top"] >= r["toggle"]["bottom"]
                  and abs(r["sheetBox"]["left"] - r["toggle"]["left"]) <= 2 and r["sheetBox"]["w"] <= 360 and r["sheetBox"]["bottom"] <= r["vh"], r)
            check("  every section listed with its count (the needs-you count highlighted)",
                  len(links) >= 9 and all(l["h"] > 0 for l in links) and sum(1 for l in links if l["badge"] is not None) >= 8
                  and any(l["href"] == "#decisions" and l["hot"] for l in links), links)
            tools = pg.evaluate("(ids) => ids.map((id) => document.getElementById(id).getBoundingClientRect().height)", list(TOOLS))
            check("  Top, Collapse all and Expand all in it", all(h > 0 for h in tools), tools)
            check("  nothing past the right edge while it is open", r["sw"] <= r["vw"], r)
            pg.keyboard.press("Escape")
            r = pg.evaluate(BAR_JS)
            check("  Escape closes it and focus goes back to the button", not r["open"] and r["sheet"] == "none" and r["expanded"] == "false" and r["focus"] == "nav-toggle", r)
            pg.close()

        print("desktop 1280 px, using it:")
        pg = new_page()
        scroll_to(pg, 2500)
        r = pg.evaluate(BAR_JS)
        check("the row fades out where it hides more links", r["rowMore"], r)
        pg.focus("#nav-row a[href='#lessons']")
        pg.evaluate("() => { document.getElementById('nav-ls').textContent = '7'; document.getElementById('nav-rm-li').hidden = true; }")
        pg.wait_for_timeout(100)
        r2 = pg.evaluate("""() => ({ focus: document.activeElement.getAttribute('href'), n: document.querySelector("#nav-row a[href='#lessons'] .badge").textContent,
          rm: document.querySelector("#nav-row a[href='#reminders']").closest('li').hidden, ids: document.querySelectorAll('#nav-row [id]').length })""")
        check("  a count or a hidden section changes in the row too, and a focused link keeps its focus",
              r2["focus"] == "#lessons" and r2["n"] == "7" and r2["rm"] and r2["ids"] == 0, r2)
        pg.evaluate("() => { document.getElementById('nav-rm-li').hidden = false; }")
        pg.click("#nav-toggle")
        y0 = pg.evaluate("() => scrollY")
        pg.mouse.click(1100, 600)
        r = pg.evaluate(BAR_JS)
        check("  a click outside closes it (and only that: the page stays put)", not r["open"] and r["y"] == y0, r)
        pg.click("#nav-toggle")
        pg.focus("#nav-unfold-all")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(50)
        r = pg.evaluate(BAR_JS)
        check("  tabbing out of it closes it", not r["open"], r)
        pg.click("#nav-toggle")
        pg.click("#nav-list a[href='#messages']")
        settle(pg)
        r = pg.evaluate("""() => { const nav = document.getElementById('secnav').getBoundingClientRect(), hd = document.getElementById('h-messages').getBoundingClientRect(),
            row = document.getElementById('nav-row').getBoundingClientRect(), cur = document.querySelector('#nav-row a[aria-current="true"]'), c = cur && cur.getBoundingClientRect();
          return { open: document.getElementById('secnav').classList.contains('open'), navTop: nav.top, navBottom: nav.bottom, head: hd.top,
            cur: cur && cur.getAttribute('href'), seen: !!c && c.left >= row.left - 1 && c.right <= row.right + 1 }; }""")
        check("  a link jumps, closes it, the heading lands below the bar", not r["open"] and r["navTop"] <= 1 and r["head"] >= r["navBottom"] - 1, r)
        check("  Messages is current in the row, and the row shows it", r["cur"] == "#messages" and r["seen"], r)
        t = pg.evaluate(TOP_JS)
        check("  ↑ shows bottom right once scrolled down", not t["hidden"] and t["vw"] - t["right"] <= 40 and t["vh"] - t["bottom"] <= 40 and t["w"] >= 40, t)
        pg.click("#to-top")
        settle(pg)
        t = pg.evaluate(TOP_JS)
        check("  ↑ goes back to the top and hides", t["y"] == 0 and t["hidden"], t)
        scroll_to(pg, 3000)
        pg.click("#nav-toggle")
        pg.click("#nav-top")
        settle(pg)
        r = pg.evaluate(BAR_JS)
        check("  Top in the dropdown goes back to the top and closes it", r["y"] == 0 and not r["open"], r)
        scroll_to(pg, 2500)
        pg.click("#nav-toggle")
        pg.click("#nav-fold-all")
        settle(pg)
        f = pg.evaluate(FOLD_JS)
        check("  Collapse all folds every section but Decisions (pending decisions wait on you)",
              not f["decisions"] and all(v for k, v in f.items() if k != "decisions") and not pg.evaluate(BAR_JS)["open"], f)
        scroll_to(pg, 600)
        pg.click("#nav-toggle")
        pg.click("#nav-unfold-all")
        f = pg.evaluate(FOLD_JS)
        check("  Expand all opens them all", not any(f.values()), f)
        pg.close()

        print("desktop 1280x560: a short screen:")
        pg = new_page(height=560)
        scroll_to(pg, 2500)
        pg.click("#nav-toggle")
        r = pg.evaluate(BAR_JS)
        check("the dropdown fits the screen and scrolls on its own", r["sheetBox"]["bottom"] <= r["vh"] and r["sh"] > r["ch"], r)
        pg.close()

        print("desktop zh-TW and dark:")
        pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); localStorage.setItem('switchboard.theme', 'dark'); } catch (e) {}")
        scroll_to(pg, 2500)
        r = pg.evaluate(BAR_JS)
        check("the button reads 區段", "區段" in r["text"], r["text"])
        pg.click("#nav-toggle")
        r = pg.evaluate("""() => ({ fold: document.getElementById('nav-fold-all').textContent, unfold: document.getElementById('nav-unfold-all').textContent,
          top: document.getElementById('nav-top').textContent, up: document.getElementById('to-top').getAttribute('aria-label'),
          row: [...document.querySelectorAll('#nav-row a')].map((a) => a.textContent).join('|'),
          bg: getComputedStyle(document.getElementById('nav-sheet')).backgroundColor, panel: getComputedStyle(document.documentElement).getPropertyValue('--panel').trim() })""")
        check("  tools, ↑ and the row in zh-TW", r["fold"] == "全部收合" and r["unfold"] == "全部展開" and "回到頂端" in r["top"] and r["up"] == "回到頂端" and "看板" in r["row"], r)
        check("  dark theme: the dropdown is solid (the panel colour)", r["bg"] not in ("rgba(0, 0, 0, 0)", "transparent") and r["panel"] == "#161d25", r)
        pg.close()

        print("phone 390 px keeps its own sheet:")
        pg = new_page(width=390, height=844)
        r = pg.evaluate(BAR_JS)
        check("no row of links, Sections at the top", r["row"] == "none" and r["shown"] and r["nav"]["top"] <= 1, r)
        pg.click("#nav-toggle")
        r = pg.evaluate(BAR_JS)
        check("  the sheet spans the bar's width", r["open"] and r["sheetBox"]["w"] >= 340, r)
        pg.close()

        print("board-serve, desktop: what waits on you is never folded away:")
        tmp = tempfile.mkdtemp(prefix="switchboard-dnav.")
        srv = None
        try:
            srv, url, home = serve(tmp)
            now = int(time.time() * 1000)
            write_json(os.path.join(home, "snapshot.json"), {"generated_at": now, "decisions": [], "tasks": [], "sessions": [
                session("WAIT", "needs-you", now), session("API", "working", now), session("WEB", "working", now)]})
            decision(home, "d-1", "API", "pending", now)
            ctx = browser.new_context(viewport={"width": 1280, "height": 500})
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(url)
            pg.wait_for_function("() => document.getElementById('nav-toggle-n').textContent === '2' && !!document.getElementById('dec-d-1')", timeout=15000)
            scroll_to(pg, 1200)
            pg.click("#nav-toggle")
            pg.click("#nav-fold-all")
            f = pg.evaluate(FOLD_JS)
            check("Collapse all keeps Board (a session needs you) and Decisions open", not f["board"] and not f["decisions"] and f["messages"], f)
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
