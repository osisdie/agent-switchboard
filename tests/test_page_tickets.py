#!/usr/bin/env python3
"""tests/test_page_tickets.py — optional headless check of the Tickets panel in artifact/board.html
at phone width (390 px), light and dark, English and 繁體中文:

  - demo: look up a sample ticket, the take confirm step with the Q4 text (the same words as the
    [BOARD ticket] relay, lib/tickets/writes.py AUTHORIZES), release, the drift badge and the
    "taken" chip on session cards, error states;
  - a real bin/board-serve with a `fake` tracker: an approver looks up, takes (a link file is
    written), comments and releases; a release manager reads with no buttons; a viewer has no panel
    at all (owner decision Q3); ambiguous refs offer the candidates; a server refusal is shown.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_tickets.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Never touches ~/.agent-switchboard.
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
PAGE = "file://" + os.path.join(REPO, "artifact", "board.html")
VERBOSE = "-v" in sys.argv[1:]

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skipped: python playwright is not installed")
    sys.exit(0)

from tickets.writes import AUTHORIZES  # noqa: E402

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
  const q = (s) => document.querySelector(s);
  const vis = (el) => !!el && !el.closest('[hidden]');
  const card = q('#tk-card');
  return {
    section: vis(q('#tickets')), nav: vis(q('#nav-tk')),
    note: vis(q('#tk-note')) ? q('#tk-note').textContent : null,
    form: vis(q('#tk-form')),
    title: card ? card.querySelector('.tktitle').textContent : null,
    status: card ? card.querySelector('.tkst').textContent : null,
    link: card ? card.querySelector('.tklink').textContent : null,
    drift: card ? !!card.querySelector('.tkdrift') : null,
    buttons: card ? [...card.querySelectorAll('button')].map((b) => b.textContent) : [],
    confirm: q('.tk-confirm') ? q('.tk-confirm').textContent : null,
    q4: q('.tk-q4') ? q('.tk-q4').textContent : null,
    err: q('.tk-err') ? q('.tk-err').dataset.code : null,
    errText: q('.tk-err') ? q('.tk-err').textContent : null,
    linked: [...document.querySelectorAll('#tk-linked li')].map((li) => li.textContent),
    chips: [...document.querySelectorAll('.scard .tkchip')].map((c) => c.textContent + (c.classList.contains('drift') ? '!' : '')),
    heading: q('#h-tickets').textContent,
    overflow: document.scrollingElement.scrollWidth > innerWidth + 1,
    toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
    bg: card ? getComputedStyle(card).backgroundColor : null,
  };
}"""


