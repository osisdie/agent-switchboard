#!/usr/bin/env python3
"""tests/test_page_resume.py — optional headless check of the usage-limit controls in
artifact/board.html: the "usage limit" chip on a session card, the owner's Resume row (with a
confirm step) and the "Resume all" banner; non-owners see the chip and the banner text only.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_resume.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a real bin/board-serve
on a temporary BOARD_HOME, with tests/fake_tmux.py as tmux (no screen: every resume is refused,
nothing sent); never touches ~/.agent-switchboard.
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
  const chip = c.querySelector('.lchip');
  return [c.querySelector('.code').textContent, chip ? chip.textContent : null];
}))"""
OVERFLOW_JS = "() => document.scrollingElement.scrollWidth > innerWidth + 1"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def events(home, kind):
    try:
        with open(os.path.join(home, "events.jsonl"), encoding="utf-8") as fh:
            return [e for e in (json.loads(x) for x in fh) if e.get("type") == kind]
    except (OSError, ValueError):
        return []


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def serve(tmp, owners, live_pid):
    """board-serve on a fresh BOARD_HOME: session CP runs, and the last look saw it at a limit."""
    home, cfg, sess, fbin = (os.path.join(tmp, x) for x in ("home", "config", "sessions", "bin"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess, fbin):
        os.makedirs(d)
    with open(os.path.join(fbin, "tmux"), "w") as fh:
        fh.write('#!/bin/sh\nexec %s %s "$@"\n' % (sys.executable, os.path.join(REPO, "tests", "fake_tmux.py")))
    os.chmod(os.path.join(fbin, "tmux"), 0o755)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("owners: %s\napprovers: [local]\nserve:\n  enabled: true\n  port: %d\n"
                 "session_actions:\n  lock_wait_seconds: 1\nsessions:\n  - code: CP\n    cwd: /w\n  - code: QA\n    cwd: /w\n"
                 % (json.dumps(owners), port))
    with open(os.path.join(sess, "%d.json" % live_pid), "w") as fh:
        with open("/proc/%d/stat" % live_pid) as st:
            stat = st.read()
        json.dump({"pid": live_pid, "sessionId": "s-cp", "name": "CP", "tmux": "CP:@1.%42", "status": "idle",
                   "bridgeSessionId": None, "startedAt": 1,
                   "procStart": str(int(stat[stat.rindex(")") + 2:].split()[19]))}, fh)
    now = int(time.time() * 1000)
    with open(os.path.join(home, "resume-scan.json"), "w") as fh:
        json.dump({"at": now, "sessions": {"CP": {"sid": "s-cp", "state": "wait", "since": now - 60000, "at": now}}}, fh)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess,
               CLAUDE_JSON=os.path.join(home, "no-claude.json"), PATH=fbin + os.pathsep + os.environ["PATH"])
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS", "TMUX", "FAKE_TMUX")):
            env.pop(k)
    env.update({"ALERT_DESKTOP": "0", "BOARD_ACTIONS_AUTO": "0", "FAKE_TMUX_LOG": os.path.join(tmp, "tmux.log")})
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


def sent_keys(tmp):
    try:
        with open(os.path.join(tmp, "tmux.log"), encoding="utf-8") as fh:
            return [x for x in fh if x.startswith("send-keys")]
    except OSError:
        return []


