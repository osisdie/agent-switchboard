#!/usr/bin/env python3
"""tests/test_page_askers.py — optional headless check of the board page for an asker (fleet.yaml
`askers`): the message composer is shown with the asker note, the decision answer buttons are not,
and the profile menu shows the asker role. An approver on the same board still gets the buttons.
The Claude account email in the usage popover is shown to owners only: an approver who is not an
owner sees "hidden" (with no owners configured, approvers count as owners).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_askers.py [-v]

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


def serve(tmp, role):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess, os.path.join(home, "decisions")):
        os.makedirs(d)
    port = free_port()
    approvers = ["boss@example.invalid"] + (["local"] if role.startswith("approver") or role == "owner" else [])
    askers = ["local"] if role == "asker" else []
    owners = {"owner": ["local"], "approver, not owner": ["boss@example.invalid"]}.get(role)
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        if owners:
            fh.write("owners: %s\n" % json.dumps(owners))
        fh.write("approvers: %s\naskers: %s\nsessions:\n  - code: API\n    cwd: /w\nserve:\n  enabled: true\n  port: %d\n"
                 % (json.dumps(approvers), json.dumps(askers), port))
    now = int(time.time() * 1000)
    # the collector reads the account from CLAUDE_JSON: a fixture, never the real ~/.claude.json
    with open(os.path.join(tmp, "claude.json"), "w", encoding="utf-8") as fh:
        json.dump({"oauthAccount": {"emailAddress": ACCOUNT, "organizationName": "Example Org"}}, fh)
    with open(os.path.join(home, "usage.json"), "w", encoding="utf-8") as fh:
        json.dump({"five_hour": {"pct": 42, "resets_at": now + 3600000}, "seven_day": {"pct": 7, "resets_at": now + 86400000},
                   "updated_at": now}, fh)
    with open(os.path.join(home, "decisions", "d-1-ask.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": "d-1-ask", "kind": "question", "from": "API", "question": "Ship it?", "options": ["yes", "no"],
                   "status": "pending", "answer": None, "answered_by": None, "answered_at": None, "relayed_at": None,
                   "created_at": now}, fh)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(tmp, "claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
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


ACCOUNT = "account-holder@example.invalid"
VIEW_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const card = document.getElementById('dec-d-1-ask');
  const btns = card ? [...card.querySelectorAll('.opts button')].filter(vis).map((b) => b.textContent.trim()) : null;
  return { composer: vis(document.getElementById('composer')), note: vis(document.getElementById('msg-asker-note')),
           readonly: vis(document.getElementById('msg-readonly')), card: !!card, buttons: btns,
           acct: (document.querySelector('.u-acct') || {}).textContent || null, html: document.documentElement.outerHTML,
           cardText: card ? card.textContent : '' };
}"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-askers.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            for role in ("asker", "approver", "approver, not owner", "owner"):
                print("local adapter (%s):" % role)
                srv, base, home = serve(os.path.join(tmp, role), role)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(base + "/")
                page.wait_for_selector("#dec-d-1-ask", timeout=10000)
                page.wait_for_function("() => !document.getElementById('composer').hidden || "
                                       "!document.getElementById('msg-readonly').hidden", timeout=10000)
                page.hover("#usage")                         # the usage popover is built when it opens
                page.wait_for_selector("#usage-pop .u-acct", timeout=5000)
                v = page.evaluate(VIEW_JS)
                check("%s: composer shown" % role, v["composer"], v)
                if role == "asker":
                    check("asker: the asker note is shown", v["note"], v)
                    check("asker: no answer buttons on the decision", v["buttons"] == [], v)
                    page.fill("#msg-input", "@API is the export done?")
                    page.click("#msg-send")
                    page.wait_for_function("() => document.querySelectorAll('#threads li').length > 0", timeout=10000)
                    names = [n for n in os.listdir(os.path.join(home, "messages")) if n.endswith(".json")]
                    rec = json.load(open(os.path.join(home, "messages", names[0]), encoding="utf-8")) if names else {}
                    check("asker: the composer sends a labelled asker message",
                          rec.get("kind") == "asker" and rec.get("to") == "API", rec)
                    page.click("#acct-btn")
                    roles = page.evaluate("() => [...document.querySelectorAll('#acct-roles li')].map((l) => l.textContent)")
                    check("asker: the profile shows the asker role", roles == ["Asker (messages only)"], roles)
                else:
                    check("%s: no asker note" % role, not v["note"], v)
                    check("%s: answer buttons shown" % role, v["buttons"] == ["yes", "no"], v)
                if role in ("asker", "approver, not owner"):     # other people's emails: owners only
                    check("%s: no one else's email on the page" % role, "boss@example.invalid" not in v["html"],
                          [x for x in v["html"].split() if "@example.invalid" in x][:3])
                sees = role in ("approver", "owner")          # "approver": no owners set, so the fallback
                check("%s: account email %s" % (role, "shown" if sees else "hidden"),
                      (ACCOUNT in (v["acct"] or "")) == sees and (ACCOUNT in v["html"]) == sees,
                      v["acct"])
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
