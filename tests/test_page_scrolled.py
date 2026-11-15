#!/usr/bin/env python3
"""tests/test_page_scrolled.py — optional headless check of what the board shows when a press
found the session's screen scrolled (board press exit 7) or the decision went stale while the
session still waits on the same prompt: a note to answer in the session, never a dead link, and a
note on the decision card that can be answered again; and the Recent decisions log, where an answer
that was recorded but never delivered (stale, partial) says "Not delivered: <reason>" with its own
chip, unlike a relayed one.

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

        def dec(did, status, tu, sid, note, **extra):
            d = {"id": did, "kind": "question", "source": "pane", "from": None, "session_id": sid, "tool_use_id": tu,
                 "tool_name": "AskUserQuestion", "question": "Which color?", "options": ["Red", "Blue"],
                 "questions": [{"question": "Which color?", "header": "Color", "multiSelect": False,
                                "options": [{"label": "Red", "description": ""}, {"label": "Blue", "description": ""}]}],
                 "pressable": True, "status": status, "answer": None, "answered_by": None, "created_at": now - 60000,
                 "updated_at": now}
            if note:
                d["press_note"] = "The session's screen is scrolled or covered; try again, or answer in the session."
            d.update(extra)
            with open(os.path.join(home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh)

        # INFRA: the press found the screen scrolled, the decision went stale (older board) but the
        # session still waits on that prompt. OPS: pending again after exit 7 (press_note). WEB: stale
        # without a note.
        dec("d-1-infra", "stale", "tu_i", "s-infra", True)
        dec("d-2-ops", "pending", "tu_o", "s-ops", True)
        dec("d-3-web", "stale", "tu_w", "s-web", False)
        # Recent decisions: an answer refused at the press (stale + screen), a free text stopped
        # part-way (partial), one relayed, and an old record with no code whose reason is only in
        # the mark event (board-serve derives "screen" from it). API's prompt still waits on d-7.
        dec("d-4-a", "stale", "tu_4", "s-x", False, answer="Blue", answered_by="local", answered_at=now - 5000,
            status_reason="screen", **{"from": "QA"})
        dec("d-5-b", "stale", "tu_5", "s-x", False, answer="purple", answered_by="local", answered_at=now - 4000,
            status_reason="partial", **{"from": "QA"})
        dec("d-6-c", "relayed", "tu_6", "s-x", False, answer="Red", answered_by="local", answered_at=now - 3000,
            relayed_at=now - 2900, **{"from": "QA"})
        # A record whose status_reason is free text (a db written by hand): the page words only codes.
        dec("d-8-x", "stale", "tu_8", "s-x", False, answer="Green", answered_by="local", answered_at=now - 1000,
            status_reason="press refused: no picker in pane %10 SECRET-ish text", **{"from": "QA"})
        dec("d-7-api", "stale", "tu_7", "s-api", False, answer="Follow the earlier approach (Recommended)",
            answered_by="local", answered_at=now - 2000)
        with open(os.path.join(home, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 1900, "type": "mark", "id": "d-7-api", "status": "stale",
                                 "reason": "press refused: no question picker visible in pane %10"}) + "\n")
        snap = {"generated_at": now, "decisions": [], "tasks": [],
                "sessions": [row("INFRA", "s-infra", "d-1-infra", "tu_i"), row("OPS", "s-ops", "d-2-ops", "tu_o"),
                             row("WEB", "s-web", "d-3-web", "tu_w"), row("API", "s-api", "d-7-api", "tu_7")]}
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
                if errors:
                    print("page errors:", errors)
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
            print("recent decisions log:")
            ctx = browser.new_context(viewport={"width": 390, "height": 900})
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(1000)
            pg.evaluate("() => { const d = document.getElementById('dec-history'); d.hidden = false; d.open = true; }")
            rows = pg.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#dec-history-list li')].map((li) => {
              const what = li.querySelector('.what').textContent;
              return [what.match(/→ (.*)$/) ? what.match(/→ (.*)$/)[1] : what, [li.querySelector('.status').textContent, li.querySelector('.status').className, what]];
            }))""")
            blue, purple, red = rows.get("Blue"), rows.get("purple"), rows.get("Red")
            check("stale with an answer: 'not delivered' chip, the reason before the answer",
                  blue and blue[0] == "not delivered" and "undelivered" in blue[1]
                  and "Not delivered: the prompt was not on the session's screen → Blue" in blue[2], blue)
            check("partial: its own chip and reason", purple and purple[0] == "partial"
                  and "Not delivered: typed into the question, but Enter was not pressed" in purple[2], purple)
            check("relayed: the relayed chip, no 'not delivered'", red and red[0] == "relayed" and "Not delivered" not in red[2], red)
            gr = rows.get("Green")
            check("a free-text status_reason is never shown: 'reason not recorded'",
                  gr and "Not delivered: reason not recorded" in gr[2] and "SECRET" not in gr[2] and "%10" not in gr[2], gr)
            old = rows.get("Follow the earlier approach (Recommended)")
            check("an old record: the reason comes from the mark event (never its text)",
                  old and old[0] == "not delivered" and "the prompt was not on the session's screen" in old[2]
                  and "%10" not in old[2] and "picker" not in old[2], old)
            pg.click('article[aria-label="Session API"] .scard-toggle')
            pg.wait_for_timeout(150)
            note = pg.evaluate("() => (document.querySelector('article[aria-label=\"Session API\"] .sdetail .in-session') || {}).textContent || null")
            check("the session card says the answer was not delivered", note == "Your answer was not delivered (the prompt was not on the session's screen). Answer in the session.", note)
            pg.evaluate("() => { const b = document.querySelector('input[name=\"sb-lang\"][value=\"zh-TW\"]'); if (b) b.click(); }")
            pg.wait_for_timeout(300)
            zh = pg.evaluate("() => [...document.querySelectorAll('#dec-history-list li .status.undelivered')].map((e) => e.textContent)")
            check("zh-TW: the chips are translated", "未送達" in zh and "未完成" in zh, zh)
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
