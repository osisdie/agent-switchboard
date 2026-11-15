#!/usr/bin/env python3
"""tests/test_page_lessons.py — optional headless check of the Lessons section in
artifact/board.html at phone width (390 px): the demo's read-only list (search, filter, the rule
in force, 繁體中文), and on a real bin/board-serve the approver's add / edit / retire / activate /
delete-with-confirm flow, the server's refusal shown as it is, and read-only for a non-approver.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_lessons.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Never touches ~/.agent-switchboard.
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


VIEW_JS = """() => {
  const cards = [...document.querySelectorAll('#ls-list .lcard')];
  return {
    titles: cards.map((c) => c.querySelector('.ltitle').textContent),
    status: cards.map((c) => c.querySelector('.lst').textContent),
    rules: cards.map((c) => c.querySelector('.lrule').textContent),
    buttons: cards.reduce((n, c) => n + c.querySelectorAll('button').length, 0),
    add: !document.getElementById('ls-add').hidden,
    readonly: document.getElementById('ls-readonly').hidden ? null : document.getElementById('ls-readonly').textContent,
    applied: document.getElementById('ls-applied').hidden ? null : document.getElementById('ls-applied').textContent,
    empty: document.getElementById('ls-empty').hidden ? null : document.getElementById('ls-empty').textContent,
    form: !!document.getElementById('ls-form'),
    err: document.getElementById('lf-err') ? document.getElementById('lf-err').textContent : null,
    confirm: document.querySelector('.lconfirm') ? document.querySelector('.lconfirm').textContent : null,
    heading: document.getElementById('h-lessons').textContent,
    badge: document.getElementById('nav-ls').textContent,
    overflow: document.scrollingElement.scrollWidth > innerWidth + 1,
    toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
  };
}"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def lines(home):
    try:
        with open(os.path.join(home, "lessons.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except OSError:
        return []


def serve(tmp, approvers, seed=()):
    """A board-serve on a fresh BOARD_HOME with these approvers. Returns (proc, base, home)."""
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (json.dumps(approvers), port))
    if seed:
        with open(os.path.join(home, "lessons.jsonl"), "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in seed)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, CLAUDE_JSON=os.path.join(home, "no-claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
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
    return proc, "http://127.0.0.1:%d" % port, home


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-lessons.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            ctx = browser.new_context(viewport={"width": 390, "height": 760})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # The refusal test makes the server answer 400 on purpose; the browser logs that request.
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 400" not in m.text else None)
            view = lambda: page.evaluate(VIEW_JS)  # noqa: E731

            print("demo (phone width, read-only):")
            page.goto(PAGE + "?demo=1")
            page.wait_for_timeout(500)
            v = view()
            check("demo: active and candidate lessons", len(v["titles"]) == 3 and v["status"].count("active") == 2, v)
            check("  read-only: no add, no buttons", not v["add"] and v["buttons"] == 0 and v["readonly"], v)
            check("  the rule in force is named", v["applied"] and "1 active lesson" in v["applied"], v)
            check("  rules read as sentences", any(r.startswith("When mem_available_pct < 30") for r in v["rules"]), v["rules"])
            check("  nav badge counts active lessons", v["badge"] == "2", v)
            check("  no horizontal scroll", not v["overflow"])
            page.fill("#ls-q", "suite")
            v = view()
            check("search", v["titles"] == ["Memory ran out with heavy jobs running"], v)
            page.fill("#ls-q", "nothing like it")
            check("search: nothing matches", 'Nothing matches "nothing like it".' in (view()["empty"] or ""), view())
            page.fill("#ls-q", "")
            page.select_option("#ls-filter", "retired")
            v = view()
            check("filter: retired", v["status"] == ["retired"], v)
            page.select_option("#ls-filter", "current")
            page.emulate_media(color_scheme="dark")
            dark = page.evaluate("() => getComputedStyle(document.querySelector('#ls-list .lcard')).backgroundColor")
            page.emulate_media(color_scheme="light")
            light = page.evaluate("() => getComputedStyle(document.querySelector('#ls-list .lcard')).backgroundColor")
            check("dark and light themes", dark == "rgb(22, 29, 37)" and light != dark, (dark, light))
            page.evaluate("() => localStorage.setItem('switchboard.lang', 'zh-TW')")
            page.reload()
            page.wait_for_timeout(500)
            v = view()
            check("繁體中文: heading and status", v["heading"] == "經驗" and "啟用中" in v["status"] and "候選" in v["status"], v)
            check("  data (titles) stays as it is", "Every session waited on the owner" in v["titles"], v)
            check("  the rule sentence is translated", any(r.startswith("當 mem_available_pct < 30") and "同時最多 1 個重型工作" in r for r in v["rules"]), v["rules"])
            check("  no horizontal scroll", not v["overflow"])
            page.evaluate("() => localStorage.setItem('switchboard.lang', 'en')")

            print("local (board-serve, approver):")
            srv, base, home = serve(os.path.join(tmp, "a"), ["local"])
            page.goto(base + "/")
            page.wait_for_selector("#ls-add:not([hidden])", timeout=5000)
            v = view()
            check("approver: add button, empty list", v["add"] and v["titles"] == [] and v["readonly"] is None, v)
            page.click("#ls-add")
            page.fill("#lf-title", "Hold heavy runs below 25% memory")
            page.fill("#lf-conds .lc-num", "25")
            page.select_option("#lf-action", "defer_heavy")
            page.fill("#lf-cause", "two suites at once")
            page.fill("#lf-reason", "two suites crashed the machine")
            check("form fits 390 px", not view()["overflow"])
            # The server's refusal is shown as it is: a request rewritten to a loosening action.
            def loosen(route):
                body = json.loads(route.request.post_data or "{}")
                body["rule"]["then"]["action"] = "allow_heavy"
                route.continue_(post_data=json.dumps(body))
            page.route("**/api/lessons", loosen)
            page.click("#lf-save")
            page.wait_for_function("() => document.getElementById('lf-err').textContent.length > 0", timeout=5000)
            v = view()
            check("refusal: the server's message, verbatim", "only tightening actions are allowed" in (v["err"] or "") and v["form"], v)
            check("  nothing written", lines(home) == [], lines(home))
            page.unroute("**/api/lessons", loosen)
            page.click("#lf-save")
            page.wait_for_function("() => document.querySelectorAll('#ls-list .lcard').length === 1", timeout=5000)
            v = view()
            check("added: card, form closed", v["titles"] == ["Hold heavy runs below 25% memory"] and not v["form"], v)
            rec = lines(home)[-1] if lines(home) else {}
            want_rule = {"when": {"all": [{"metric": "mem_available_pct", "lt": 25}]},
                         "then": {"action": "defer_heavy", "reason": "two suites crashed the machine"}}
            check("  stored with the rule and its reason", rec.get("rule") == want_rule and rec.get("updated_by") == "board:local", rec)
            page.click("#ls-list .lcard button:has-text('Edit')")
            check("  the edit form shows the reason", page.input_value("#lf-reason") == "two suites crashed the machine")
            page.fill("#lf-title", "Hold heavy runs below 25%")                # a title-only edit
            page.click("#lf-save")
            page.wait_for_function("() => !document.getElementById('ls-form')", timeout=5000)
            rec = lines(home)[-1] if lines(home) else {}
            check("edit of the title keeps the rule and its reason", rec.get("rule") == want_rule and rec.get("title") == "Hold heavy runs below 25%", rec)
            page.click("#ls-list .lcard button:has-text('Edit')")
            check("  the help says unticking removes the rule", "removes the rule" in page.inner_text("#ls-form .help"))
            page.fill("#lf-title", "Hold heavy runs below 25% available memory")
            page.click("#lf-with-rule")                                   # drop the rule: a note only
            page.click("#lf-save")
            page.wait_for_function("() => document.querySelector('#ls-list .ltitle').textContent.includes('available')", timeout=5000)
            v = view()
            check("edited: title and no rule", v["rules"] == ["No rule: a note only."], v)
            page.click("#ls-list .lcard button:has-text('Retire')")
            page.wait_for_function("() => !document.querySelector('#ls-list .lcard')", timeout=5000)
            check("retired: hidden from the current list", view()["titles"] == [], view())
            page.select_option("#ls-filter", "all")
            page.click("#ls-list .lcard button:has-text('Activate')")
            page.wait_for_function("() => document.querySelector('#ls-list .lst').textContent === 'active'", timeout=5000)
            check("activated", view()["status"][0] == "active", view())
            page.click("#ls-list .lcard button:has-text('Delete…')")
            v = view()
            check("delete asks to confirm first", v["confirm"] and "Delete this lesson?" in v["confirm"] and len(lines(home)) == 5, v)
            page.click("#ls-list .lcard button:has-text('Cancel')")
            check("  cancel keeps it", view()["confirm"] is None and len(view()["titles"]) == 1, view())
            page.click("#ls-list .lcard button:has-text('Delete…')")
            page.click("#ls-list .lcard .btn.deny.solid")
            page.wait_for_function("() => !document.querySelector('#ls-list .lcard')", timeout=5000)
            last = lines(home)[-1] if lines(home) else {}
            check("deleted: a tombstone by the approver", last.get("deleted") is True and last.get("by") == "board:local", last)
            check("  no horizontal scroll", not view()["overflow"])
            srv.terminate()
            srv.wait()

            print("local (board-serve, not an approver):")
            seed = [{"id": "l-5-0000abcd", "status": "active", "source": "owner", "title": "Seeded",
                     "rule": {"when": {"all": [{"metric": "heavy_running", "gte": 2}]}, "then": {"action": "pause_new_starts"}},
                     "occurrences": 0, "lost_min_total": 0, "last_seen": 1}]
            srv, base, home = serve(os.path.join(tmp, "b"), ["boss@example.com"], seed)
            page.goto("about:blank")
            page.goto(base + "/")
            page.wait_for_function("() => document.querySelectorAll('#ls-list .lcard').length === 1", timeout=5000)
            v = view()
            check("non-approver: reads, no add, no buttons", not v["add"] and v["buttons"] == 0 and v["titles"] == ["Seeded"], v)
            check("  read-only note", v["readonly"] == "Read-only: only approvers change lessons.", v)

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv and srv.poll() is None:
            srv.terminate()
            srv.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
