#!/usr/bin/env python3
"""tests/test_page_cards.py — optional headless check of the expandable session cards in
artifact/board.html: the collapsed summary, the detail panel, keyboard use, the open state kept
across live updates, phone width, the Artifact-mode note, and no machine paths in the DOM.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_cards.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://), a stubbed claude.ai viewer
and a real bin/board-serve on a temporary BOARD_HOME; never touches ~/.agent-switchboard.
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


CARD_JS = """(code) => {
  const card = document.querySelector('article.scard[aria-label="Session ' + code + '"]');
  if (!card) return null;
  const b = card.querySelector('.scard-toggle'), p = document.getElementById(b.getAttribute('aria-controls'));
  const txt = (sel) => { const e = card.querySelector(sel); return e ? e.textContent : null; };
  const r = card.getBoundingClientRect();
  return {
    tag: b.tagName, expanded: b.getAttribute('aria-expanded'), panelOpen: !!p && !p.hidden && p.getClientRects().length > 0, panelH: p ? p.getBoundingClientRect().height : -1,
    inCard: !!p && card.contains(p), chip: txt('.schip'), title: txt('.stitle'), where: txt('.swhere'), now: txt('.snow'),
    dt: [...card.querySelectorAll('.sdetail dt')].map((e) => e.textContent),
    pending: [...card.querySelectorAll('.sdetail dd a[href^="#dec-"]')].map((a) => a.getAttribute('href')),
    running: [...card.querySelectorAll('.sdetail dd li .tool')].map((e) => e.textContent),
    tmux: (() => { const dt = [...card.querySelectorAll('.sdetail dt')].find((e) => e.textContent === 'tmux'); return dt ? dt.nextElementSibling.textContent : null; })(),
    remote: !!card.querySelector('.sdetail a.linkout'),
    note: txt('.sdetail .note'), focused: document.activeElement === b,
    right: r.right, vw: innerWidth, scrollW: document.scrollingElement.scrollWidth,
  };
}"""

# claude.ai viewer stub: one session document as the operator mirrors it (no `recent`).
CLAUDE_STUB = """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const sess = { code: 'CP', aliases: ['CP'], state: 'working', status: 'busy', title: 'Ship the card details',
    repo: 'agent-board', branch: 'feat/cards', last_event: 'PreToolUse', notification_type: null, tmux: 'CP:@3.%7',
    remote_url: null, pending: null, updated_at: Date.now() - 60000 };
  const query = (path) => { const q = { orderBy: () => q, limit: () => q,
    onSnapshot(cb) { setTimeout(() => cb(snap(path === 'sessions' ? [{ id: 'CP', exists: true, data: () => sess }] : [])), 0); return () => {}; } }; return q; };
  const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now(), approver_ids: [] } : null;
    setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => false, isOwner: async () => false, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: false, canEdit: false }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-cards-test.")
    srv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []

            def new_page(init=None):
                ctx = browser.new_context(viewport={"width": 390, "height": 800})
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            def card(pg, code):
                return pg.evaluate(CARD_JS, code)

            print("demo adapter, phone width:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            c = card(pg, "API")
            check("collapsed: a real button, aria-expanded false, panel hidden inside the card",
                  c["tag"] == "BUTTON" and c["expanded"] == "false" and not c["panelOpen"] and c["panelH"] == 0 and c["inCard"], c)
            check("collapsed: chip, title, repo · branch, what it waits on",
                  (c["chip"], c["title"], c["where"]) == ("needs you", "Paginate the orders endpoint", "orders-api · feat/pagination")
                  and c["now"].startswith("Waiting: permission for Bash"), c)
            d = card(pg, "DOCS")
            check("collapsed: a working card says what is running", d["now"] == "Running Edit: …/configuration.md", d)
            check("collapsed: an idle card says since when", card(pg, "QA")["now"].startswith("Idle since "), card(pg, "QA"))
            check("collapsed: no title yet", card(pg, "DATA")["title"] == "No title yet")
            pg.click('article[aria-label="Session API"] .scard-toggle')
            pg.wait_for_timeout(150)
            c = card(pg, "API")
            check("click expands it", c["expanded"] == "true" and c["panelOpen"], c)
            check("detail: task, now, both pending decisions, running, tmux, session link",
                  c["dt"][:3] == ["Task", "Now", "Pending"] and c["pending"] == ["#dec-d-1-demo", "#dec-d-5-demo"] and c["running"] == ["Bash"]
                  and c["tmux"] == "API:@1.%3" and c["remote"], c)
            check("the toggle keeps focus after the re-render", c["focused"], c)
            check("expanded card fits the phone screen, no sideways scroll", c["right"] <= c["vw"] and c["scrollW"] <= c["vw"], c)
            pg.click('article[aria-label="Session API"] .sdetail a[href="#dec-d-1-demo"]')
            pg.wait_for_timeout(500)
            check("the pending link goes to the decision card", pg.evaluate("() => location.hash") == "#dec-d-1-demo")
            pg.focus('article[aria-label="Session DOCS"] .scard-toggle')
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(150)
            d = card(pg, "DOCS")
            check("Enter expands, focus stays on the toggle", d["expanded"] == "true" and d["panelOpen"] and d["focused"], d)
            check("  running tools listed", d["running"] == ["Read", "Edit"], d)
            pg.keyboard.press("Space")
            pg.wait_for_timeout(150)
            d = card(pg, "DOCS")
            check("Space collapses it", d["expanded"] == "false" and not d["panelOpen"] and d["focused"], d)
            check("other cards keep their state", card(pg, "API")["expanded"] == "true")
            for code in ("WEB", "DOCS", "OPS", "QA", "DATA", "MOBILE"):
                pg.click('article[aria-label="Session %s"] .scard-toggle' % code)
                pg.wait_for_timeout(60)
            html = pg.evaluate("() => document.getElementById('board').innerHTML")
            check("all expanded: no machine path in the cards", not any(x in html for x in ("/home/", "/Users/", "/tmp/")))
            check("all expanded: no sideways scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            pg.close()

            print("claude adapter (stubbed viewer):")
            pg = new_page(CLAUDE_STUB)
            pg.goto(PAGE)
            pg.wait_for_timeout(600)
            pg.click('article[aria-label="Session CP"] .scard-toggle')
            pg.wait_for_timeout(150)
            c = card(pg, "CP")
            check("mirrored title, repo · branch", (c["title"], c["where"]) == ("Ship the card details", "agent-board · feat/cards"), c)
            check("no tool summaries in Artifact mode: it says where to find them",
                  c["note"] == "Shown on the self-hosted board only." and c["now"] == "Running a tool", c)
            pg.close()

            print("local adapter (board-serve, live updates):")
            home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
            for x in (home, cfg, sess):
                os.makedirs(x)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            now = int(time.time() * 1000)

            def write_snap(title):
                row = {"code": "CP", "aliases": ["CP"], "name": "CP", "pid": 1, "session_id": "s1",
                       "cwd": "/secret/home/someone/agent-board", "tmux": "CP:@3.%7", "tmux_pane": "%7",
                       "status": "busy", "state": "working", "pending": None, "remote_url": None, "updated_at": now,
                       "title": title, "repo": "agent-board", "branch": "feat/cards", "last_event": "PreToolUse",
                       "notification_type": None, "recent": [{"tool": "Bash", "summary": "npm test -- …/card.spec.ts"}]}
                with open(os.path.join(home, "snapshot.json.tmp"), "w", encoding="utf-8") as fh:
                    json.dump({"generated_at": now, "sessions": [row], "decisions": [], "tasks": []}, fh)
                os.replace(os.path.join(home, "snapshot.json.tmp"), os.path.join(home, "snapshot.json"))

            write_snap("First title")
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
            pg = new_page()
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(1000)
            pg.focus('article[aria-label="Session CP"] .scard-toggle')
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(150)
            c = card(pg, "CP")
            check("local: expanded with the running tool", c["expanded"] == "true" and c["running"] == ["Bash"], c)
            write_snap("Second title")
            pg.wait_for_function("() => document.querySelector('article[aria-label=\"Session CP\"] .stitle').textContent === 'Second title'", timeout=8000)
            pg.wait_for_timeout(100)
            c = card(pg, "CP")
            check("a live update keeps the card open and the focus on its toggle", c["expanded"] == "true" and c["panelOpen"] and c["focused"], c)
            check("the cwd never reaches the page", "/secret" not in pg.content())
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
