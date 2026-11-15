#!/usr/bin/env python3
"""tests/test_page_rc.py — optional headless check of the Remote Control controls in
artifact/board.html: the RC chip on session cards, the per-session toggle for approvers and the
"RC: all on / all off" switch (with its confirm step) in the Notifications dialog.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_rc.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a real bin/board-serve
on a temporary BOARD_HOME, with tests/fake_tmux.py as tmux; never touches ~/.agent-switchboard.
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


CHIPS_JS = """() => Object.fromEntries([...document.querySelectorAll('.scard')].map((c) => {
  const chip = c.querySelector('.rcchip');
  return [c.querySelector('.code').textContent, chip ? chip.className.replace('rcchip ', '') + ':' + chip.textContent : null];
}))"""
OVERFLOW_JS = "() => document.scrollingElement.scrollWidth > innerWidth + 1"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def serve(tmp, approvers, live_pid):
    """board-serve on a fresh BOARD_HOME: session CP runs (busy, Remote Control off)."""
    home, cfg, sess, fbin = (os.path.join(tmp, x) for x in ("home", "config", "sessions", "bin"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess, fbin):
        os.makedirs(d)
    with open(os.path.join(fbin, "tmux"), "w") as fh:
        fh.write('#!/bin/sh\nexec %s %s "$@"\n' % (sys.executable, os.path.join(REPO, "tests", "fake_tmux.py")))
    os.chmod(os.path.join(fbin, "tmux"), 0o755)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\nsessions:\n  - code: CP\n    cwd: /w\n  - code: QA\n    cwd: /w\n"
                 % (json.dumps(approvers), port))
    with open(os.path.join(sess, "%d.json" % live_pid), "w") as fh:
        with open("/proc/%d/stat" % live_pid) as st:
            stat = st.read()
        json.dump({"pid": live_pid, "sessionId": "s-cp", "name": "CP", "tmux": "CP:@1.%42", "status": "busy",
                   "bridgeSessionId": None, "startedAt": 1,
                   "procStart": str(int(stat[stat.rindex(")") + 2:].split()[19]))}, fh)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess,
               CLAUDE_JSON=os.path.join(home, "no-claude.json"), PATH=fbin + os.pathsep + os.environ["PATH"])
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS", "TMUX", "FAKE_TMUX")):
            env.pop(k)
    env["ALERT_DESKTOP"] = "0"
    subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                   env=env, check=True, stdout=subprocess.DEVNULL)
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
    tmp = tempfile.mkdtemp(prefix="switchboard-page-rc.")
    srv = None
    # The fixture session's process: without DO_NOT_TRACK, which would make Remote Control unavailable.
    live = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={k: v for k, v in os.environ.items() if k != "DO_NOT_TRACK"})
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
            chips = lambda: page.evaluate(CHIPS_JS)  # noqa: E731

            print("demo adapter (phone width):")
            page.goto(PAGE + "?demo=1")
            page.wait_for_timeout(600)
            c = chips()
            check("on: chip RC on", c.get("API") == "on:RC on", c)
            check("off: chip RC off", c.get("QA") == "off:RC off", c)
            check("a choice waiting for idle: pending chip", c.get("DOCS") == "pending:RC → on", c)
            check("offline: no chip", c.get("MOBILE") is None, c)
            check("DO_NOT_TRACK: RC unavailable chip", c.get("DATA") == "off:RC unavailable", c)
            check("no horizontal scroll", not page.evaluate(OVERFLOW_JS))
            card = page.locator(".scard", has=page.locator(".code", has_text="DOCS")).first
            card.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("detail row explains the pending change",
                  "Off now; turns on when the session is idle" in card.locator(".rc-ctl").text_content(), card.locator(".rc-ctl").text_content())
            card.locator(".rc-ctl button").click()
            page.wait_for_timeout(200)
            check("toggle back: no longer pending", chips().get("DOCS") == "off:RC off", chips())
            check("  demo toast", any("Demo:" in t for t in page.locator(".toast").all_text_contents()))
            qa = page.locator(".scard", has=page.locator(".code", has_text="QA")).first
            qa.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("compact: last compacted note", "Last compacted 50m ago" in qa.locator(".compact-ctl").text_content(), qa.locator(".compact-ctl").text_content())
            qa.locator("[data-compact=ask]").click()
            page.wait_for_timeout(100)
            qa = page.locator(".scard", has=page.locator(".code", has_text="QA")).first
            check("compact asks first", "Compact QA's conversation?" in qa.locator(".compact-ctl").text_content())
            qa.locator("[data-compact=cancel]").click()
            page.wait_for_timeout(100)
            qa = page.locator(".scard", has=page.locator(".code", has_text="QA")).first
            check("  cancel: back to the button", qa.locator("[data-compact=ask]").count() == 1)
            qa.locator("[data-compact=ask]").click()
            page.wait_for_timeout(100)
            page.locator(".scard", has=page.locator(".code", has_text="QA")).first.locator("[data-compact=confirm]").click()
            page.wait_for_timeout(200)
            qa = page.locator(".scard", has=page.locator(".code", has_text="QA")).first
            check("  confirmed: compacted just now", "Last compacted just now" in qa.locator(".compact-ctl").text_content(), qa.locator(".compact-ctl").text_content())
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            page.click("#nf-rc-all-on")
            page.wait_for_timeout(100)
            check("all on asks first", page.locator("#nf-rc-ask").count() == 1 and "every session" in page.text_content("#nf-rc-ask"))
            check("  nothing changed yet", chips().get("QA") == "off:RC off", chips())
            page.click("#nf-rc-cancel")
            page.wait_for_timeout(100)
            check("cancel: back to the two buttons", page.locator("#nf-rc-ask").count() == 0 and page.locator("#nf-rc-all-on").count() == 1)
            page.click("#nf-rc-all-on")
            page.click("#nf-rc-confirm")
            page.wait_for_timeout(200)
            check("confirmed: state says all on", page.text_content("#nf-rc-state").startswith("All sessions: on."), page.text_content("#nf-rc-state"))
            c = chips()
            check("  sessions that are off now wait for idle", c.get("QA") == "pending:RC → on" and c.get("DOCS") == "pending:RC → on", c)
            check("  sessions that are on stay on", c.get("API") == "on:RC on", c)
            check("dialog fits a phone", not page.evaluate("() => { const d = document.getElementById('notif'); return d.querySelector('.info-in').scrollWidth > d.clientWidth + 1; }"))
            page.keyboard.press("Escape")
            page.evaluate("() => { localStorage.setItem('switchboard.lang', 'zh-TW'); }")
            page.reload()
            page.wait_for_timeout(600)
            c = chips()
            check("zh-TW: chips translated", c.get("API") == "on:RC 開" and c.get("QA") == "off:RC 關", c)
            page.evaluate("() => { localStorage.removeItem('switchboard.lang'); }")

            print("local adapter (board-serve, approver):")
            srv, base, home = serve(os.path.join(tmp, "a"), ["local"], live.pid)
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            c = chips()
            check("running session without Remote Control: RC off", c.get("CP") == "off:RC off", c)
            check("offline fleet session: no chip", c.get("QA") is None, c)
            card = page.locator(".scard", has=page.locator(".code", has_text="CP")).first
            card.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            card.locator(".rc-ctl button").click()
            pref = wait_for(lambda: read(os.path.join(home, "rc-prefs.json")).get("sessions", {}).get("CP"))
            check("Turn on → board rc on CP --by board:local", pref and pref.get("state") == "on" and pref.get("by") == "board:local", pref)
            page.wait_for_timeout(500)
            check("  the busy session shows the pending chip", chips().get("CP") == "pending:RC → on", chips())
            ev = wait_for(lambda: [json.loads(x) for x in open(os.path.join(home, "events.jsonl")) if '"op": "run"' in x])
            check("  applied in the background: deferred while busy, nothing typed",
                  ev and ev[-1].get("result") == "deferred", ev)
            card = page.locator(".scard", has=page.locator(".code", has_text="CP")).first
            card.locator("[data-compact=ask]").click()
            page.wait_for_timeout(100)
            page.locator(".scard", has=page.locator(".code", has_text="CP")).first.locator("[data-compact=confirm]").click()
            qd = wait_for(lambda: read(os.path.join(home, "session-actions.json")).get("CP"))
            check("Compact (confirmed) → queued by board:local", qd and qd.get("action") == "compact" and qd.get("by") == "board:local", qd)
            page.wait_for_timeout(1500)
            card = page.locator(".scard", has=page.locator(".code", has_text="CP")).first
            check("  the busy session shows it queued", "Queued: runs when the session is idle." in card.locator(".compact-ctl").text_content(),
                  card.locator(".compact-ctl").text_content())
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            page.click("#nf-rc-all-off")
            page.click("#nf-rc-confirm")
            allp = wait_for(lambda: (read(os.path.join(home, "rc-prefs.json")).get("all") or {}).get("state"))
            check("all off (confirmed) → board rc off all", allp == "off", read(os.path.join(home, "rc-prefs.json")))
            check("  replaces the per-session choice", read(os.path.join(home, "rc-prefs.json")).get("sessions") == {})
            page.wait_for_timeout(400)
            check("  CP is off and wants off: plain off chip", chips().get("CP") == "off:RC off", chips())
            page.keyboard.press("Escape")
            srv.terminate()
            srv.wait()

            print("local adapter (board-serve, not an approver):")
            srv, base, home = serve(os.path.join(tmp, "b"), ["boss@example.com"], live.pid)
            page.goto("about:blank")
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            card = page.locator(".scard", has=page.locator(".code", has_text="CP")).first
            card.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("non-approver: chip shown", chips().get("CP") == "off:RC off", chips())
            check("  no toggle", card.locator(".rc-ctl button").count() == 0)
            check("  no compact button", card.locator("[data-compact]").count() == 0)
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            check("  all switch read-only", page.locator("#nf-rc-readonly").count() == 1 and page.locator("#nf-rc-all-on").count() == 0)

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv and srv.poll() is None:
            srv.terminate()
            srv.wait()
        live.kill()
        live.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
