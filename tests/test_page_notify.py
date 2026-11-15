#!/usr/bin/env python3
"""tests/test_page_notify.py — optional headless check of the board page's notification control
(the Notifications dialog, the muted banner and the presence heartbeat) in artifact/board.html.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_notify.py [-v]

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


VIEW_JS = """() => {
  const b = document.getElementById('mute-banner');
  const d = document.getElementById('notif');
  return {
    banner: b ? b.textContent : null,
    label: document.getElementById('notif-label').textContent,
    open: d.open,
    overflow: document.scrollingElement.scrollWidth > innerWidth + 1,
    dialogOverflow: d.open ? d.querySelector('.info-in').scrollWidth > d.clientWidth + 1 : false,
    readonly: !!document.getElementById('notif-readonly'),
    disabled: d.open ? [...d.querySelectorAll('button:not(#notif-close), select, input')].every((x) => x.disabled) : null,
    toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
  };
}"""


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


def wait_for(fn, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def serve(tmp, approvers):
    """A board-serve on a fresh BOARD_HOME with these approvers. Returns (proc, base, home)."""
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (json.dumps(approvers), port))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess)
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
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
    tmp = tempfile.mkdtemp(prefix="switchboard-page-notify.")
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
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731

            print("demo adapter (phone width):")
            page.goto(PAGE + "?demo=1")
            page.wait_for_timeout(600)
            s = view()
            check("muted banner names the channel and the time", (s["banner"] or "").startswith("Slack muted until "), s)
            check("nav button says something is muted", s["label"] == "1 muted", s)
            check("no horizontal scroll", not s["overflow"], s)
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            s = view()
            check("dialog opens", s["open"], s)
            check("dialog fits a phone (no sideways scroll)", not s["dialogOverflow"], s)
            check("demo is editable", not s["readonly"] and s["disabled"] is False, s)
            check("email shows its own floor", "urgent only" in page.text_content("#nf-email .nf-state"))
            check("unconfigured channel says so", page.text_content("#nf-sns .nf-state") == "not configured")
            check("breakthrough rule explained", "break through" in page.text_content("#notif-body .bt"))
            page.click("#notif-mute-all")
            page.wait_for_timeout(200)
            s = view()
            check("mute all: banner", "All push muted until" in (s["banner"] or ""), s)
            check("mute all: nav label", s["label"] == "All muted", s)
            page.select_option("#notif-dur", "")
            page.click("#notif-unmute-all")
            page.wait_for_timeout(200)
            check("unmute all: banner gone", view()["banner"] is None, view())
            page.click("#nf-slack button")
            page.wait_for_timeout(200)
            check("mute one channel until I unmute", view()["banner"] == "Slack muted until you unmute", view())
            page.emulate_media(color_scheme="dark")
            page.wait_for_timeout(100)
            bg = page.evaluate("() => getComputedStyle(document.querySelector('.banner.mute')).backgroundColor")
            check("dark theme: the muted banner uses the dark palette", bg == "rgb(51, 37, 15)", bg)
            page.emulate_media(color_scheme="light")
            page.keyboard.press("Escape")

            print("local adapter (board-serve, approver):")
            srv, base, home = serve(os.path.join(tmp, "a"), ["local"])
            page.goto(base + "/")
            pres = wait_for(lambda: read(os.path.join(home, "presence.json")).get("local"))
            check("first state: a presence heartbeat under the server's identity",
                  pres and pres.get("visible") is True and pres.get("via") == "loopback", pres)
            page.click("#notif-btn")
            page.wait_for_timeout(300)
            s = view()
            check("approver: controls enabled", not s["readonly"] and s["disabled"] is False, s)
            page.click("#notif-mute-all")
            prefs = wait_for(lambda: read(os.path.join(home, "notify-prefs.json")).get("mutes", {}).get("all"))
            check("Mute all → board mute all --by board:local", prefs and prefs.get("by") == "board:local", prefs)
            check("  for 1 hour by default", prefs and 3500000 < prefs["until"] - int(time.time() * 1000) <= 3600000, prefs)
            page.wait_for_timeout(300)
            check("  banner shows it", "All push muted until" in (view()["banner"] or ""), view())
            page.select_option("#nf-level-slack", "high")
            lv = wait_for(lambda: read(os.path.join(home, "notify-prefs.json")).get("min_level", {}).get("slack"))
            check("slack min level select → board notify-level slack high", lv == "high", lv)
            page.wait_for_timeout(300)
            page.uncheck("#notif-presence")
            red = wait_for(lambda: read(os.path.join(home, "notify-prefs.json")).get("presence", {}).get("reduce") is False)
            check("presence toggle → board notify-presence off", red, read(os.path.join(home, "notify-prefs.json")))
            page.wait_for_timeout(300)
            page.click("#notif-unmute-all")
            gone = wait_for(lambda: read(os.path.join(home, "notify-prefs.json")).get("mutes") == {})
            check("Unmute all", gone, read(os.path.join(home, "notify-prefs.json")))
            page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
                                     document.dispatchEvent(new Event('visibilitychange')); }""")
            hidden = wait_for(lambda: read(os.path.join(home, "presence.json")).get("local", {}).get("visible") is False)
            check("tab hidden → heartbeat says not visible", hidden, read(os.path.join(home, "presence.json")))
            srv.terminate()
            srv.wait()

            print("local adapter (board-serve, not an approver):")
            srv, base, home = serve(os.path.join(tmp, "b"), ["boss@example.com"])
            page.goto("about:blank")
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            page.click("#notif-btn")
            page.wait_for_timeout(200)
            s = view()
            check("non-approver: read-only note", s["readonly"], s)
            check("  every control disabled", s["disabled"] is True, s)
            check("  no presence heartbeat sent", not os.path.exists(os.path.join(home, "presence.json")))

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv and srv.poll() is None:
            srv.terminate()
            srv.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
