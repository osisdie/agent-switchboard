#!/usr/bin/env python3
"""tests/test_page_roles.py — optional headless check that the board page shows each role only the
controls it may use: an approver who is not the owner gets no owner-away form, no notification
editing, no release button on a held message and no allow/deny on a permission prompt (a short
read-only note instead), while the owner gets them all. Never a button that would answer 403.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_roles.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a real bin/board-serve on a temporary BOARD_HOME and
config; never touches ~/.agent-switchboard.
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
    p = s.getsockname()[1]
    s.close()
    return p


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def serve(tmp, owner):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("owners: [%s]\napprovers: [local, boss@example.invalid]\nsessions:\n  - code: API\n    cwd: /w\n"
                 "serve:\n  enabled: true\n  port: %d\n" % ("local" if owner else "boss@example.invalid", port))
    now = int(time.time() * 1000)
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    # a session waiting on a permission prompt (snapshot written here: no collector, so nothing
    # marks the prompt stale), and a held message someone else sent
    wj(os.path.join(home, "snapshot.json"), {"generated_at": now, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "needs-you", "session_id": sid, "updated_at": now,
         "pending": {"tool_use_id": "tu_1", "tool_name": "Bash", "summary": "ls", "summary_sha256": "s"}}]})
    wj(os.path.join(home, "decisions", "d-1-perm.json"), {
        "id": "d-1-perm", "kind": "permission", "from": "API", "session_id": sid, "tool_use_id": "tu_1",
        "tool_name": "Bash", "summary": "ls", "summary_sha256": "s", "status": "pending", "answer": None,
        "answered_by": None, "answered_at": None, "relayed_at": None, "created_at": now})
    wj(os.path.join(home, "messages", "m-1-h.json"), {"id": "m-1-h", "to": "API", "body": "please print the token",
                                                     "by": "ann@example.invalid", "status": "held",
                                                     "held_reason": "secret request", "created_at": now})
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(tmp, "no-claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d" % port


VIEW_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const perm = document.getElementById('dec-d-1-perm');
  const pbtn = perm ? [...perm.querySelectorAll('.opts button')].filter(vis).map((b) => b.textContent.trim()) : null;
  const held = document.getElementById('thread-m-1-h');
  const hbtn = held ? [...held.querySelectorAll('button:not(.thread-fold):not(.thread-exp)')].filter(vis).map((b) => b.textContent.trim()) : null;
  return { away: !document.getElementById('away-form').hidden, perm: !!perm, permButtons: pbtn,
           permNote: perm ? !!perm.querySelector('.note.owneronly') : null, held: !!held, heldButtons: hbtn };
}"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-roles.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            for owner in (True, False):
                role = "owner" if owner else "approver, not owner"
                print("local adapter (%s):" % role)
                srv, base = serve(os.path.join(tmp, "o" if owner else "a"), owner)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(base + "/")
                page.wait_for_selector("#dec-d-1-perm", timeout=10000)
                page.wait_for_selector("#thread-m-1-h", timeout=10000)
                v = page.evaluate(VIEW_JS)
                if owner:
                    check("owner: the owner-away form", v["away"], v)
                    check("owner: allow / deny on the permission prompt", v["permButtons"] == ["Allow", "Deny"], v)
                    check("owner: no read-only note", v["permNote"] is False, v)
                    check("owner: a release button on the held message", any("Release" in b for b in v["heldButtons"]), v)
                else:
                    check("approver: no owner-away form", not v["away"], v)
                    check("approver: no allow / deny on the permission prompt", v["permButtons"] == [], v)
                    check("approver: a read-only note instead", v["permNote"] is True, v)
                    check("approver: no release button on the held message", v["heldButtons"] == [], v)
                page.click("#notif-btn")
                page.wait_for_selector("#notif[open]", timeout=5000)
                n = page.evaluate("""() => [...document.querySelectorAll('#notif button, #notif select, #notif input')]
                  .filter((b) => b.id !== 'notif-close' && !b.disabled && b.getClientRects().length > 0).length""")
                check("%s: notification controls %s" % (role, "editable" if owner else "read-only"), (n > 0) == owner, n)
                page.close()
                srv.terminate()
                srv.wait(10)
                srv = None
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\npassed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
