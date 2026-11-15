#!/usr/bin/env python3
"""tests/test_page_scrolled.py — optional headless check of what the board shows when a press
found the session's screen scrolled (board press exit 7) or the decision went stale while the
session still waits on the same prompt: a note to answer in the session, never a dead link, and a
note on the decision card that can be answered again.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0. CI does not run it. PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless
shell. Uses a real bin/board-serve on a temporary BOARD_HOME; never touches ~/.agent-switchboard.

  python3 tests/test_page_scrolled.py [-v]
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


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-scrolled-test.")
    srv = None
    try:
        home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
        for x in (home, cfg, sess, os.path.join(home, "decisions")):
            os.makedirs(x)
        port = free_port()
        with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
            fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
        now = int(time.time() * 1000)

        def row(code, sid, dec_id, tu):
            return {"code": code, "aliases": [code], "name": code, "pid": 1, "session_id": sid, "tmux": code + ":@1.%1",
                    "tmux_pane": "%1", "status": "waiting", "state": "needs-you", "remote_url": None, "updated_at": now,
                    "title": "Pick a color", "repo": "app", "branch": "main", "last_event": "PreToolUse",
                    "notification_type": None, "recent": [],
                    "pending": {"tool_use_id": tu, "tool_name": "AskUserQuestion", "summary": "Which color?", "decision_id": dec_id}}

        def dec(did, status, tu, sid, note):
            d = {"id": did, "kind": "question", "source": "pane", "from": None, "session_id": sid, "tool_use_id": tu,
                 "tool_name": "AskUserQuestion", "question": "Which color?", "options": ["Red", "Blue"],
                 "questions": [{"question": "Which color?", "header": "Color", "multiSelect": False,
                                "options": [{"label": "Red", "description": ""}, {"label": "Blue", "description": ""}]}],
                 "pressable": True, "status": status, "answer": None, "answered_by": None, "created_at": now - 60000,
                 "updated_at": now}
            if note:
                d["press_note"] = "The session's screen is scrolled or covered; try again, or answer in the session."
            with open(os.path.join(home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh)

        # INFRA: the press found the screen scrolled, the decision went stale (older board) but the
        # session still waits on that prompt. OPS: pending again after exit 7 (press_note). WEB: stale
        # without a note.
        dec("d-1-infra", "stale", "tu_i", "s-infra", True)
        dec("d-2-ops", "pending", "tu_o", "s-ops", True)
        dec("d-3-web", "stale", "tu_w", "s-web", False)
        snap = {"generated_at": now, "decisions": [], "tasks": [],
                "sessions": [row("INFRA", "s-infra", "d-1-infra", "tu_i"), row("OPS", "s-ops", "d-2-ops", "tu_o"),
                             row("WEB", "s-web", "d-3-web", "tu_w")]}
        with open(os.path.join(home, "snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
        env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess)
        env.pop("TMUX", None)
        srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []
            for vw in (1280, 390):
                print("width %d:" % vw)
                ctx = browser.new_context(viewport={"width": vw, "height": 900})
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto("http://127.0.0.1:%d/" % port)
                pg.wait_for_timeout(1000)
                for code in ("INFRA", "OPS", "WEB"):
                    pg.click('article[aria-label="Session %s"] .scard-toggle' % code)
                    pg.wait_for_timeout(100)

                def card(code):
                    return pg.evaluate("""(code) => {
                      const c = document.querySelector('article[aria-label="Session ' + code + '"]');
                      return { links: [...c.querySelectorAll('.sdetail a[href^="#dec-"]')].map((a) => a.getAttribute('href')),
                               note: (c.querySelector('.sdetail .in-session') || {}).textContent || null,
                               text: c.querySelector('.sdetail').textContent };
                    }""", code)
                c = card("INFRA")
                check("stale, still waiting, press found it scrolled: a note, no dead link",
                      c["links"] == [] and c["note"] == "Answer in the session (screen scrolled)", c)
                c = card("WEB")
                check("stale, still waiting, no press note: answer-in-session note, no dead link",
                      c["links"] == [] and c["note"] == "Answer in the session: the board can no longer press this prompt", c)
                c = card("OPS")
                check("pending again after a scrolled press: still a link, with a retry note",
                      c["links"] == ["#dec-d-2-ops"] and c["note"] is None and "screen was scrolled; try again" in c["text"], c)
                dc = pg.evaluate("() => { const d = document.getElementById('dec-d-2-ops'); return d ? [d.textContent, d.querySelectorAll('button').length] : null; }")
                check("the decision card says the screen was scrolled and keeps its buttons",
                      dc is not None and "screen was scrolled, so nothing was pressed" in dc[0] and dc[1] >= 2, dc)
                check("no sideways scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
                pg.close()
                ctx.close()
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
