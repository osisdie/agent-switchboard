#!/usr/bin/env python3
"""tests/test_page_resolve.py — optional headless check of closing a board ask that was answered in
the session: the "maybe answered in the session" hint, the approver-only "Mark answered in session"
action with its confirm step (POST /api/decisions/<id>/resolve through a real board-serve), no
action on a gate or for a non-approver, the history chip, zh-TW, and phone width. A hinted ask is
de-emphasised and listed last. Pane prompts the collector settled from the session's own report
(lib/reconcile.py): "delivered (finished in the session)" and "the board's was not used" in the
history, and no "not delivered" note on the session card.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0. CI does not run it. PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless
shell. Uses a temporary BOARD_HOME; never touches ~/.agent-switchboard.

  python3 tests/test_page_resolve.py [-v]
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


CARD_JS = """(id) => {
  const c = document.getElementById('dec-' + id);
  if (!c) return null;
  const btn = [...c.querySelectorAll('button')].find((b) => b.textContent === 'Mark answered in session' || b.textContent === '標記為已在 session 中回答');
  return { text: c.textContent, maybe: (c.querySelector('.note.maybe') || {}).textContent || null, resolve: !!btn,
           confirm: (c.querySelector('.confirm-row .q') || {}).textContent || null, free: !!c.querySelector('.free input') };
}"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-resolve-test.")
    srv = None
    try:
        home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
        for x in (home, cfg, sess, os.path.join(home, "decisions")):
            os.makedirs(x)
        port = free_port()

        def write_cfg(approvers):
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [%s]\nserve:\n  enabled: true\n  port: %d\n" % (approvers, port))
        write_cfg("local")
        now = int(time.time() * 1000)

        def dec(did, **kw):
            d = {"id": did, "kind": "question", "from": "CP", "question": "Ship it behind a flag?", "options": ["yes", "no"],
                 "status": "pending", "answer": None, "answered_by": None, "answered_at": None, "relayed_at": None,
                 "created_at": now - 600000}
            d.update(kw)
            with open(os.path.join(home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh)
        dec("d-1-ask", hint="maybe_answered_in_session", hint_at=now - 120000)
        dec("d-2-gate", gate="owner", stage="plan", task_id="t-1", question="Plan ok?", options=["approve", "request changes"])
        dec("d-3-plain")
        # settled from the session's report: a press stopped at Submit, then submitted in the pane;
        # and a pane question answered differently in the pane
        dec("d-4-deliv", source="pane", question="Which color for the deck?", tool_use_id="tu_x4", status="relayed",
            answer="Blue", answer_index=2, answered_at=now - 500000, relayed_at=now - 400000,
            delivered_via="session", reconciled_from="partial", session_id="s-1")
        dec("d-5-pane", source="pane", question="Which port for the tunnel?", tool_use_id="tu_x5", status="answered_in_pane",
            answer="8080", answered_at=now - 450000, resolved_via="pane", resolved_by="CP", session_id="s-1",
            reconcile_result="differs")
        # the session's report cannot tell what it got (a permission allowed, then the command failed)
        dec("d-7-unk", kind="permission", question="Allow Make?", tool_name="Bash", tool_use_id="tu_x7",
            status="answered_in_pane", answer="allow", answered_at=now - 350000, resolved_via="pane", resolved_by="CP",
            reconcile_result="unknown", reconciled_from="screen")
        dec("d-6-perm", kind="permission", question="Allow Bash?", tool_name="Bash", tool_use_id="tu_x6", status="relayed",
            answer="allow", answered_at=now - 300000, relayed_at=now - 300000, delivered_via="session", reconciled_from="screen")
        with open(os.path.join(home, "snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_at": now, "sessions": [], "decisions": [], "tasks": []}, fh)
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

            def new_page(width, init=None):
                ctx = browser.new_context(viewport={"width": width, "height": 900})
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto("http://127.0.0.1:%d/" % port)
                pg.wait_for_timeout(1000)
                return pg

            print("approver, phone width:")
            pg = new_page(390)
            c = pg.evaluate(CARD_JS, "d-1-ask")
            check("the hint shows on the board ask", c and c["maybe"] and c["maybe"].startswith("Maybe answered in the session: CP took a new prompt"), c)
            check("an approver gets 'Mark answered in session'", c and c["resolve"], c)
            check("no hint on an ask without one", pg.evaluate(CARD_JS, "d-3-plain")["maybe"] is None)
            order = pg.evaluate("() => [...document.querySelectorAll('#dec-list > article')].map((a) => [a.id, a.classList.contains('maybe')])")
            check("the hinted ask is listed last, de-emphasised; the others are not",
                  order and order[-1] == ["dec-d-1-ask", True] and not any(m for _, m in order[:-1]), order)
            check("  quieter (lower opacity)", float(pg.evaluate("() => getComputedStyle(document.getElementById('dec-d-1-ask')).opacity")) < 1)
            pg.evaluate("() => { const d = document.getElementById('dec-history'); d.hidden = false; d.open = true; }")
            HIST = """(q) => { const li = [...document.querySelectorAll('#dec-history-list li')].find((l) => l.textContent.includes(q));
              return li ? { chip: li.querySelector('.status').textContent, text: li.textContent, nd: li.classList.contains('undelivered') } : null; }"""
            h4 = pg.evaluate(HIST, "Which color for the deck?")
            check("history: delivered (finished in the session), never 'not delivered'",
                  h4 and h4["chip"] == "delivered" and "delivered (finished in the session)" in h4["text"] and not h4["nd"]
                  and "Not delivered" not in h4["text"], h4)
            h5 = pg.evaluate(HIST, "Which port for the tunnel?")
            check("history: answered in session, the board's answer was not used",
                  h5 and h5["chip"] == "answered in session" and "the board's was not used" in h5["text"] and not h5["nd"], h5)
            h7 = pg.evaluate(HIST, "Allow Make?")
            check("history: an unknown outcome is neutral (finished in the session), never 'different answer'",
                  h7 and h7["chip"] == "answered in session" and "finished in the session" in h7["text"]
                  and "different answer" not in h7["text"] and not h7["nd"], h7)
            h6 = pg.evaluate(HIST, "Allow Bash?")
            check("history: a permission allowed in the session = delivered", h6 and h6["chip"] == "delivered", h6)
            g = pg.evaluate(CARD_JS, "d-2-gate")
            check("never on a gate", g and not g["resolve"], g)
            pg.click('#dec-d-1-ask button:has-text("Mark answered in session")')
            pg.wait_for_timeout(200)
            c = pg.evaluate(CARD_JS, "d-1-ask")
            check("a confirm step first (nothing sent yet, the answer box hidden)",
                  c["confirm"] == "Close this question as answered in the session? Nothing is sent to CP." and not c["free"], c)
            with open(os.path.join(home, "decisions", "d-1-ask.json"), encoding="utf-8") as fh:
                check("  still pending on disk", json.load(fh)["status"] == "pending")
            pg.click('#dec-d-1-ask .confirm-row button:has-text("Cancel")')
            pg.wait_for_timeout(150)
            check("Cancel goes back", pg.evaluate(CARD_JS, "d-1-ask")["confirm"] is None)
            pg.click('#dec-d-1-ask button:has-text("Mark answered in session")')
            pg.wait_for_timeout(150)
            pg.click('#dec-d-1-ask .confirm-row button:has-text("Yes, close it")')
            pg.wait_for_function("() => !document.getElementById('dec-d-1-ask')", timeout=8000)
            with open(os.path.join(home, "decisions", "d-1-ask.json"), encoding="utf-8") as fh:
                d = json.load(fh)
            check("closed: answered_in_pane by the approver", (d["status"], d.get("resolved_by"), d.get("resolved_via")) == ("answered_in_pane", "local", "approver"), d)
            pg.evaluate("() => { const d = document.getElementById('dec-history'); d.hidden = false; d.open = true; }")
            chip = pg.evaluate("""() => { const li = [...document.querySelectorAll('#dec-history-list li')].find((l) => l.textContent.includes('Ship it behind a flag?'));
              return li ? li.querySelector('.status').textContent : null; }""")
            check("the history chip says answered in session", chip == "answered in session", chip)
            check("no sideways scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            pg.close()

            print("zh-TW:")
            pg = new_page(1280, "try{localStorage.setItem('switchboard.lang','zh-TW')}catch(e){}")
            c = pg.evaluate(CARD_JS, "d-3-plain")
            check("the action in zh-TW", c and c["resolve"] and "標記為已在 session 中回答" in c["text"], c)
            pg.evaluate("() => { const d = document.getElementById('dec-history'); d.hidden = false; d.open = true; }")
            zh = pg.evaluate("() => document.getElementById('dec-history-list').textContent")
            check("history wording in zh-TW", "已送達（在 session 中完成）" in zh and "看板的回答沒有用上" in zh
                  and "· 在 session 中完成" in zh, zh[:300])
            pg.close()

            print("not an approver:")
            write_cfg("boss@example.com")
            pg = new_page(1280)
            c = pg.evaluate(CARD_JS, "d-3-plain")
            check("no action for a non-approver", c is not None and not c["resolve"], c)
            st = pg.evaluate("""async () => { const r = await fetch('/api/decisions/d-3-plain/resolve', { method: 'POST',
              headers: { 'Content-Type': 'application/json', 'X-Board': '1' }, body: JSON.stringify({ confirm: 'resolve' }) });
              return [r.status, (await r.json()).error]; }""")
            check("and the route refuses them (403 not_approver)", st == [403, "not_approver"], st)
            pg.close()
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
