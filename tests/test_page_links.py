#!/usr/bin/env python3
"""tests/test_page_links.py — optional headless check of the board page's alert links
(?d= / ?m= / ?c=, #<id>, #dec-<id>): openLinked() in artifact/board.html, plus the System tile
and Resources section (demo data and a board-serve resources.json).

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


RES_JS = """() => ({
  tile: !document.getElementById('sys').hidden, lvl: document.getElementById('sys-lvl').textContent,
  mem: document.querySelector('#sys-mem .u-p').textContent, memCls: document.getElementById('sys-mem').className,
  oom: document.getElementById('sys-oom').textContent,
  rows: document.querySelectorAll('#res-body tr').length, idle: document.querySelectorAll('#res-body .idle').length,
  disks: document.querySelectorAll('#disk-body tr').length, nav: document.getElementById('nav-res').textContent,
  disk0: (document.querySelector('#disk-body td') || {}).textContent,
  note: document.getElementById('res-idle-note').textContent,
  wide: document.documentElement.scrollWidth > window.innerWidth,
  banners: [...document.querySelectorAll('#banners .banner strong')].map((x) => x.textContent),
})"""


# A stand-in for the Google Fonts stylesheet: one face whose file (on fonts.gstatic.com) the test
# holds open, which keeps the load event from firing until it is released.
FONT_CSS = ("@font-face { font-family: 'IBM Plex Sans'; font-display: swap;"
            " src: url(https://fonts.gstatic.com/s/held/held.woff2) format('woff2'); }")


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

            # The viewer starts typing in another card before the load event (a slow font): the
            # deferred jump must not move focus, scroll, or the rest of the typing into the gate.
            held = []
            page.route("https://fonts.googleapis.com/**",
                       lambda r: r.fulfill(status=200, content_type="text/css", body=FONT_CSS))
            page.route("https://fonts.gstatic.com/**", lambda r: held.append(r))  # answered only when released
            page.goto("about:blank")
            page.goto(PAGE + "?demo=1&d=d-5-demo", wait_until="domcontentloaded")
            page.wait_for_selector("#ans-d-2-demo")
            for _ in range(50):
                if held:
                    break
                page.wait_for_timeout(50)
            pre = page.evaluate("() => document.readyState")
            check("slow font holds the load event (precondition)", held and pre != "complete", (len(held), pre))
            page.click("#ans-d-2-demo")
            page.keyboard.type("keep ")
            for r in held:
                r.fulfill(status=404, body="")
            page.wait_for_function("() => document.readyState === 'complete'")
            page.wait_for_timeout(600)
            page.keyboard.type("typing")
            s = page.evaluate("""() => ({ active: document.activeElement.id,
              other: document.getElementById('ans-d-2-demo').value,
              gate: document.getElementById('ans-d-5-demo').value,
              linked: !!document.querySelector('#dec-d-5-demo.linked') })""")
            check("typing first: focus stays in the other card", s["active"] == "ans-d-2-demo", s)
            check("typing first: all text in the other card, none in the gate",
                  s["other"] == "keep typing" and s["gate"] == "", s)
            check("typing first: the linked card is still highlighted", s["linked"], s)
            page.unroute("https://fonts.googleapis.com/**")
            page.unroute("https://fonts.gstatic.com/**")
            # The released font answered 404 on purpose: that console line is not a page error.
            errors[:] = [e for e in errors if not e.startswith("Failed to load resource")]

            page.emulate_media(reduced_motion="reduce")
            s = go(PAGE + "?demo=1&d=d-6-demo")
            check("reduced motion: highlight without animation", s["linked"] == "dec-d-6-demo" and s["anim"] == "none", s)
            page.emulate_media(reduced_motion="no-preference")
            s = go(PAGE + "?demo=1&d=d-6-demo")
            check("motion allowed: highlight animates", s["anim"] == "linked", s)

            s = go(PAGE + "?d=d-6-demo", 1500)
            check("no data source (hosted page without data): nothing happens",
                  s["linked"] is None and not s["toasts"], s)

            print("resources (demo):")
            go(PAGE + "?demo=1")
            r = page.evaluate(RES_JS)
            check("System tile shown", r["tile"], r)
            check("level chip", r["lvl"] == "warn", r)
            check("memory bar: used % in the warn color", r["mem"] == "88%" and "lvl-amber" in r["memCls"], r)
            check("OOM counter", r["oom"] == "OOM 1", r)
            check("a row per session plus orphaned teammates", r["rows"] == 4, r)
            check("idle teammates flagged", r["idle"] == 2, r)
            check("disks table", r["disks"] == 2, r)
            check("disks by opaque id, not path", r["disk0"] == "disk1", r)
            check("nav badge counts idle teammates", r["nav"] == "2", r)
            check("idle note says the board stops nothing", "never stops a process" in r["note"], r)
            check("no critical banner at warn", not any("critical" in b for b in r["banners"]), r)
            check("no horizontal scroll at phone width", not r["wide"], r)

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
            proc = os.path.join(tmp, "proc")  # a calm fixture /proc: never the live machine's numbers
            subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), proc], check=True)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, CLAUDE_JSON=os.path.join(home, "no-claude.json"), BOARD_PROC_ROOT=proc)
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

            r = page.evaluate(RES_JS)
            check("local: System tile from resources.json", r["tile"] and r["lvl"] == "ok" and r["mem"] == "50%", r)
            check("local: no watch process -> banner", "Operator watch is not running" in r["banners"], r)
            with open(os.path.join(home, "resources.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            doc.update(level="critical", worst="mem", levels={"mem": "critical"}, operator={"watch": True})
            doc["mem"]["available_pct"] = 4
            with open(os.path.join(home, "resources.json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            page.wait_for_timeout(2500)  # serve polls the signature every second, then SSE
            r = page.evaluate(RES_JS)
            check("local: critical -> red banner", "System resources critical — memory" in r["banners"], r)
            check("local: watch back -> its banner gone", "Operator watch is not running" not in r["banners"], r)
            check("local: memory bar red", r["mem"] == "96%" and "lvl-red" in r["memCls"], r)

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