def card(page, code):
    return page.locator(".scard", has=page.locator(".code", has_text=code)).first


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-resume.")
    srv = None
    live = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            check("the session at a limit has the chip", c.get("DATA") == "usage limit", c)
            check("  the others have none", all(v is None for k, v in c.items() if k != "DATA"), c)
            check("banner names it", "1 session stopped at a usage limit: DATA" in (page.text_content("#limit-banner") or ""))
            check("no horizontal scroll", not page.evaluate(OVERFLOW_JS))
            d = card(page, "DATA")
            d.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("details: the state in words", "Stopped at the usage limit; the prompt is empty." in d.locator(".resume-ctl").text_content(),
                  d.locator(".resume-ctl").text_content())
            d.locator("[data-resume=ask]").click()
            page.wait_for_timeout(100)
            d = card(page, "DATA")
            check("Resume asks first", "Resume DATA?" in d.locator(".resume-ctl").text_content())
            d.locator("[data-resume=cancel]").click()
            page.wait_for_timeout(100)
            check("  cancel: back to the button", card(page, "DATA").locator("[data-resume=ask]").count() == 1)
            page.click("[data-resume=all]")
            page.wait_for_timeout(100)
            check("Resume all asks first", "Resume them all?" in page.text_content("#limit-banner"))
            page.click("[data-resume=all-cancel]")
            page.wait_for_timeout(100)
            check("  cancel: the button again", page.locator("[data-resume=all]").count() == 1)
            page.click("[data-resume=all]")
            page.click("[data-resume=all-confirm]")
            page.wait_for_timeout(200)
            check("confirmed (demo): chip and banner gone", chips().get("DATA") is None and page.locator("#limit-banner").count() == 0, chips())
            check("  demo toast", any("Demo:" in t for t in page.locator(".toast").all_text_contents()))
            page.evaluate("() => { localStorage.setItem('switchboard.lang', 'zh-TW'); }")
            page.reload()
            page.wait_for_timeout(600)
            check("zh-TW: chip and banner translated", chips().get("DATA") == "用量上限"
                  and "1 個 session 停在用量上限：DATA" in (page.text_content("#limit-banner") or ""),
                  (chips(), page.text_content("#limit-banner")))
            check("  zh-TW: Resume all", page.text_content("[data-resume=all]") == "全部繼續")
            page.evaluate("() => { localStorage.removeItem('switchboard.lang'); }")

            print("local adapter (board-serve, owner):")
            srv, base, home = serve(os.path.join(tmp, "a"), ["local"], live.pid)
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            c = chips()
            check("collector's look → chip on CP", c.get("CP") == "usage limit", c)
            check("  offline QA: none", c.get("QA") is None, c)
            cp = card(page, "CP")
            cp.locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("  details say it waits to continue", "Claude waits to continue automatically" in card(page, "CP").locator(".resume-ctl").text_content())
            card(page, "CP").locator("[data-resume=ask]").click()
            page.wait_for_timeout(100)
            card(page, "CP").locator("[data-resume=confirm]").click()
            ev = wait_for(lambda: events(home, "resume"))
            check("Resume (confirmed) → board resume CP --by board:local", ev and ev[-1].get("code") == "CP"
                  and ev[-1].get("by") == "board:local", ev)
            check("  the pane shows no limit screen here: refused, nothing sent",
                  ev and ev[-1].get("result") == "refused" and not sent_keys(os.path.join(tmp, "a")), (ev, sent_keys(os.path.join(tmp, "a"))))
            audit = [e for e in events(home, "serve") if e.get("action") == "resume"]
            check("  audited by board-serve", audit and audit[-1].get("result") == "ok", audit)
            page.click("[data-resume=all]")
            page.click("[data-resume=all-confirm]")
            audit2 = wait_for(lambda: [e for e in events(home, "serve") if e.get("action") == "resume"][1:])
            check("Resume all (confirmed) → POST /api/resume with exactly the codes shown", audit2 and audit2[-1].get("codes") == ["CP"],
                  events(home, "serve"))
            srv.terminate()
            srv.wait()

            print("local adapter (board-serve, not an owner):")
            srv, base, home = serve(os.path.join(tmp, "b"), ["boss@example.com"], live.pid)
            page.goto("about:blank")
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            check("non-owner: chip shown", chips().get("CP") == "usage limit", chips())
            check("  banner text, no Resume all", "stopped at a usage limit" in (page.text_content("#limit-banner") or "")
                  and page.locator("[data-resume=all]").count() == 0)
            card(page, "CP").locator(".scard-toggle").click()
            page.wait_for_timeout(200)
            check("  no Resume button", card(page, "CP").locator("[data-resume]").count() == 0)
            # the first board-serve is stopped while the page still listens to it: that reconnect is expected
            errs = [e for e in errors if "ERR_CONNECTION_REFUSED" not in e]
            check("no page errors", not errs, errs)
    finally:
        if srv:
            srv.terminate()
            srv.wait()
        live.kill()          # the sleep this test started (its recorded pid)
        live.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
