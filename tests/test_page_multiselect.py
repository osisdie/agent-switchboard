#!/usr/bin/env python3
"""tests/test_page_multiselect.py — optional headless check of multi-select pane questions
(AskUserQuestion with multiSelect: true) on the board:
  - board-serve, a lone multi-select question: one checkbox per option (label and description),
    nothing checked, "Submit" ready (no box checked is an answer, and the card says so); the
    checks survive a re-render (a language switch); the POST body is {"answers":
    [{question_index, labels}]}, labels in option order; works from the keyboard; no free-text box;
  - board-serve, a form mixing single- and multi-select questions: "Send answers" waits for the
    single-select pick; labels for the multi-select questions, option for the other;
  - phone width (390px) in both themes: no horizontal scroll, rows at least 44px tall;
  - the claude.ai Artifact page: read-only, "(multi-select)" note; a card that is not pressable
    (an older hook) is read-only too.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_multiselect.py [-v]

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


FRUITS = [{"question": "Which fruits should go into the basket today?", "header": "Fruits", "multiSelect": True,
           "options": [{"label": "Apple", "description": "Crisp and sweet"}, {"label": "Banana", "description": "Soft"},
                       {"label": "Cherry", "description": "Small and juicy"}]}]
PIZZA = [{"question": "Which toppings go on the pizza tonight?", "header": "Toppings", "multiSelect": True,
          "options": [{"label": "Cheese", "description": ""}, {"label": "Olives", "description": ""}]},
         {"question": "Which crust do you want?", "header": "Crust", "multiSelect": False,
          "options": [{"label": "Thin", "description": ""}, {"label": "Thick", "description": ""}]},
         {"question": "Which sides should we order with it?", "header": "Sides", "multiSelect": True,
          "options": [{"label": "Salad", "description": ""}, {"label": "Fries", "description": ""}]}]
D1, D2, D3 = "d-1794650400-ms01", "d-1794650400-ms02", "d-1794650400-ms03"
CARD_JS = """(id) => { const c = document.getElementById('dec-' + id); if (!c) return null;
  const send = [...c.querySelectorAll('button')].find((b) => b.dataset.focus === 'send-form');
  return { boxes: [...c.querySelectorAll('.pq-check input[type=checkbox]')].map((b) => b.checked),
    rows: [...c.querySelectorAll('.pq-check')].map((r) => r.textContent),
    heights: [...c.querySelectorAll('.pq-check')].map((r) => r.getBoundingClientRect().height),
    toggles: c.querySelectorAll('.pq-opt').length, texts: c.querySelectorAll('.pq-text').length,
    none: [...c.querySelectorAll('.pq-none')].map((n) => n.hidden ? '' : n.textContent),
    send: send ? { disabled: send.disabled, text: send.textContent } : null,
    notes: [...c.querySelectorAll('.note')].map((n) => n.textContent),
    wide: document.documentElement.scrollWidth > window.innerWidth }; }"""


def decision(did, qs, pressable=True, tu="tu"):
    return {"id": did, "kind": "question", "source": "pane", "from": "CP", "question": qs[0]["question"],
            "options": [o["label"] for o in qs[0]["options"]] if len(qs) == 1 else [], "questions": qs,
            "questions_clipped": False, "labels_clipped": False, "pressable": pressable, "status": "pending",
            "answer": None, "tool_use_id": tu, "tool_name": "AskUserQuestion", "created_at": int(time.time() * 1000)}


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-msel-page-test.")
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
            decs = [decision(D1, FRUITS, tu="tu1"), decision(D2, PIZZA, tu="tu2"), decision(D3, FRUITS, pressable=False, tu="tu3")]
            for dec in decs:
                with open(os.path.join(home, "decisions", dec["id"] + ".json"), "w", encoding="utf-8") as fh:
                    json.dump(dec, fh)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=os.path.join(tmp, "s"),
                       CLAUDE_JSON=os.path.join(tmp, "no-claude.json"), ALERT_DESKTOP="0")
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

            def new_page(init=None, scheme="light"):
                ctx = browser.new_context(viewport={"width": 390, "height": 900}, color_scheme=scheme)
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            posts = []

            def capture(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "x", "status": "relayed"}))

            print("board-serve, a lone multi-select question:")
            pg = new_page()
            pg.route("**/api/decisions/*/answer", capture)
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_selector("#dec-%s .pq-check" % D1, timeout=5000)
            c = pg.evaluate(CARD_JS, D1)
            check("one checkbox per option, label + description, none checked",
                  c["boxes"] == [False] * 3 and c["rows"] == ["1. AppleCrisp and sweet", "2. BananaSoft", "3. CherrySmall and juicy"], c)
            check("no option buttons and no free-text box (never typed into Type something)", c["toggles"] == 0 and c["texts"] == 0, c)
            check("'Submit' ready with nothing checked, and the card says it sends an empty answer",
                  c["send"] == {"disabled": False, "text": "Submit"} and c["none"] == ["No box checked: sent as an empty answer."], c)
            check("rows are at least 44px tall, no horizontal scroll at 390px", min(c["heights"]) >= 44 and not c["wide"], c)
            pg.click('#dec-%s .pq-check:has(input[data-option="3"])' % D1)
            pg.focus('#dec-%s input[data-option="1"]' % D1)
            pg.keyboard.press("Space")
            c = pg.evaluate(CARD_JS, D1)
            check("click and keyboard (Space) check boxes", c["boxes"] == [True, False, True] and c["none"] == [""], c)
            if os.environ.get("MSEL_SHOTS"):
                pg.locator("#dec-%s" % D1).screenshot(path=os.path.join(os.environ["MSEL_SHOTS"], "msel-light.png"))
            pg.click("#acct-btn")
            pg.click('label:has(input[name="sb-lang"][value="zh-TW"])')
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(200)
            c = pg.evaluate(CARD_JS, D1)
            check("checks survive a re-render (language switch), button in 繁體中文",
                  c["boxes"] == [True, False, True] and c["send"] == {"disabled": False, "text": "送出"}, c)
            pg.click('#dec-%s button[data-focus="send-form"]' % D1)
            pg.wait_for_timeout(400)
            check("the POST carries the labels, in option order", posts == [{"answers": [{"question_index": 0, "labels": ["Apple", "Cherry"]}]}], posts)
            c = pg.evaluate(CARD_JS, D3)
            check("a multi-select card that is not pressable is read-only (複選)",
                  c is not None and not c["boxes"] and c["send"] is None and any("複選" in n for n in c["notes"]), c)
            pg.close()

            print("board-serve, a form mixing single- and multi-select:")
            pg = new_page()
            posts.clear()
            pg.route("**/api/decisions/*/answer", capture)
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_selector("#dec-%s .pq-check" % D2, timeout=5000)
            c = pg.evaluate(CARD_JS, D2)
            check("checkboxes for the two multi-select questions only", len(c["boxes"]) == 4 and c["toggles"] == 2 and c["texts"] == 1, c)
            check("'Send answers' waits for the single-select pick", c["send"] == {"disabled": True, "text": "Send answers"}, c)
            pg.click('#dec-%s .pq-check:has(input[data-q="1"][data-option="2"])' % D2)
            pg.click('#dec-%s .pq-opt[data-q="2"][data-option="2"]' % D2)
            c = pg.evaluate(CARD_JS, D2)
            check("  enabled once it is picked (question 3 left empty)", c["send"]["disabled"] is False, c)
            pg.click('#dec-%s button[data-focus="send-form"]' % D2)
            pg.wait_for_timeout(400)
            check("the POST: labels, option, labels ([] for none)",
                  posts == [{"answers": [{"question_index": 0, "labels": ["Olives"]}, {"question_index": 1, "option": 2},
                                         {"question_index": 2, "labels": []}]}], posts)
            pg.close()

            print("dark theme:")
            pg = new_page(scheme="dark")
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_selector("#dec-%s .pq-check" % D1, timeout=5000)
            pg.click('#dec-%s .pq-check:has(input[data-option="2"])' % D1)
            st = pg.evaluate("""(id) => { const r = document.querySelector('#dec-' + id + ' .pq-check');
              const cs = getComputedStyle(r); const bodyBg = getComputedStyle(document.body).backgroundColor;
              return { bg: cs.backgroundColor, fg: cs.color, bodyBg, wide: document.documentElement.scrollWidth > window.innerWidth }; }""", D1)
            check("dark rows: dark background, light text, no horizontal scroll",
                  st["bg"] != "rgb(255, 255, 255)" and st["fg"] != st["bg"] and not st["wide"], st)
            if os.environ.get("MSEL_SHOTS"):
                pg.locator("#dec-%s" % D2).screenshot(path=os.path.join(os.environ["MSEL_SHOTS"], "msel-dark.png"))
            pg.close()

            print("claude adapter (Artifact):")
            stub = """(() => { const snap = (docs) => ({ docs, metadata: { fromCache: false } });
              const decs = %s;
              const query = (path) => { const q = { orderBy: () => q, limit: () => q, onSnapshot(cb) { setTimeout(() => cb(snap(path === 'decisions'
                ? decs.map((dec) => ({ id: dec.id, exists: true, data: () => dec })) : [])), 0); return () => {}; } }; return q; };
              const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now(), approver_ids: ['u_x'] } : null;
                setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
              const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
                me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
              window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) }; })();""" % json.dumps(decs[:2])
            pg = new_page(stub)
            pg.goto(PAGE)
            pg.wait_for_selector("#dec-%s" % D1, timeout=5000)
            for did in (D1, D2):
                c = pg.evaluate(CARD_JS, did)
                check("read-only on the Artifact page, with the multi-select note (%s)" % did,
                      c["send"] is None and not c["boxes"] and any("self-hosted board" in n and "multi-select" in n for n in c["notes"]), c)
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
