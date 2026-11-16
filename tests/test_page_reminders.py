#!/usr/bin/env python3
"""tests/test_page_reminders.py — optional headless check of the ⏰ Reminders panel and the sticky
banner in artifact/board.html at phone width (390 px), light and dark, English and 繁體中文:

  - demo: the list with countdowns, the banner for a reminder inside its lead window (Ack,
    Snooze), add from the form, delete with a confirm step, no autoplay (no AudioContext before a
    click), no horizontal scroll;
  - a real bin/board-serve: the owner (approvers fallback) adds a reminder from the form (it lands
    in reminders.json), sees the banner, acks it; a viewer who is not an owner sees no panel, no
    nav entry, no banner and no count (GET /api/reminders is 403).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0. CI does not run it. Never touches ~/.agent-switchboard; ALERT_DESKTOP=0 throughout.

  python3 tests/test_page_reminders.py [-v]
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
os.environ["ALERT_DESKTOP"] = "0"

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


VIEW_JS = """() => {
  const q = (s) => document.querySelector(s);
  const vis = (el) => !!el && !el.hidden && !el.closest('[hidden]');
  return {
    section: vis(q('#reminders')), nav: vis(q('#nav-rm-li')), count: q('#nav-rm').textContent,
    rows: [...document.querySelectorAll('#rm-list .rmrow')].map((r) => r.querySelector('.rmtitle').textContent),
    phases: [...document.querySelectorAll('#rm-list .rmrow')].map((r) => r.className.replace('rmrow ', '')),
    banner: vis(q('#rm-banner')) ? [...document.querySelectorAll('#rm-banner .rmb')].map((b) => b.textContent) : [],
    bannerTop: vis(q('#rm-banner')) ? q('#rm-banner').getBoundingClientRect().top : null,
    note: vis(q('#rm-note')) ? q('#rm-note').textContent : null,
    err: q('#rm-err').textContent,
    confirm: q('#rm-list .lconfirm') ? q('#rm-list .lconfirm').textContent : null,
    heading: q('#h-reminders').textContent,
    overflow: document.scrollingElement.scrollWidth > innerWidth + 1,
    toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
    audio: window.__audioCtx || 0,
  };
}"""
# Counts AudioContext constructions: none may happen before a user gesture.
AUDIO_SPY = """(() => { const A = window.AudioContext; if (!A) return; window.__audioCtx = 0;
  window.AudioContext = function (...a) { window.__audioCtx++; return new A(...a); }; })();"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(tmp, approvers, owners=()):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("owners: %s\napprovers: %s\nserve:\n  enabled: true\n  port: %d\n" % (json.dumps(list(owners)), json.dumps(approvers), port))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(home, "no-claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")) or (k.startswith("ALERT_") and k != "ALERT_DESKTOP"):
            env.pop(k)
    subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d" % port, home


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-reminders.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []

            def new_page(scheme="light", lang=None):
                ctx = browser.new_context(viewport={"width": 390, "height": 800}, color_scheme=scheme)
                ctx.add_init_script(AUDIO_SPY)
                if lang:
                    ctx.add_init_script("try { localStorage.setItem('switchboard.lang', '%s'); } catch (e) {}" % lang)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 403" not in m.text
                      and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("demo (phone width):")
            page = new_page()
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731
            page.goto(PAGE + "?demo=1")
            page.wait_for_timeout(1500)
            v = view()
            check("demo: panel and nav", v["section"] and v["nav"] and v["count"] == "3", v)
            check("demo: three reminders, the soon one first", v["rows"][0] == "Design review with the web team" and "ph-soon" in v["phases"][0], v)
            check("demo: banner with a countdown", len(v["banner"]) == 1 and "Design review" in v["banner"][0]
                  and "in 3:" in v["banner"][0] and "Ack" in v["banner"][0] and "Snooze 5 min" in v["banner"][0], v["banner"])
            check("no AudioContext before a gesture", v["audio"] == 0, v["audio"])
            a11y = page.evaluate("""() => { const b = document.getElementById('rm-banner');
              return { live: [b, ...b.querySelectorAll('*')].filter((e) => e.hasAttribute('aria-live')).length,
                       alerts: b.querySelectorAll('[role=alert], [role=status]').length,
                       ticking: [...b.querySelectorAll('[data-rm-left]')].map((e) => e.getAttribute('aria-hidden')),
                       sr: [...b.querySelectorAll('.sr-only')].map((e) => e.textContent) }; }""")
            check("banner: one live region, no per-row alert, the ticking countdown aria-hidden, the time once in words",
                  a11y["live"] == 1 and a11y["alerts"] == 0 and a11y["ticking"] == ["true"]
                  and len(a11y["sr"]) == 1 and a11y["sr"][0].startswith("in ") and a11y["sr"][0].endswith(" minutes"), a11y)
            sr0 = a11y["sr"]
            page.wait_for_timeout(2100)
            check("banner: the spoken part does not change every second",
                  page.evaluate("() => [...document.querySelectorAll('#rm-banner .sr-only')].map((e) => e.textContent)") == sr0)
            page.evaluate("window.scrollTo({top: document.getElementById('approvals').offsetTop, behavior: 'instant'})")
            page.wait_for_timeout(300)
            check("banner stays at the top while scrolled (sticky)", (view()["bannerTop"] or 999) < 200, view()["bannerTop"])
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.wait_for_timeout(200)
            page.click("#rm-banner button:has-text('Snooze 5 min')")
            page.wait_for_timeout(300)
            v = view()
            check("snooze: toast, still soon (5 min away)", any("Snoozed" in t for t in v["toasts"]), v["toasts"])
            page.click("#rm-banner button:has-text('Ack')")
            page.wait_for_timeout(300)
            v = view()
            check("ack: the banner is gone", v["banner"] == [], v["banner"])
            page.fill("#rm-at", "07:15")
            page.fill("#rm-title", "Call the bank")
            page.fill("#rm-lead", "10")
            page.select_option("#rm-repeat", "daily")
            page.click("#rm-form button[type=submit]")
            page.wait_for_timeout(300)
            check("add: the new row", "Call the bank" in view()["rows"], view()["rows"])
            page.fill("#rm-title", "")
            page.fill("#rm-at", "")
            page.click("#rm-form button[type=submit]")
            page.wait_for_timeout(200)
            idx = view()["rows"].index("Call the bank")
            page.locator("#rm-list .rmrow").nth(idx).locator("button:has-text('Delete…')").click()
            check("delete asks first", view()["confirm"] == "Delete this reminder?", view()["confirm"])
            page.locator("#rm-list .rmrow").nth(idx).locator("button.deny.solid").click()
            page.wait_for_timeout(200)
            check("deleted", "Call the bank" not in view()["rows"], view()["rows"])
            check("demo: no horizontal overflow", not view()["overflow"])
            page.context.close()

            print("demo in 繁體中文, dark:")
            zh = new_page("dark", "zh-TW")
            zh.goto(PAGE + "?demo=1")
            zh.wait_for_timeout(1200)
            vz = zh.evaluate(VIEW_JS)
            check("zh-TW: heading and banner buttons", "提醒" in vz["heading"] and vz["banner"] and "確認" in vz["banner"][0]
                  and "延後 5 分鐘" in vz["banner"][0] and "還有" in vz["banner"][0], vz)
            check("zh-TW dark: no overflow", not vz["overflow"])
            zh.context.close()

            print("local board-serve: the owner (approvers fallback)")
            srv, base, home = serve(os.path.join(tmp, "a"), ["local"])
            page = new_page()
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731
            page.goto(base + "/")
            page.wait_for_timeout(1500)
            check("owner: panel, empty", view()["section"] and view()["rows"] == [], view())
            soon = time.localtime(time.time() + 180)
            page.fill("#rm-at", "%02d:%02d" % (soon.tm_hour, soon.tm_min))
            page.fill("#rm-title", "Meeting")
            page.click("#rm-form button[type=submit]")
            page.wait_for_timeout(1200)
            with open(os.path.join(home, "reminders.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            check("owner: added through the route", [r["title"] for r in doc["reminders"]] == ["Meeting"]
                  and doc["reminders"][0]["created_by"] == "board:local", doc)
            v = view()
            check("owner: the banner (inside the lead window)", len(v["banner"]) == 1 and "Meeting" in v["banner"][0], v)
            page.click("#rm-banner button:has-text('Ack')")
            page.wait_for_timeout(1200)
            with open(os.path.join(home, "reminders.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            check("owner: ack reached the store", doc["reminders"][0]["state"] == "done", doc["reminders"][0].get("state"))
            check("owner: banner gone", view()["banner"] == [], view()["banner"])
            page.context.close()
            srv.terminate(); srv.wait(10)

            print("local board-serve: a viewer (not an owner)")
            srv, base, home = serve(os.path.join(tmp, "b"), ["boss@example.com"], ["owner@example.com"])
            with open(os.path.join(home, "reminders.json"), "w", encoding="utf-8") as fh:
                now = int(time.time() * 1000)
                json.dump({"version": 1, "reminders": [{"id": "r-1-abcd", "title": "Private thing", "at": "15:00",
                           "date": "2026-11-16", "tz": "", "lead_min": 5, "level": "high", "breakthrough": True,
                           "repeat": "once", "created_by": "cli", "created_at": now, "state": "active", "history": [],
                           "occ": {"at": now + 120000, "lead_at": now - 180000}}]}, fh)
            page = new_page()
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731
            page.goto(base + "/")
            page.wait_for_timeout(1500)
            v = view()
            check("viewer: no panel, no nav, no banner", not v["section"] and not v["nav"] and v["banner"] == [], v)
            html = page.content()
            check("viewer: the title is nowhere on the page", "Private thing" not in html)
            st = page.evaluate("fetch('/api/reminders', {headers: {'X-Board': '1'}}).then((r) => r.status)")
            check("viewer: GET /api/reminders is 403", st == 403, st)
            page.context.close()
            srv.terminate(); srv.wait(10)
            srv = None
            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\npassed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
