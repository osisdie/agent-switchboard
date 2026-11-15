#!/usr/bin/env python3
"""tests/test_page_version.py — optional headless check, against a real bin/board-serve, of:
  - the "newer version" banner: a page served before board.html changed offers a reload (in the
    page's language) and never reloads by itself, so nothing being typed is lost;
  - the generic save error names the server's error code;
  - a pane question whose text the hook clipped shows "(shortened)" and stays pressable;
  - ?demo=1 is ignored on a real board-serve (a crafted link never shows sample data).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_version.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a temporary BOARD_HOME; never touches
~/.agent-switchboard.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
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


LONG_Q = "Which color do you prefer for the release notes header? " + "context " * 30
LONG_Q = LONG_Q[:299] + "…"


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-version-test.")
    srv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []
            home, cfg = os.path.join(tmp, "home"), os.path.join(tmp, "config")
            os.makedirs(os.path.join(home, "decisions"))
            os.makedirs(cfg)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            now = int(time.time() * 1000)
            qs = [{"question": LONG_Q, "header": "Color", "multiSelect": False, "question_clipped": True,
                   "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}]}]
            with open(os.path.join(home, "decisions", "d-1794650400-aa11.json"), "w", encoding="utf-8") as fh:
                json.dump({"id": "d-1794650400-aa11", "kind": "question", "source": "pane", "from": "CP",
                           "question": LONG_Q, "options": ["Red", "Blue"], "questions": qs, "questions_clipped": True,
                           "labels_clipped": False, "pressable": True, "status": "pending", "answer": None,
                           "tool_use_id": "tu_1", "tool_name": "AskUserQuestion", "created_at": now}, fh)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=os.path.join(tmp, "s"),
                       CLAUDE_JSON=os.path.join(tmp, "no-claude.json"))
            for k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
                env.pop(k, None)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            base = "http://127.0.0.1:%d/" % port

            def new_page(old=False, lang=None):
                ctx = browser.new_context(viewport={"width": 1100, "height": 900})
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                if lang:
                    ctx.add_init_script("try { localStorage.setItem('switchboard.lang', %s); } catch (e) {}" % json.dumps(lang))
                pg = ctx.new_page()
                if old:  # the page as an older board.html served it
                    def older(route):
                        r = route.fetch()
                        body = re.sub(r'(<meta name="board-version" content=")[0-9a-f]+', r"\g<1>000000000000", r.text())
                        route.fulfill(response=r, body=body)
                    pg.route(base, older)
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                pg.goto(base)
                pg.wait_for_timeout(900)
                return pg

            print("newer version banner:")
            pg = new_page()
            check("the current page: no banner", pg.query_selector("#new-version") is None)
            pg.close()
            pg = new_page(old=True)
            ban = pg.query_selector("#new-version")
            check("a page from an older board.html offers a reload",
                  ban is not None and "A newer version of the board is available." in ban.inner_text() and "Reload" in ban.inner_text(),
                  ban.inner_text() if ban else None)
            pg.fill("#msg-input", "@CP half-written note")
            pg.wait_for_timeout(2500)
            check("it never reloads by itself: the draft stays",
                  pg.input_value("#msg-input") == "@CP half-written note" and pg.query_selector("#new-version") is not None)
            pg.close()
            pg = new_page(old=True, lang="zh-TW")
            ban = pg.query_selector("#new-version")
            check("the banner speaks the page's language", ban is not None and "看板有新版本" in ban.inner_text() and "重新載入" in ban.inner_text(),
                  ban.inner_text() if ban else None)
            pg.close()

            print("?demo=1 on a real board-serve:")
            pg = browser.new_context(viewport={"width": 1100, "height": 900}).new_page()
            pg.route("https://fonts.**", lambda r: r.abort())
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "?demo=1")
            pg.wait_for_timeout(900)
            st = pg.evaluate("""() => ({ demoTag: !document.getElementById('demo-tag').hidden,
              demoCards: document.querySelectorAll('[id$="-demo"]').length,
              real: !!document.getElementById('dec-d-1794650400-aa11') })""")
            check("ignored: the real board, no sample data", st == {"demoTag": False, "demoCards": 0, "real": True}, st)
            pg.close()

            print("clipped pane question:")
            pg = new_page()
            card = pg.evaluate("""() => { const c = document.getElementById('dec-d-1794650400-aa11'); return c ? {
              h3: c.querySelector('h3').textContent, buttons: c.querySelectorAll('.pq-opt').length,
              note: (c.querySelector('.pq-short') || {}).textContent || null } : null; }""")
            check("shows the question as stored, marked (shortened)", card and card["h3"].startswith("CP asks:Which color")
                  and card["note"] == " (shortened)", card)
            check("and stays pressable: its options are buttons", card and card["buttons"] == 2, card)

            print("unknown error code:")
            pg.route("**/api/decisions/*/answer", lambda r: r.fulfill(status=409, content_type="application/json",
                                                                         body=json.dumps({"error": "brand_new_code", "detail": "x"})))
            pg.click("#dec-d-1794650400-aa11 .pq-opt >> nth=0")
            pg.wait_for_timeout(500)
            toasts = pg.evaluate("() => [...document.querySelectorAll('.toast')].map((t) => t.textContent)")
            check("the generic error names the server's code", any("(brand_new_code)" in t for t in toasts), toasts)
            pg.close()

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
