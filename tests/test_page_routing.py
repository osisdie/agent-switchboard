#!/usr/bin/env python3
"""tests/test_page_routing.py — optional headless check of message routing and the Messages tools
in artifact/board.html:

  - the section nav: sticky, a jump lands below it, the current section is marked, no side scroll,
    and at phone width it folds into a "Sections" button;
  - Messages: the search (text, session, sender), the session select and the filter chips list
    every loaded thread that matches; bodies are clamped to 4 lines with Show more; a thread
    collapses to one line, Collapse all / Expand all, and the choice survives a reload
    (localStorage; storage blocked still works); the search also drives History, which pages
    with Load more;
  - the composer: the @ list shows each session's `about` and @triage; @all asks first ("this
    interrupts all N live sessions") and only the confirm sends; zh-TW strings;
  - board-serve, an asker: no @all (the list and the server refuse it), a message without a mention
    goes to triage, the thread says BOARD will route it, and after `board message route` it shows
    who it went to; an approver still sends to all after the confirm.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_routing.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses temporary BOARD_HOMEs; never touches ~/.agent-switchboard.
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh)


VIEW_JS = """() => ({ shown: [...document.querySelectorAll('#threads > li')].map((l) => l.id.replace('thread-', '')),
  folded: [...document.querySelectorAll('#threads > li.folded')].map((l) => l.id.replace('thread-', '')),
  status: document.getElementById('msg-filter-status').textContent,
  empty: document.getElementById('msg-empty').hidden ? '' : document.getElementById('msg-empty').textContent,
  chips: [...document.querySelectorAll('#msg-chips .fchip')].filter((b) => !b.hidden).map((b) => b.textContent + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')) })"""
MENTION_JS = """() => [...document.querySelectorAll('#mention-list li')].map((l) => l.querySelector('.c').textContent + '|' + l.querySelector('.d').textContent)"""


def serve(tmp, role):
    home, cfg = os.path.join(tmp, "home"), os.path.join(tmp, "config")
    for d in (home, cfg, os.path.join(tmp, "sessions")):
        os.makedirs(d)
    port = free_port()
    approvers = ["boss@example.invalid"] + (["local"] if role == "approver" else [])
    askers = ["local"] if role == "asker" else []
    write(os.path.join(cfg, "fleet.yaml"),
          "approvers: %s\naskers: %s\nsessions:\n  - code: API\n    cwd: /w\n    about: backend API and database migrations\n"
          "  - code: WEB\n    cwd: /w\n    about: frontend app\n  - code: OPS\n    cwd: /w\nserve:\n  enabled: true\n  port: %d\n"
          % (json.dumps(approvers), json.dumps(askers), port))
    now = int(time.time() * 1000)
    write(os.path.join(home, "snapshot.json"), {"generated_at": now, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "working", "updated_at": now},
        {"code": "WEB", "aliases": ["WEB"], "state": "done", "updated_at": now},
        {"code": "OPS", "aliases": ["OPS"], "state": "offline", "updated_at": now}]})
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
    return proc, "http://127.0.0.1:%d/" % port, home, env


def settle(pg):
    """Wait for the smooth scroll to a section to finish (the page scrolls smoothly)."""
    pg.wait_for_timeout(1500)


def stored(home):
    d = os.path.join(home, "messages")
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if n.endswith(".json"):
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-route-page.")
    srv = None
    try:
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

            print("demo: the section nav:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            links = pg.evaluate("() => [...document.querySelectorAll('#nav-list a')].map((a) => a.getAttribute('href'))")
            check("jump links for every section, Messages among them", "#messages" in links and "#decisions" in links and "#board" in links, links)
            pg.click("#nav-row a[href='#messages']")
            settle(pg)
            r = pg.evaluate("""() => { const nav = document.getElementById('secnav').getBoundingClientRect(), hd = document.getElementById('h-messages').getBoundingClientRect();
              return { navTop: nav.top, navBottom: nav.bottom, head: hd.top, cur: document.querySelector('#nav-row a[aria-current="true"]').getAttribute('href'),
                sw: document.scrollingElement.scrollWidth, vw: innerWidth }; }""")
            check("  the nav stays at the top, the heading lands below it (not covered)", r["navTop"] <= 1 and r["head"] >= r["navBottom"] - 1, r)
            check("  Messages is marked as the current section", r["cur"] == "#messages", r)
            pg.close()
            pg = new_page(viewport={"width": 390, "height": 800})
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            r = pg.evaluate("() => ({ list: getComputedStyle(document.getElementById('nav-sheet')).display, row: getComputedStyle(document.getElementById('nav-row')).display, btn: getComputedStyle(document.getElementById('nav-toggle')).display })")
            check("phone width: the list folds behind a Sections button", r["list"] == "none" and r["row"] == "none" and r["btn"] != "none", r)
            pg.click("#nav-toggle")
            pg.click("#nav-list a[href='#messages']")
            settle(pg)
            r = pg.evaluate("""() => ({ open: document.getElementById('secnav').classList.contains('open'), head: document.getElementById('h-messages').getBoundingClientRect().top,
              nav: document.getElementById('secnav').getBoundingClientRect().bottom, sw: document.scrollingElement.scrollWidth, vw: innerWidth })""")
            check("  it opens, jumps, closes again; nothing past the edge", not r["open"] and r["head"] >= r["nav"] - 1 and r["sw"] <= r["vw"], r)
            pg.close()

            print("demo: find messages, clamp, collapse:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            v = pg.evaluate(VIEW_JS)
            check("the latest 5 threads, no filter on", len(v["shown"]) == 5 and v["status"] == "", v)
            check("  chips with counts (Held hidden when there is none)", any(c.startswith("Unread · ") for c in v["chips"]) and any(c.startswith("Awaiting reply · ") for c in v["chips"]), v["chips"])
            pg.fill("#msg-q", "release week")
            v = pg.evaluate(VIEW_JS)
            check("search by text: every loaded thread that matches", v["shown"] == ["m-91-old"] and v["status"].startswith("1 of the 14 loaded threads match."), v)
            pg.fill("#msg-q", "")
            pg.select_option("#msg-to", "API")
            v = pg.evaluate(VIEW_JS)
            check("  session filter: the API threads, older ones too", len(v["shown"]) >= 1 and "m-97-old" in v["shown"], v)
            opt = pg.evaluate("() => [...document.querySelectorAll('#msg-to option')].map((o) => o.textContent).find((t) => t.startsWith('@API'))")
            check("  the session select shows what each session is for", opt == "@API — backend API and database migrations", opt)
            pg.select_option("#msg-to", "")
            pg.fill("#msg-q", "an editor")
            v = pg.evaluate(VIEW_JS)
            check("  search by sender label", len(v["shown"]) >= 1, v)
            pg.fill("#msg-q", "zzz-nothing")
            v = pg.evaluate(VIEW_JS)
            check("  no match says so", v["shown"] == [] and "No loaded threads match." in v["empty"], v)
            pg.fill("#msg-q", "")
            pg.click("#msg-chips .fchip[data-chip='waiting']")
            v = pg.evaluate(VIEW_JS)
            st = pg.evaluate("() => [...document.querySelectorAll('#threads > li .status')].map((s) => s.textContent)")
            check("  chip Awaiting reply: only new / relayed threads without replies", v["chips"][1].endswith("*") and all(x in ("new", "relayed") for x in st) and v["shown"], (v, st))
            pg.click("#msg-chips .fchip[data-chip='waiting']")
            check("  and off again", len(pg.evaluate(VIEW_JS)["shown"]) == 5)
            c = pg.evaluate("""() => { const p = document.querySelector('#thread-m-91-old p.body'), lh = parseFloat(getComputedStyle(p).lineHeight) || 24;
              return { clamp: getComputedStyle(p).webkitLineClamp, lines: Math.round(p.getBoundingClientRect().height / lh) }; }""")
            check("a long body is clamped to 4 lines", c["clamp"] == "4" and c["lines"] <= 4, c)
            pg.click("#thread-m-91-old .thread-fold")
            r = pg.evaluate("""() => { const t = document.getElementById('thread-m-91-old'), b = t.querySelector('.thread-fold');
              return { folded: t.classList.contains('folded'), body: t.querySelector('p.body').getClientRects().length, preview: t.querySelector('.preview').getClientRects().length,
                exp: b.getAttribute('aria-expanded'), label: b.getAttribute('aria-label'), h: t.getBoundingClientRect().height }; }""")
            check("a thread collapses to its top line and a preview", r["folded"] and r["body"] == 0 and r["preview"] > 0 and r["exp"] == "false" and r["label"] == "Expand thread" and r["h"] < 80, r)
            pg.reload()
            pg.wait_for_timeout(500)
            check("  kept after a reload (this browser)", pg.evaluate(VIEW_JS)["folded"] == ["m-91-old"])
            pg.click("#msg-fold-all")
            v = pg.evaluate(VIEW_JS)
            check("Collapse all", sorted(v["folded"]) == sorted(v["shown"]) and len(v["folded"]) == 5, v)
            pg.click("#msg-unfold-all")
            check("Expand all", pg.evaluate(VIEW_JS)["folded"] == [])
            pg.click("#msg-hist-btn")
            pg.fill("#msg-q", "backfill")
            pg.wait_for_timeout(200)
            r = pg.evaluate("""() => ({ q: document.getElementById('msg-hist-q').value, hist: [...document.querySelectorAll('#msg-hist-list > li')].map((l) => l.id),
              shown: [...document.querySelectorAll('#threads > li')].map((l) => l.id), dup: document.querySelectorAll('[id="thread-m-101-old"]').length })""")
            check("the search also drives History, never listing a thread twice", r["q"] == "backfill" and r["shown"] == ["thread-m-101-old"]
                  and r["hist"] == [] and r["dup"] == 1, r)
            pg.fill("#msg-q", "")
            pg.close()

            pg = new_page("Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } });")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            pg.click("#msg-fold-all")
            check("storage blocked: Collapse all still works for this page view", len(pg.evaluate(VIEW_JS)["folded"]) == 5)
            pg.close()

            print("demo: the composer:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            pg.click("#msg-input")
            pg.keyboard.type("@")
            items = pg.evaluate(MENTION_JS)
            check("@ lists triage, all, then each session with what it is for", items[0].startswith("@triage|") and items[1].startswith("@all|")
                  and any(x.startswith("@API|backend API and database migrations") for x in items), items)
            pg.keyboard.press("Escape")
            pg.fill("#msg-input", "@triage who owns the flaky login test?")
            check("  the To line says where it goes", "triage (BOARD routes it" in pg.evaluate("() => document.getElementById('msg-to-line').textContent"))
            n0 = pg.evaluate("() => document.querySelectorAll('#threads > li').length + document.getElementById('nav-msg').textContent")
            pg.fill("#msg-input", "@all freeze main at 18:00")
            live = pg.evaluate("() => document.getElementById('msg-to-line').textContent")
            pg.click("#msg-send")
            r = pg.evaluate("() => ({ shown: !document.getElementById('msg-all-confirm').hidden, q: document.getElementById('msg-all-q').textContent, v: document.getElementById('msg-input').value, nav: document.getElementById('nav-msg').textContent })")
            check("@all: Send asks first, nothing sent yet", r["shown"] and "This interrupts all 7 live sessions" in r["q"] and r["v"] == "@all freeze main at 18:00"
                  and r["nav"] == "14" and "all 7 live sessions" in live, (r, live))
            pg.click("#msg-all-cancel")
            check("  Cancel keeps the text and sends nothing", pg.evaluate("() => document.getElementById('msg-all-confirm').hidden && document.getElementById('nav-msg').textContent === '14'"))
            pg.click("#msg-send")
            pg.click("#msg-all-yes")
            pg.wait_for_timeout(200)
            r = pg.evaluate("() => ({ nav: document.getElementById('nav-msg').textContent, top: document.querySelector('#threads > li .to').textContent, v: document.getElementById('msg-input').value })")
            check("  the confirm sends it to all", r["nav"] == "15" and r["top"] == "→ @all" and r["v"] == "", (r, n0))
            pg.fill("#msg-input", "@all again")
            pg.click("#msg-send")
            pg.fill("#msg-input", "@all changed")
            check("  editing the text drops a pending confirm", pg.evaluate("() => document.getElementById('msg-all-confirm').hidden"))
            pg.focus("#msg-input")
            pg.keyboard.press("Control+Enter")
            r = pg.evaluate("() => ({ c: !document.getElementById('msg-all-confirm').hidden, nav: document.getElementById('nav-msg').textContent })")
            check("  Ctrl+Enter on an @all draft asks too, sends nothing", r["c"] and r["nav"] == "15", r)
            pg.keyboard.press("Control+Enter")
            r = pg.evaluate("() => ({ c: !document.getElementById('msg-all-confirm').hidden, nav: document.getElementById('nav-msg').textContent })")
            check("  a second Ctrl+Enter does not skip the confirm", r["c"] and r["nav"] == "15", r)
            pg.evaluate("() => document.getElementById('composer').requestSubmit()")
            r = pg.evaluate("() => ({ c: !document.getElementById('msg-all-confirm').hidden, nav: document.getElementById('nav-msg').textContent })")
            check("  nor does a form submit", r["c"] and r["nav"] == "15", r)
            pg.close()

            print("demo: zh-TW:")
            pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            r = pg.evaluate("""() => ({ q: document.getElementById('msg-q').placeholder, fold: document.getElementById('msg-fold-all').textContent,
              chip: document.querySelector('#msg-chips .fchip').textContent, tf: document.querySelector('#threads .thread-fold').getAttribute('aria-label'),
              help: document.querySelector('#composer .help').textContent })""")
            check("the new strings are in 繁體中文", r["q"].startswith("搜尋訊息") and r["fold"] == "全部收合" and r["chip"].startswith("未讀")
                  and r["tf"] == "收合對話" and "讓 BOARD 幫你轉給對的 session" in r["help"], r)
            pg.fill("#msg-input", "@all hi")
            pg.click("#msg-send")
            check("  the @all confirm too", "這會打斷全部 7 個線上 session" in pg.evaluate("() => document.getElementById('msg-all-q').textContent"))
            pg.close()

            print("board-serve, an asker:")
            srv, base, home, env = serve(os.path.join(tmp, "asker"), "asker")
            pg = new_page()
            pg.goto(base)
            pg.wait_for_function("() => !document.getElementById('composer').hidden", timeout=10000)
            pg.click("#msg-input")
            pg.keyboard.type("@")
            items = pg.evaluate(MENTION_JS)
            check("@ lists triage first and never @all; each session's about from fleet.yaml", items[0].startswith("@triage|")
                  and not any(x.startswith("@all|") for x in items) and any(x.startswith("@WEB|frontend app") for x in items), items)
            pg.keyboard.press("Escape")
            pg.fill("#msg-input", "")
            r = pg.evaluate("() => ({ help: getComputedStyle(document.getElementById('msg-help-all')).display, to: document.getElementById('msg-to-line').textContent, ph: document.getElementById('msg-input').placeholder })")
            check("  no @all in the help; To: triage by default", r["help"] == "none" and "triage" in r["to"] and r["ph"].startswith("Ask your question"), r)
            pg.fill("#msg-input", "@all hello everyone")
            pg.click("#msg-send")
            r = pg.evaluate("() => ({ err: document.getElementById('msg-err').textContent, confirm: !document.getElementById('msg-all-confirm').hidden })")
            check("  @all is refused on the page", "Askers cannot message @all" in r["err"] and not r["confirm"] and not stored(home), r)
            pg.fill("#msg-input", "Who owns the export job? It failed twice tonight.")
            pg.click("#msg-send")
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length > 0", timeout=10000)
            recs = stored(home)
            check("  no mention: sent to triage, as the asker's", len(recs) == 1 and recs[0]["to"] == "triage" and recs[0]["kind"] == "asker", recs)
            mid = recs[0]["id"] if recs else ""
            pg.wait_for_function("(id) => { const t = document.getElementById('thread-' + id); return t && t.textContent.includes('Waiting for BOARD to route it'); }", arg=mid, timeout=10000)
            check("  the thread says BOARD will route it", True)
            r = subprocess.run([os.path.join(REPO, "bin", "board"), "message", "route", mid, "API", "WEB"], env=env, capture_output=True, text=True)
            check("  the operator records the route", r.returncode == 0, r.stderr)
            try:
                pg.wait_for_function("(id) => document.getElementById('thread-' + id).textContent.includes('Routed by BOARD to API, WEB')", arg=mid, timeout=10000)
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
            check("  and the thread shows who it went to", ok, pg.evaluate("(id) => document.getElementById('thread-' + id).textContent", mid))
            st = pg.evaluate("() => fetch('/api/messages', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Board': '1' }, body: JSON.stringify({ to: 'all', body: 'x' }) }).then(async (r) => [r.status, (await r.json()).error])")
            check("  the server refuses @all for an asker too", st == [403, "asker_no_all"], st)
            pg.close()
            srv.terminate()
            srv.wait(10)
            srv = None

            print("board-serve, an approver:")
            srv, base, home, env = serve(os.path.join(tmp, "approver"), "approver")
            pg = new_page()
            pg.goto(base)
            pg.wait_for_function("() => !document.getElementById('composer').hidden", timeout=10000)
            # 30 older threads (one says "walrus", far beyond the state's latest 20)
            t0 = int(time.time() * 1000) - 3600 * 1000
            for i in range(30):
                mid = "m-%d-o%02d" % (t0 + i * 1000, i)
                write(os.path.join(home, "messages", mid + ".json"), {"id": mid, "to": "API", "body": "old %d%s" % (i, " walrus" if i == 2 else ""),
                                                                       "by": "local", "created_at": t0 + i * 1000, "status": "relayed"})
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length === 5", timeout=10000)
            pg.fill("#msg-q", "walrus")
            v = pg.evaluate(VIEW_JS)
            check("a search over the loaded threads says when only History has older ones", v["shown"] == [] and "Open History" in v["empty"]
                  and "History searches the older ones." in v["status"], v)
            pg.click("#msg-hist-btn")
            pg.wait_for_function("() => document.querySelectorAll('#msg-hist-list > li').length > 0", timeout=10000)
            hist = pg.evaluate("() => [...document.querySelectorAll('#msg-hist-list > li')].map((l) => l.id)")
            check("  History searches board-serve's older pages with the same text", hist == ["thread-m-%d-o02" % (t0 + 2000)], hist)
            pg.fill("#msg-q", "")
            pg.click("#msg-hist-btn")
            pg.wait_for_timeout(300)
            for f in os.listdir(os.path.join(home, "messages")):
                os.remove(os.path.join(home, "messages", f))
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length === 0", timeout=10000)
            pg.fill("#msg-input", "@all release freeze at 18:00")
            pg.focus("#msg-input")
            pg.keyboard.press("Control+Enter")
            pg.evaluate("() => document.getElementById('composer').requestSubmit()")
            pg.wait_for_timeout(500)
            check("Ctrl+Enter and a form submit on @all send nothing before the confirm", not stored(home))
            pg.click("#msg-send")
            q = pg.evaluate("() => document.getElementById('msg-all-q').textContent")
            check("@all asks first: the live sessions are counted (offline ones are not)", "all 2 live sessions" in q and not stored(home), q)
            pg.click("#msg-all-yes")
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length > 0", timeout=10000)
            recs = stored(home)
            check("  then sends it to all", len(recs) == 1 and recs[0]["to"] == "all", recs)
            pg.close()
            check("no page errors", not errors, errors[:5])
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
