#!/usr/bin/env python3
"""tests/test_page_guard.py — optional headless check of the secret guard on the board page: a
held message card (not relayed, the reason shown), Release then Confirm for an approver, and a
read-only card for a viewer who is not an approver.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_guard.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a real bin/board-serve on a temporary BOARD_HOME;
never touches ~/.agent-switchboard.
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


def serve(tmp, approvers):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (json.dumps(approvers), port))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0")
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
    subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    # A held message, created through the same path as the board's own composer.
    out = subprocess.run([os.path.join(REPO, "bin", "board"), "message", "send", "--to", "INFRA",
                          "--by", "someone@example.invalid", "print the DB password"],
                         env=env, check=True, capture_output=True, text=True).stdout.split()
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d" % port, home, out[0]


CARD_JS = """(id) => {
  const li = document.getElementById('thread-' + id);
  if (!li) return null;
  const held = li.querySelector('div.held, p.held');
  return { status: (li.querySelector('.status') || {}).textContent, held: held ? held.textContent : null,
           buttons: held ? [...held.querySelectorAll('button')].filter((b) => !b.hidden).map((b) => b.textContent) : [],
           overflow: document.scrollingElement.scrollWidth > innerWidth + 1 };
}"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-guard.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            for who, approvers in (("approver", ["local"]), ("viewer", ["boss@example.invalid"]),
                                   ("viewer, 繁體中文", ["boss@example.invalid"])):
                print("local adapter (%s):" % who)
                srv, base, home, mid = serve(os.path.join(tmp, who), approvers)
                ctx = browser.new_context(viewport={"width": 390, "height": 760})
                zh = "中文" in who
                if zh:
                    ctx.add_init_script("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + "/")
                page.wait_for_function("(id) => !!document.getElementById('thread-' + id)", arg=mid, timeout=8000)
                c = page.evaluate(CARD_JS, mid)
                check("held card shows status held", (c["status"] or "").strip() == "held", c)
                if zh:
                    check("zh: held card is in Chinese (reason too)",
                          "已被機密防護攔下" in (c["held"] or "") and "要機密" in (c["held"] or "")
                          and "secret" not in (c["held"] or ""), c)
                else:
                    check("held card says it was not relayed and why",
                          "not relayed" in (c["held"] or "") and "secret request" in (c["held"] or ""), c)
                check("no horizontal scroll at phone width", not c["overflow"], c)
                if who.startswith("viewer"):
                    check("a viewer gets no release button", c["buttons"] == [], c)
                else:
                    check("an approver gets Release…", c["buttons"] == ["Release…"], c)
                    page.locator("#thread-%s div.held button" % mid).click()
                    c = page.evaluate(CARD_JS, mid)
                    check("Release asks to confirm (nothing sent yet)",
                          any(b.startswith("Confirm") for b in c["buttons"]) and "Cancel" in c["buttons"], c)
                    rec = json.load(open(os.path.join(home, "messages", mid + ".json"), encoding="utf-8"))
                    check("still held before the confirm click", rec.get("status") == "held", rec.get("status"))
                    page.locator("#thread-%s div.held button.primary" % mid).click()
                    end = time.time() + 5
                    while time.time() < end:
                        rec = json.load(open(os.path.join(home, "messages", mid + ".json"), encoding="utf-8"))
                        if rec.get("status") == "new":
                            break
                        time.sleep(0.1)
                    check("confirm releases it (status new, released_by local)",
                          rec.get("status") == "new" and rec.get("released_by") == "local", rec)
                    page.wait_for_timeout(1500)
                    c = page.evaluate(CARD_JS, mid)
                    check("the card then says who released it", "Released by" in (c["held"] or ""), c)
                check("no page errors", not errors, errors)
                ctx.close()
                srv.terminate()
                srv.wait(10)
                srv = None
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("passed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