# A stand-in for the claude.ai viewer (as in test_page_profile.py): claude.use('user') / ('db')
# with an empty board and a meta/operator document. %s is a JSON object {me, operator}.
CLAUDE_STUB = """(() => {
  const cfg = %s;
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const query = () => { const q = { orderBy: () => q, limit: () => q,
    onSnapshot(cb) { setTimeout(() => cb(snap([])), 0); return () => {}; } }; return q; };
  const doc = (path) => ({
    onSnapshot(cb) { const d = path === 'meta/operator' ? Object.assign({ heartbeat_at: Date.now() }, cfg.operator) : null;
      setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; },
    set: async () => {}, update: async () => {} });
  const db = { collection: query, doc };
  const me = Object.assign({ id: 'u_test_me', name: '', avatarUrl: '', color: '#777', email: null, isOwner: false, canEdit: true }, cfg.me);
  const user = { id: async () => me.id, canEdit: async () => me.canEdit, isOwner: async () => me.isOwner,
    can: async () => null, me: async () => me };
  window.claude = { use: (name) => Promise.resolve(name === 'user' ? user : name === 'db' ? db : null) };
})();"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(tmp, approvers, rms, extra_tracker=""):
    """A board-serve on a fresh BOARD_HOME with a fake tracker. Returns (proc, base, home)."""
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nrelease_managers: %s\nserve:\n  enabled: true\n  port: %d\n"
                 "sessions:\n  - code: API\n    cwd: /w\n  - code: WEB\n    cwd: /w\n"
                 "tickets:\n  trackers:\n    - name: demo\n      type: fake\n"
                 "      take: {transition: in_progress, comment: \"Picked up as {task_id} ({session}).\"}\n%s"
                 % (json.dumps(approvers), json.dumps(rms), port, extra_tracker))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, CLAUDE_JSON=os.path.join(home, "no-claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS", "BOARD_MOCK")):
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
    return proc, "http://127.0.0.1:%d" % port, home, env


def lookup(page, ref):
    page.fill("#tk-ref", ref)
    page.click("#tk-form button[type=submit]")
    page.wait_for_timeout(500)


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-tickets.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            ctx = browser.new_context(viewport={"width": 390, "height": 800})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # Refusals (403/404/409) are expected; the browser logs those requests.
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 4" not in m.text else None)
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731

            print("demo (phone width):")
            page.goto(PAGE + "?demo=1")
            page.wait_for_timeout(700)
            v = view()
            check("demo: panel and nav shown", v["section"] and v["nav"], v)
            check("demo: note says sample tickets", v["note"] and "DEMO-1" in v["note"], v["note"])
            check("demo: linked takes listed", len(v["linked"]) == 2 and "DEMO-2" in v["linked"][0], v["linked"])
            check("demo: drift on the closed ticket's row", any("Closed on the tracker" in x for x in v["linked"]), v["linked"])
            check("demo: session chips (taken, closed upstream)", "DEMO-2 taken" in v["chips"] and "DEMO-3 closed upstream!" in v["chips"], v["chips"])
            lookup(page, "DEMO-1")
            v = view()
            check("demo: card", v["title"] == "Add a health endpoint to the API" and v["status"] == "todo" and v["link"] == "Not taken.", v)
            check("demo: approver buttons", v["buttons"] == ["Take…", "Comment…", "Move…", "Assign…"], v["buttons"])
            page.click("#tk-card button:has-text('Take…')")
            page.wait_for_timeout(150)
            check("take step 1: no Q4 yet, session select", view()["q4"] is None and page.locator("#tk-session").count() == 1)
            page.select_option("#tk-session", "WEB")
            page.click("#tk-card button:has-text('Next…')")
            page.wait_for_timeout(150)
            v = view()
            check("take confirm: names the session", v["confirm"] and "Take DEMO-1 for WEB?" in v["confirm"], v["confirm"])
            check("take confirm: Q4 text verbatim", v["q4"] and ("Authorizes WEB: " + AUTHORIZES + ".") in v["q4"], v["q4"])
            check("take confirm: merge/production note", "Merge and production still need" in (v["q4"] or ""), v["q4"])
            page.click("#tk-take-go")
            page.wait_for_timeout(400)
            v = view()
            check("taken: card shows the take", v["link"].startswith("Taken for WEB as task"), v["link"])
            check("taken: Release instead of Take", "Release take…" in v["buttons"] and "Take…" not in v["buttons"], v["buttons"])
            page.click("#tk-card button:has-text('Release take…')")
            page.wait_for_timeout(150)
            v = view()
            check("release confirm: Q4 text", v["q4"] and AUTHORIZES in v["q4"] and "Release DEMO-1 from WEB?" in v["confirm"], v)
            page.click("#tk-release-go")
            page.wait_for_timeout(400)
            check("released", view()["link"] == "Was taken for WEB (released).", view()["link"])
            lookup(page, "DEMO-404")
            check("error: not found", view()["err"] == "ticket_not_found" and "no such ticket" in view()["errText"], view())
            lookup(page, "not a ref!")
            check("error: bad ref", view()["err"] == "bad_ticket_ref", view())
            check("demo: no horizontal overflow at 390 px", not view()["overflow"])
            lookup(page, "DEMO-3")
            check("demo: drift badge on the card", view()["drift"], view())

            print("demo in 繁體中文, dark:")
            zh = ctx.new_page()
            zh.add_init_script("try { localStorage.setItem('switchboard.lang', 'zh-TW'); localStorage.setItem('switchboard.theme', 'dark'); } catch (e) {}")
            zh.on("pageerror", lambda e: errors.append(str(e)))
            zh.goto(PAGE + "?demo=1")
            zh.wait_for_timeout(600)
            zh.fill("#tk-ref", "DEMO-1")
            zh.click("#tk-form button[type=submit]")
            zh.wait_for_timeout(400)
            zh.click("#tk-card button:has-text('接手…')")
            zh.click("#tk-card button:has-text('下一步…')")
            zh.wait_for_timeout(200)
            vz = zh.evaluate(VIEW_JS)
            check("zh-TW: heading", vz["heading"] == "工單", vz["heading"])
            check("zh-TW: Q4 text translated", vz["q4"] and "不授權：合併" in vz["q4"] and "開出 PR/MR" in vz["q4"], vz["q4"])
            check("dark: the card is not white", vz["bg"] not in ("rgb(255, 255, 255)", None), vz["bg"])
            check("zh-TW dark: no overflow", not vz["overflow"])
            zh.close()

            light_bg = None
            lctx = browser.new_context(viewport={"width": 390, "height": 800}, color_scheme="light")   # own storage: no stored theme
            lp = lctx.new_page()
            lp.goto(PAGE + "?demo=1")
            lp.wait_for_timeout(500)
            lp.fill("#tk-ref", "DEMO-1")
            lp.click("#tk-form button[type=submit]")
            lp.wait_for_timeout(300)
            light_bg = lp.evaluate(VIEW_JS)["bg"]
            lctx.close()
            check("light and dark cards differ", light_bg and light_bg != vz["bg"], (light_bg, vz["bg"]))

            print("Artifact mode:")
            for who, ids, shown in (("approver", ["u_test_me"], True), ("viewer", ["u_other"], False)):
                ap = browser.new_context(viewport={"width": 390, "height": 760})
                ap.add_init_script(CLAUDE_STUB % json.dumps({"me": {}, "operator": {"approver_ids": ids, "release_manager_ids": []}}))
                ap.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ap.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ap.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto(PAGE)
                pg.wait_for_timeout(800)
                va = pg.evaluate(VIEW_JS)
                if shown:
                    check("Artifact approver: read-only note, no lookup", va["section"] and not va["form"]
                          and va["note"] and "self-hosted board" in va["note"], va)
                else:
                    check("Artifact viewer: no panel", not va["section"] and not va["nav"], va)
                ap.close()

            print("local board-serve: approver")
            srv, base, home, env = serve(os.path.join(tmp, "a"), ["local"], [])
            page.goto(base + "/")
            page.wait_for_timeout(1200)
            v = view()
            check("approver: panel shown, no read-only note", v["section"] and v["note"] is None and v["form"], v)
            lookup(page, "DEMO-2")
            v = view()
            check("approver: the fake ticket", v["title"] == "Fix the login redirect loop" and v["status"] == "in_progress", v)
            check("approver: buttons", v["buttons"] == ["Take…", "Comment…", "Move…", "Assign…"], v["buttons"])
            page.click("#tk-card button:has-text('Take…')")
            page.select_option("#tk-session", "API")
            page.click("#tk-card button:has-text('Next…')")
            page.wait_for_timeout(150)
            check("approver: confirm shows Q4", AUTHORIZES in (view()["q4"] or ""), view()["q4"])
            page.click("#tk-take-go")
            page.wait_for_timeout(1500)
            links = glob.glob(os.path.join(home, "tickets", "links", "*.json"))
            check("approver: the take wrote a link", len(links) == 1, links)
            v = view()
            check("approver: card shows taken for API", v["link"].startswith("Taken for API as task t-"), v["link"])
            check("approver: toast", any("operator relays it to API" in t for t in v["toasts"]), v["toasts"])
            check("approver: session card chip (via the task in the state)", "DEMO-2 taken" in v["chips"], v["chips"])
            page.click("#tk-card button:has-text('Comment…')")
            page.fill("#tk-text", "From the board")
            page.click("#tk-card button:has-text('Post comment')")
            page.wait_for_timeout(800)
            with open(os.path.join(home, "tickets", "fake-writes.jsonl"), encoding="utf-8") as fh:
                writes = [json.loads(x) for x in fh]
            check("approver: comment sent with the footer", writes[-1]["op"] == "comment"
                  and writes[-1]["value"] == "From the board\n\n(via agent-switchboard)", writes[-1])
            # Taken again elsewhere: the server's refusal shows (already_taken) in the toast.
            subprocess.run([os.path.join(REPO, "bin", "board"), "ticket", "release", "DEMO-2", "--by", "local"], env=env,
                           check=True, capture_output=True)
            subprocess.run([os.path.join(REPO, "bin", "board"), "ticket", "take", "DEMO-1", "--session", "WEB", "--by", "local",
                            "--no-comment", "--no-transition"], env=env, check=True, capture_output=True)
            lookup(page, "DEMO-1")
            check("approver: a take made elsewhere shows", view()["link"].startswith("Taken for WEB"), view()["link"])
            page.click("#tk-card button:has-text('Release take…')")
            page.wait_for_timeout(100)
            subprocess.run([os.path.join(REPO, "bin", "board"), "ticket", "release", "DEMO-1", "--by", "local"], env=env,
                           check=True, capture_output=True)
            page.click("#tk-release-go")
            page.wait_for_timeout(800)
            check("approver: the refusal is shown (not_taken)", any("Nobody has this ticket now." in t for t in view()["toasts"]), view()["toasts"])
            check("approver: no overflow", not view()["overflow"])
            srv.terminate(); srv.wait(10)

            print("local board-serve: ambiguous ref")
            srv, base, home, env = serve(os.path.join(tmp, "b"), ["local"], [], "    - name: other\n      type: fake\n")
            page.goto(base + "/")
            page.wait_for_timeout(1000)
            lookup(page, "DEMO-3")
            v = view()
            check("ambiguous: candidates offered", v["err"] == "ticket_ambiguous" and "demo:DEMO-3" in v["errText"] and "other:DEMO-3" in v["errText"], v)
            page.click(".tk-err button:has-text('other:DEMO-3')")
            page.wait_for_timeout(600)
            check("ambiguous: picking one looks it up", view()["title"] == "Retire the old export job", view())
            srv.terminate(); srv.wait(10)

            print("local board-serve: release manager (read-only)")
            srv, base, home, env = serve(os.path.join(tmp, "c"), ["owner@example.com"], ["local"])
            page.goto(base + "/")
            page.wait_for_timeout(1000)
            v = view()
            check("release manager: panel with a read-only note", v["section"] and v["note"] and "Read-only" in v["note"], v)
            lookup(page, "DEMO-1")
            v = view()
            check("release manager: reads the ticket", v["title"] == "Add a health endpoint to the API", v)
            check("release manager: no buttons", v["buttons"] == [], v["buttons"])
            srv.terminate(); srv.wait(10)

            print("local board-serve: viewer (no panel)")
            srv, base, home, env = serve(os.path.join(tmp, "d"), ["owner@example.com"], ["rm@example.com"])
            page.goto(base + "/")
            page.wait_for_timeout(1000)
            v = view()
            check("viewer: no panel, no nav entry", not v["section"] and not v["nav"], v)
            check("viewer: no ticket card or chips", v["title"] is None and v["chips"] == [], v)
            srv.terminate(); srv.wait(10)
            srv = None

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\npassed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
