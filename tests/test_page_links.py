#!/usr/bin/env python3
"""tests/test_page_links.py — optional headless check of the board page's alert links
(?d= / ?m= / ?c=, #<id>, #dec-<id>): openLinked() in artifact/board.html.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_links.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a real bin/board-serve
on a temporary BOARD_HOME; never touches ~/.agent-switchboard.
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


STATE_JS = """() => {
  const a = document.activeElement;
  const linked = document.querySelector('.linked');
  const r = linked ? linked.getBoundingClientRect() : null;
  return {
    tag: a ? a.tagName : null, activeId: a ? a.id : null,
    card: a && a.closest('article, li.thread') ? a.closest('article, li.thread').id : null,
    linked: linked ? linked.id : null,
    inView: r ? (r.top < innerHeight && r.bottom > 0) : null,
    anim: linked ? getComputedStyle(linked).animationName : null,
    toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
    search: location.search, hash: location.hash,
  };
}"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-test.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            ctx = browser.new_context(viewport={"width": 390, "height": 760})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            def go(url, wait=900):
                page.goto("about:blank")  # a hash-only change would not reload the page
                page.goto(url)
                page.wait_for_timeout(wait)
                return page.evaluate(STATE_JS)

            print("demo adapter:")
            s = go(PAGE + "?demo=1&d=d-5-demo")
            check("gate: card highlighted and in view", s["linked"] == "dec-d-5-demo" and s["inView"], s)
            check("gate: focus on the free-text answer, not a button",
                  s["tag"] == "INPUT" and s["activeId"] == "ans-d-5-demo", s)
            check("replaceState drops d, keeps demo", s["search"] == "?demo=1", s)
            page.keyboard.press("Enter")  # an empty answer field: Enter must not answer
            page.wait_for_timeout(300)
            check("gate: Enter on the focused field answers nothing",
                  page.evaluate("() => !!document.getElementById('dec-d-5-demo')"))

            s = go(PAGE + "?demo=1&d=d-1-demo")
            check("permission (no text field): focus on the card itself",
                  s["tag"] == "ARTICLE" and s["activeId"] == "dec-d-1-demo", s)
            page.keyboard.press("Space")
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)
            check("permission: Space / Enter neither allow nor ask to confirm",
                  page.evaluate("() => { const c = document.getElementById('dec-d-1-demo');"
                                " return !!c && !c.querySelector('.confirm-row'); }"))

            s = go(PAGE + "?demo=1#dec-d-5-demo", 1500)
            check("#dec-<id>: focus kept after the browser's fragment scroll",
                  s["tag"] == "INPUT" and s["activeId"] == "ans-d-5-demo", s)
            s = go(PAGE + "?demo=1#d-2-demo")
            check("#<id>: card highlighted", s["linked"] == "dec-d-2-demo", s)

            s = go(PAGE + "?demo=1&d=d-3-demo")
            check("answered decision: notice", any("already answered" in t for t in s["toasts"]), s)
            check("answered decision: nothing highlighted", s["linked"] is None, s)
            check("answered decision: parameter dropped too", s["search"] == "?demo=1", s)
            s = go(PAGE + "?demo=1&c=c-1-demo")
            check("?c=: consensus card focused", s["card"] == "cons-c-1-demo" and s["linked"] == "cons-c-1-demo", s)
            s = go(PAGE + "?demo=1&m=m-1-demo")
            check("?m=: message thread focused", s["card"] == "thread-m-1-demo", s)
            s = go(PAGE + "?demo=1&m=m-9-gone")
            check("?m= unknown: notice", any("no longer on the board" in t for t in s["toasts"]), s)
            s = go(PAGE + '?demo=1&d=d-1-%22%5D*%5Bx')
            check("malformed id: ignored silently", s["linked"] is None and not s["toasts"], s)
            check("malformed id: left in the address bar", "d=" in s["search"], s)
            s = go(PAGE + "?demo=1#decisions")
            check("section anchors untouched", s["linked"] is None and not s["toasts"], s)

            page.emulate_media(reduced_motion="reduce")
            s = go(PAGE + "?demo=1&d=d-6-demo")
            check("reduced motion: highlight without animation", s["linked"] == "dec-d-6-demo" and s["anim"] == "none", s)
            page.emulate_media(reduced_motion="no-preference")
            s = go(PAGE + "?demo=1&d=d-6-demo")
            check("motion allowed: highlight animates", s["anim"] == "linked", s)

            s = go(PAGE + "?d=d-6-demo", 1500)
            check("no data source (hosted page without data): nothing happens",
                  s["linked"] is None and not s["toasts"], s)

            print("local adapter (board-serve):")
            home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
            for d in (os.path.join(home, "decisions"), cfg, sess):
                os.makedirs(d)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            now = int(time.time() * 1000)
            for did, st in (("d-1794650400-ab12", "pending"), ("d-1794650400-cd34", "answered")):
                with open(os.path.join(home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
                    json.dump({"id": did, "kind": "question", "from": "API", "question": "Ship?",
                               "options": ["yes", "no"], "status": st, "created_at": now, "updated_at": now}, fh)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess)
            env.pop("BOARD_PUBLIC_URL", None)
            subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = "http://127.0.0.1:%d" % port
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            s = go(base + "/?d=d-1794650400-ab12", 1500)
            check("local ?d=: card highlighted, focus on the text field",
                  s["linked"] == "dec-d-1794650400-ab12" and s["tag"] == "INPUT", s)
            check("local ?d=: parameter dropped", s["search"] == "", s)
            s = go(base + "/?d=d-1794650400-cd34", 1500)
            check("local answered: notice", any("already answered" in t for t in s["toasts"]), s)

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
