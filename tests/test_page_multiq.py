#!/usr/bin/env python3
"""tests/test_page_multiq.py — optional headless check of a form of several questions (an
AskUserQuestion picker with several single-select questions) on the board:
  - board-serve: one toggle group per question; "Send answers" only once every question has a
    pick; picks survive a re-render (a language switch); the POST body is {"options": [...]},
    nothing else; works from the keyboard;
  - free text per question: a text box under each question; typing clears its pick and a pick
    clears its text; the POST body is {"answers": [{question_index, option | text}]}, the text on
    one line (newlines and escapes become spaces);
  - the claude.ai Artifact page: read-only, with a note (no relay presses a form there).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_multiq.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a temporary BOARD_HOME; never touches
~/.agent-switchboard.
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


QS = [{"question": "Which color for the banner?", "header": "Color", "multiSelect": False,
       "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}]},
      {"question": "Which size for the banner?", "header": "Size", "multiSelect": False,
       "options": [{"label": "Small", "description": "compact"}, {"label": "Large", "description": "wide"}]}]
DID = "d-1794650400-mq11"
CARD_JS = """() => { const c = document.getElementById('dec-%s'); if (!c) return null;
  const send = [...c.querySelectorAll('button')].find((b) => b.dataset.focus === 'send-form');
  return { groups: c.querySelectorAll('.pq-opts[role="group"]').length,
    pressed: [...c.querySelectorAll('.pq-opt')].map((b) => b.getAttribute('aria-pressed')),
    send: send ? { disabled: send.disabled, text: send.textContent } : null,
    notes: [...c.querySelectorAll('.note')].map((n) => n.textContent) }; }""" % DID


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-multiq-test.")
    srv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []
            home, cfg = os.path.join(tmp, "home"), os.path.join(tmp, "config")
            os.makedirs(os.path.join(home, "decisions"))
            os.makedirs(cfg)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            dec = {"id": DID, "kind": "question", "source": "pane", "from": "CP", "question": QS[0]["question"],
                   "options": [], "questions": QS, "questions_clipped": False, "labels_clipped": False,
                   "pressable": True, "status": "pending", "answer": None, "tool_use_id": "tu_m",
                   "tool_name": "AskUserQuestion", "created_at": int(time.time() * 1000)}
            with open(os.path.join(home, "decisions", DID + ".json"), "w", encoding="utf-8") as fh:
                json.dump(dec, fh)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=os.path.join(tmp, "s"),
                       CLAUDE_JSON=os.path.join(tmp, "no-claude.json"))
            for k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
                env.pop(k, None)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def new_page(init=None):
                ctx = browser.new_context(viewport={"width": 390, "height": 900})
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("board-serve:")
            pg = new_page()
            posts = []

            def capture(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": DID, "status": "relayed"}))
            pg.route("**/api/decisions/*/answer", capture)
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_selector("#dec-%s .pq-opt" % DID, timeout=5000)
            c = pg.evaluate(CARD_JS)
            check("one toggle group per question, nothing picked", c["groups"] == 2 and c["pressed"] == ["false"] * 4, c)
            check("'Send answers' waits for a pick per question", c["send"] == {"disabled": True, "text": "Send answers"}, c)
            pg.click('#dec-%s .pq-opt[data-q="1"][data-option="2"]' % DID)
            c = pg.evaluate(CARD_JS)
            check("a pick marks only that option of question 1", c["pressed"] == ["false", "true", "false", "false"] and c["send"]["disabled"], c)
            pg.focus('#dec-%s .pq-opt[data-q="2"][data-option="1"]' % DID)
            pg.keyboard.press("Space")
            c = pg.evaluate(CARD_JS)
            check("keyboard: Space picks question 2, Send enabled", c["pressed"] == ["false", "true", "true", "false"] and not c["send"]["disabled"], c)
            pg.click("#acct-btn")
            pg.click('label:has(input[name="sb-lang"][value="zh-TW"])')
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(200)
            c = pg.evaluate(CARD_JS)
            check("picks survive a re-render (language switch), labels in 繁體中文",
                  c["pressed"] == ["false", "true", "true", "false"] and c["send"] == {"disabled": False, "text": "送出回答"}, c)
            pg.click('#dec-%s button[data-focus="send-form"]' % DID)
            pg.wait_for_timeout(400)
            check("the POST carries only the option numbers", posts == [{"options": [2, 1]}], posts)
            toasts = pg.evaluate("() => [...document.querySelectorAll('.toast')].map((t) => t.textContent)")
            check("the toast says the answers were submitted", any("已在 CP 選取並送出回答" in t for t in toasts), toasts)
            pg.close()

            print("board-serve, free text per question:")
            pg = new_page()
            posts.clear()
            pg.route("**/api/decisions/*/answer", capture)
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_selector("#dec-%s .pq-text" % DID, timeout=5000)
            sel = "#dec-%s" % DID
            n_text = pg.locator(sel + " .pq-text").count()
            check("every question has its own text box", n_text == 2, n_text)
            check("  with the single-question placeholder", pg.get_attribute(sel + ' .pq-text[data-q="2"]', "placeholder") == "Or type a different answer")
            pg.click(sel + ' .pq-opt[data-q="2"][data-option="2"]')
            pg.fill(sel + ' .pq-text[data-q="2"]', "teal green")
            pressed = pg.evaluate("(s) => [...document.querySelectorAll(s + ' .pq-opt')].map((b) => b.getAttribute('aria-pressed'))", sel)
            check("typing clears that question's option pick", pressed == ["false", "false", "false", "false"], pressed)
            send = pg.locator(sel + ' button[data-focus="send-form"]')
            check("  Send still waits for question 1", send.is_disabled())
            pg.fill(sel + ' .pq-text[data-q="1"]', "navy")
            pg.click(sel + ' .pq-opt[data-q="1"][data-option="2"]')
            check("picking an option clears that question's text", pg.input_value(sel + ' .pq-text[data-q="1"]') == "")
            check("  Send enabled: an option for 1, text for 2", not send.is_disabled())
            # A pasted newline and escape never reach the session: the text box itself drops line
            # breaks (an <input> value holds none), the page turns the escape into a space.
            pg.evaluate("""(s) => { const i = document.querySelector(s + ' .pq-text[data-q="2"]');
              i.value = 'teal' + String.fromCharCode(10) + 'green' + String.fromCharCode(27) + '[2J  '; i.dispatchEvent(new Event('input', { bubbles: true })); }""", sel)
            # Not delivered (409 partial): the typed text and the pick stay, to try again.
            pg.unroute("**/api/decisions/*/answer")

            def refuse(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=409, content_type="application/json",
                              body=json.dumps({"error": "partial", "detail": "keys were sent but Enter was not pressed"}))
            pg.route("**/api/decisions/*/answer", refuse)
            send.click()
            pg.wait_for_timeout(400)
            check("the POST carries {question_index, option | text}, text on one line",
                  posts == [{"answers": [{"question_index": 0, "option": 2}, {"question_index": 1, "text": "tealgreen [2J"}]}], posts)
            kept = pg.evaluate("""(s) => ({ text: document.querySelector(s + ' .pq-text[data-q="2"]').value,
              pick: document.querySelector(s + ' .pq-opt[data-q="1"][data-option="2"]').getAttribute('aria-pressed') })""", sel)
            toasts = pg.evaluate("() => [...document.querySelectorAll('.toast')].map((t) => t.textContent)")
            check("a form's partial press says the form was not submitted", any("form but it was not submitted" in t for t in toasts), toasts)
            check("not delivered: the typed text and the pick are kept",
                  kept["pick"] == "true" and kept["text"].startswith("tealgreen"), kept)
            pg.unroute("**/api/decisions/*/answer")
            pg.route("**/api/decisions/*/answer", capture)
            pg.locator(sel + ' button[data-focus="send-form"]').click()
            pg.wait_for_timeout(400)
            check("accepted on the retry: the same answers sent again", len(posts) == 2 and posts[1] == posts[0], posts)
            pg.close()

            print("claude adapter (Artifact):")
            stub = """(() => { const snap = (docs) => ({ docs, metadata: { fromCache: false } });
              const dec = %s;
              const query = (path) => { const q = { orderBy: () => q, limit: () => q, onSnapshot(cb) { setTimeout(() => cb(snap(path === 'decisions'
                ? [{ id: dec.id, exists: true, data: () => dec }] : [])), 0); return () => {}; } }; return q; };
              const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now(), approver_ids: ['u_x'] } : null;
                setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
              const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
                me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
              window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) }; })();""" % json.dumps(dec)
            pg = new_page(stub)
            pg.goto(PAGE)
            pg.wait_for_selector("#dec-%s" % DID, timeout=5000)
            c = pg.evaluate(CARD_JS)
            check("read-only on the Artifact page, with a note", c["send"] is None and not c["pressed"]
                  and any("self-hosted board" in n for n in c["notes"]), c)
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
