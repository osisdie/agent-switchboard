#!/usr/bin/env python3
"""tests/test_page_committee.py — optional headless check of committee decisions in artifact/board.html,
through a real board-serve: a board ask the committee decided while the owner was away shows in the
"Decided while you were away" list (red lines and production gates marked) and in the history as
"Decided by committee" with who agreed; open advice says it becomes the decision when everyone agrees;
the owner's profile control (committee / owner_final, red lines) saves through POST /api/away-decisions
and rewords the advice; "Mark reviewed" hides the list for this browser; the page can't answer a
committee decision (409); zh-TW, phone width, the dark theme.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and exits 0.
CI does not run it. PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell.
Uses a temporary BOARD_HOME; never touches ~/.agent-switchboard.

  python3 tests/test_page_committee.py [-v]
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
BOARD = os.path.join(REPO, "bin", "board")
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


def wait(fn, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


VIEW_JS = """() => {
  const box = document.getElementById('cmt-decided');
  const rows = [...document.querySelectorAll('#cmt-list li')].map((li) => ({ id: li.id, text: li.textContent,
    red: (li.querySelector('.cmt-red') || {}).textContent || null,
    why: (li.querySelector('.why') || {}).textContent || null, notes: (li.querySelector('.notes') || {}).textContent || null }));
  const hist = document.getElementById('dec-history-list');
  return { shown: !!box && !box.hidden, head: box && box.querySelector('h3').textContent, rows,
    hist: hist ? hist.textContent : '', pending: [...document.querySelectorAll('#dec-list .dcard')].map((c) => c.id) };
}"""

PROFILE_JS = """() => {
  const box = document.getElementById('acct-awaydec');
  const r = box.getBoundingClientRect();
  return { shown: !box.hidden, legend: box.querySelector('legend').textContent,
    checked: (box.querySelector('input[name=sb-awaydec]:checked') || {}).value || null,
    red: document.getElementById('ad-red-owner').checked, redShown: !document.getElementById('ad-red-row').hidden,
    note: document.getElementById('acct-ad-note').textContent, right: r.right, vw: innerWidth,
    scrollW: document.scrollingElement.scrollWidth };
}"""


def main():
    # under /tmp: the committee's test mode (BOARD_COMMITTEE_TEST) applies only to a board home there
    tmp = tempfile.mkdtemp(prefix="switchboard-committee-page.", dir="/tmp")
    srv = None
    try:
        home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
        for x in (home, cfg, sess):
            os.makedirs(x)
        port = free_port()
        with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
            fh.write("approvers: [local]\nserve:\n  enabled: true\n  port: %d\nsessions:\n"
                     "  - {code: API, cwd: /a}\n  - {code: WEB, cwd: /w}\n  - {code: QA, cwd: /q}\n  - {code: OPS, cwd: /o}\n" % port)
        env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
                   BOARD_COMMITTEE_TEST="1")
        for k in ("TMUX", "TMUX_PANE", "SLACK_WEBHOOK_URL", "BOARD_PUBLIC_URL"):
            env.pop(k, None)

        def board(*args):
            r = subprocess.run([BOARD, *args], env=env, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError("board %s: %s" % (" ".join(args[:2]), r.stderr.strip()))
            return r.stdout.strip()

        with open("/proc/self/stat", encoding="utf-8") as fh:
            st = fh.read()
        pst = st[st.rindex(")") + 2:].split()[19]
        reg = os.path.join(sess, "%d.json" % os.getpid())

        def as_session(code, *args):
            """board ARGS run inside session CODE: this process gets a session registry entry (as
            Claude Code writes one), so `board advise` verifies --from CODE."""
            with open(reg, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "sessionId": "s-" + code, "name": code, "procStart": pst}, fh)
            try:
                return board(*args)
            finally:
                os.unlink(reg)

        def setting():
            try:
                with open(os.path.join(home, "away-decisions.json"), encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return {}

        now = int(time.time() * 1000)
        with open(os.path.join(home, "snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_at": now, "sessions": [], "decisions": [], "tasks": []}, fh)
        board("away", "on", "--by", "OPS", "--said", "back tomorrow")
        board("away", "decisions", "committee", "--red-lines", "committee", "--by", "local")   # the owner opts in
        d1 = board("ask", "--from", "API", "Keep the old endpoint as a redirect?", "--option", "Keep redirect", "--option", "Remove it")
        as_session("WEB", "advise", d1, "--from", "WEB", "--to", "QA", "--option", "1", "Keep it <img src=x onerror=\"window.__pwned=1\">")
        as_session("QA", "advise", d1, "agree", "--from", "QA")
        as_session("API", "advise", d1, "agree", "--from", "API")
        tid = board("task", "new", "--owner", "OPS", "Rotate certificates", "--stage", "production")
        d2 = board("ask", "--from", "OPS", "--task", tid, "--gate", "production", "Release to production?")
        as_session("API", "advise", d2, "--from", "API", "--option", "approve", "The canary has been green for a day.")
        as_session("OPS", "advise", d2, "agree", "--from", "OPS", "--note", "Certificates expire Friday.")
        d3 = board("ask", "--from", "QA", "Which image tag?", "--option", "dated", "--option", "latest")
        as_session("WEB", "advise", d3, "--from", "WEB", "--option", "dated", "Use the dated tag.")
        for d in (d1, d2):
            with open(os.path.join(home, "decisions", d + ".json"), encoding="utf-8") as fh:
                rec = json.load(fh)
            check("setup: %s decided by the committee" % d, rec.get("answered_by") == "committee" and rec.get("status") == "answered", rec)

        srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        base = "http://127.0.0.1:%d/" % port

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []

            def new_page(width, init=None, dark=False):
                ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark" if dark else "light")
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto(base)
                pg.wait_for_selector("#dec-" + d3, timeout=8000)
                return pg

            print("decided while away:")
            pg = new_page(1280)
            pg.wait_for_function("() => !document.getElementById('cmt-decided').hidden", timeout=8000)
            v = pg.evaluate(VIEW_JS)
            check("the list is shown with both decisions, newest first",
                  v["shown"] and [r["id"] for r in v["rows"]] == ["cmt-" + d2, "cmt-" + d1], v["rows"])
            r2 = next((r for r in v["rows"] if r["id"] == "cmt-" + d2), {})
            r1 = next((r for r in v["rows"] if r["id"] == "cmt-" + d1), {})
            check("  the production gate is marked a red line", r2.get("red") == "red line · production gate", r2)
            check("  who agreed and that it waits to be relayed", "agreed: API, OPS" in r2.get("text", "")
                  and "waiting for the operator to relay it" in r2.get("text", ""), r2)
            check("  the red line's record: why and each session's reason", r2.get("why") == "why: The canary has been green for a day."
                  and r2.get("notes") == "their reasons: OPS: Certificates expire Friday.", r2)
            check("  the ordinary one has no red-line mark, its answer shown", r1.get("red") is None and "→ Keep redirect" in r1.get("text", ""), r1)
            check("decided asks are not pending cards", ("dec-" + d1) not in v["pending"] and ("dec-" + d2) not in v["pending"], v["pending"])
            check("the history says decided by committee", "Decided by committee" in v["hist"] and "agreed: WEB, QA, API" in v["hist"], v["hist"][:300])
            check("  with the red line's why and reasons", "why: The canary has been green for a day." in v["hist"]
                  and "their reasons: OPS: Certificates expire Friday." in v["hist"], v["hist"][:500])
            check("text is inserted as text", pg.evaluate("() => !window.__pwned && !document.querySelector('#cmt-list img, #dec-history-list img')"))
            tag = pg.evaluate("(id) => document.querySelector('#adv-' + id + ' .adv-tag').textContent", d3)
            check("open advice says it becomes the decision when everyone agrees", tag == "decides if every session asked agrees", tag)
            # the page can't answer a committee decision
            res = pg.evaluate("""(id) => fetch('/api/decisions/' + id + '/answer', { method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Board': '1' }, body: JSON.stringify({ answer: 'Remove it' }) })
                .then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) }))""", d1)
            check("answering it from the page: 409 committee_decided", res["status"] == 409 and (res["body"] or {}).get("error") in ("committee_decided", None)
                  and "committee" in json.dumps(res["body"]), res)

            print("profile control:")
            pg.click("#acct-btn")
            pg.wait_for_function("() => !document.getElementById('acct-awaydec').hidden", timeout=8000)
            pv = pg.evaluate(PROFILE_JS)
            check("the control is in the profile menu: committee, red lines included", pv["shown"] and pv["checked"] == "committee"
                  and not pv["red"] and pv["redShown"] and "red lines and production gates included" in pv["note"], pv)
            pg.click("#acct-awaydec label:has(input[value=owner_final])")
            check("owner_final is saved", wait(lambda: setting().get("mode") == "owner_final"), setting())
            pg.wait_for_function("(id) => document.querySelector('#adv-' + id + ' .adv-tag').textContent === 'advice only · not a decision'", arg=d3, timeout=8000)
            check("  the advice reads advice only again", True)
            if pg.evaluate("() => document.getElementById('acct-panel').hidden"):
                pg.click("#acct-btn")
            pv = pg.evaluate(PROFILE_JS)
            check("  the red-lines box hides with owner_final", pv["checked"] == "owner_final" and not pv["redShown"], pv)
            pg.click("#acct-awaydec label:has(input[value=committee])")
            check("committee is saved again", wait(lambda: setting().get("mode") == "committee"), setting())
            if pg.evaluate("() => document.getElementById('acct-panel').hidden"):
                pg.click("#acct-btn")
            pg.wait_for_function("() => !document.getElementById('ad-red-row').hidden", timeout=8000)
            pg.click("#ad-red-owner")
            check("red lines wait for the owner: saved", wait(lambda: setting().get("red_lines") == "owner"), setting())
            check("  by the owner on the board", (setting().get("by") or "").startswith("board:"), setting())
            pg.keyboard.press("Escape")

            print("mark reviewed:")
            pg.click("#cmt-reviewed")
            pg.wait_for_function("() => document.getElementById('cmt-decided').hidden", timeout=8000)
            check("the list hides once reviewed", True)
            pg.reload()
            pg.wait_for_selector("#dec-" + d3, timeout=8000)
            time.sleep(0.5)
            check("  and stays hidden after a reload (this browser)", pg.evaluate("() => document.getElementById('cmt-decided').hidden"))
            check("  the history still has them", "Decided by committee" in pg.evaluate("() => document.getElementById('dec-history-list').textContent"))
            check("no page errors", not errors, errors)

            print("zh-TW, phone width, dark:")
            pg = new_page(390, "try{localStorage.setItem('switchboard.lang','zh-TW')}catch(e){}", dark=True)
            pg.wait_for_function("() => !document.getElementById('cmt-decided').hidden", timeout=8000)
            v = pg.evaluate(VIEW_JS)
            check("zh-TW heading and marks", v["head"] == "你不在時做出的決定" and any(r["red"] == "紅線 · production gate" for r in v["rows"])
                  and any("同意：API, OPS" in r["text"] for r in v["rows"]), v)
            check("  the history tag in zh-TW", "由委員會決定" in v["hist"], v["hist"][:200])
            check("  why and reasons in zh-TW", any((r["why"] or "").startswith("理由：") and (r["notes"] or "").startswith("各 session 的理由：")
                                                  for r in v["rows"]), v["rows"])
            check("no sideways scroll at 390 px", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            box = pg.evaluate("() => { const s = getComputedStyle(document.getElementById('cmt-decided'));"
                              " return { bg: s.backgroundColor, ink: getComputedStyle(document.querySelector('#cmt-decided h3')).color }; }")
            check("the list renders in the dark theme", box["bg"] != box["ink"], box)
            pg.click("#acct-btn")
            pg.wait_for_function("() => !document.getElementById('acct-awaydec').hidden", timeout=8000)
            pv = pg.evaluate(PROFILE_JS)
            check("  the profile control in zh-TW fits", pv["legend"] == "你不在時的看板提問" and pv["red"] and pv["right"] <= pv["vw"]
                  and pv["scrollW"] <= pv["vw"], pv)
            check("no page errors", not errors, errors)
            browser.close()
        print("passed: %d  failed: %d" % (passed, failed))
        return 0 if failed == 0 else 1
    finally:
        if srv:
            srv.terminate()
            srv.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
