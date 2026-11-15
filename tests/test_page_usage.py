#!/usr/bin/env python3
"""tests/test_page_usage.py — optional headless check of the usage popover in artifact/board.html:
mouse hover, keyboard focus and Escape, touch tap and tap outside, its contents (both windows,
reset times, updated, threshold, account), and the account line per viewer: the email for an
approver on board-serve, "hidden" for everyone else and on the Artifact page.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_usage.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://), a stubbed claude.ai viewer
and a real bin/board-serve on a temporary BOARD_HOME; never touches ~/.agent-switchboard or
~/.claude.json.
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


POP_JS = """() => { const p = document.getElementById('usage-pop'); const r = p.getBoundingClientRect();
  return { open: !p.hidden && r.width > 0, text: p.innerText, left: r.left, right: r.right, vw: innerWidth,
    role: p.getAttribute('role'), described: document.getElementById('usage').getAttribute('aria-describedby') }; }"""

CLAUDE_STUB = """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const query = () => { const q = { orderBy: () => q, limit: () => q, onSnapshot(cb) { setTimeout(() => cb(snap([])), 0); return () => {}; } }; return q; };
  const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: Date.now(), approver_ids: ['u_x'],
      usage: { five_hour: { pct: 42, resets_at: Date.now() + 3600000 }, seven_day: { pct: 12, resets_at: Date.now() + 86400000 }, updated_at: Date.now() } } : null;
    setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-usage-test.")
    srv = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []

            def new_page(init=None, **kw):
                ctx = browser.new_context(**dict({"viewport": {"width": 1280, "height": 860}}, **kw))
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("demo adapter, mouse and keyboard:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            s = pg.evaluate(POP_JS)
            check("closed at first; a tooltip the meter describes", not s["open"] and s["role"] == "tooltip" and s["described"] == "usage-pop", s)
            pg.hover("#usage")
            pg.wait_for_timeout(100)
            s = pg.evaluate(POP_JS)
            check("hover opens it", s["open"], s)
            t = s["text"]
            check("5-hour: used and reset time", "5-hour window: 91% used · resets ~" in t and "(in 1h 35m)" in t, t)
            check("weekly: used and reset time", "Weekly: 63% used · resets ~" in t, t)
            check("updated", "Updated: 1m ago" in t, t)
            check("threshold reached", "Threshold: 90% · prepare to switch" in t, t)
            check("account (demo)", "Account: you@example.com · Example Org" in t, t)
            lay = pg.evaluate("""() => { const p = document.getElementById('usage-pop'); const ps = [...p.querySelectorAll('p')].map((e) => e.getBoundingClientRect());
              return { flow: ps.every((r, i) => i === 0 || r.top >= ps[i - 1].bottom - 1), pos: getComputedStyle(p.querySelector('p:last-of-type')).position }; }""")
            check("its lines stack in order (no line taken out of the flow)", lay == {"flow": True, "pos": "static"}, lay)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(100)
            check("Escape closes the hover-opened popover, focus elsewhere", not pg.evaluate(POP_JS)["open"]
                  and pg.evaluate("() => document.activeElement !== document.getElementById('usage')"))
            pg.mouse.move(5, 5)
            pg.mouse.move(400, 200)
            pg.hover("#usage")
            pg.wait_for_timeout(100)
            pg.mouse.move(5, 5)
            pg.wait_for_timeout(100)
            check("leaving closes it", not pg.evaluate(POP_JS)["open"])
            pg.focus("#usage")
            pg.wait_for_timeout(100)
            check("keyboard focus opens it", pg.evaluate(POP_JS)["open"])
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(100)
            check("Escape closes it", not pg.evaluate(POP_JS)["open"])
            pg.close()

            print("touch, phone width:")
            pg = new_page(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            pg.tap("#usage")
            pg.wait_for_timeout(150)
            s = pg.evaluate(POP_JS)
            check("a tap opens it, inside the screen", s["open"] and s["left"] >= 0 and s["right"] <= s["vw"], s)
            pg.tap("h1")
            pg.wait_for_timeout(150)
            check("a tap outside closes it", not pg.evaluate(POP_JS)["open"])
            pg.tap("#usage")
            pg.wait_for_timeout(150)
            pg.tap("#usage")
            pg.wait_for_timeout(150)
            check("a second tap on the meter closes it", not pg.evaluate(POP_JS)["open"])
            pg.close()

            print("threshold legend: a calm scale, only the level reached in colour:")
            LEGEND_JS = """() => { const p = document.getElementById('usage-pop'), l = p.querySelector('.lvls');
              const bg = (e) => getComputedStyle(e).backgroundColor;
              const clear = (c) => c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
              const r = p.getBoundingClientRect();
              return { label: l.querySelector('.k').textContent, chips: [...l.querySelectorAll('.esc')].map((e) => ({ text: e.textContent, filled: !clear(bg(e)), on: e.classList.contains('on') })),
                text: p.innerText, right: r.right, vw: document.documentElement.clientWidth, sw: document.scrollingElement.scrollWidth }; }"""
            for scheme, vp in (("light", {"width": 1280, "height": 860}), ("dark", {"width": 390, "height": 844})):
                for pct, filled in ((39, []), (96, ["95% · switch accounts"])):
                    pg = new_page(CLAUDE_STUB.replace("pct: 42", "pct: %d" % pct), viewport=vp, color_scheme=scheme, has_touch=vp["width"] < 400)
                    pg.goto(PAGE)
                    pg.wait_for_timeout(600)
                    if vp["width"] < 400:
                        pg.tap("#usage")
                    else:
                        pg.hover("#usage")
                    pg.wait_for_timeout(100)
                    lg = pg.evaluate(LEGEND_JS)
                    tag = "%s, %dpx, %d%%" % (scheme, vp["width"], pct)
                    check("%s: the legend is labelled, calm wording" % tag, lg["label"] == "Alerts at: "
                          and [c["text"] for c in lg["chips"]] == ["80% · heads-up", "90% · plan a switch", "95% · switch accounts"], lg)
                    check("%s: filled chips %s" % (tag, filled or "none"), [c["text"] for c in lg["chips"] if c["filled"]] == filled, lg["chips"])
                    check("%s: the Threshold line is unchanged" % tag,
                          ("Threshold: 95% · switch now" if pct == 96 else "Threshold: below 80% · fine") in lg["text"], lg["text"])
                    check("%s: inside the screen" % tag, lg["right"] <= lg["vw"] and lg["sw"] <= lg["vw"], lg)
                    pg.close()
            pg = new_page(CLAUDE_STUB.replace("pct: 42", "pct: 39"), viewport={"width": 390, "height": 844}, has_touch=True)
            pg.goto(PAGE)
            pg.wait_for_timeout(600)
            pg.tap("#acct-btn")
            pg.tap('label:has(input[name="sb-lang"][value="zh-TW"])')
            pg.keyboard.press("Escape")
            pg.tap("#usage")
            pg.wait_for_timeout(100)
            lg = pg.evaluate(LEGEND_JS)
            check("zh-TW legend: 提醒門檻：80% · 提醒 / 90% · 規劃切換 / 95% · 切換帳號", lg["label"] == "提醒門檻："
                  and [c["text"] for c in lg["chips"]] == ["80% · 提醒", "90% · 規劃切換", "95% · 切換帳號"], lg)
            pg.close()

            print("claude adapter: no email on the Artifact page:")
            pg = new_page(CLAUDE_STUB)
            pg.goto(PAGE)
            pg.wait_for_timeout(600)
            pg.hover("#usage")
            pg.wait_for_timeout(100)
            t = pg.evaluate(POP_JS)["text"]
            check("usage from meta/operator, account hidden", "5-hour window: 42% used" in t and "Account: hidden" in t, t)
            pg.close()

            print("board-serve: the email for approvers only:")
            home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
            for x in (home, cfg, sess):
                os.makedirs(x)
            cj = os.path.join(tmp, "claude.json")
            with open(cj, "w", encoding="utf-8") as fh:
                json.dump({"oauthAccount": {"emailAddress": "owner@example.com", "organizationName": "Example Org"}, "primaryApiKey": "not-" + "real"}, fh)
            port = free_port()
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, CLAUDE_JSON=cj)
            env.pop("TMUX", None)

            def fleet(approvers):
                with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                    fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (approvers, port))

            fleet("[local]")
            subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            base = "http://127.0.0.1:%d/" % port
            pg = new_page()
            pg.goto(base)
            pg.wait_for_timeout(1000)
            pg.hover("#usage")
            pg.wait_for_timeout(100)
            t = pg.evaluate(POP_JS)["text"]
            check("an approver sees the account email", "Account: owner@example.com · Example Org" in t, t)
            pg.close()
            fleet("[boss@example.com]")  # local is no longer an approver
            pg = new_page()
            pg.goto(base)
            pg.wait_for_timeout(1000)
            pg.hover("#usage")
            pg.wait_for_timeout(100)
            t = pg.evaluate(POP_JS)["text"]
            check("anyone else sees account: hidden", "Account: hidden" in t, t)
            check("  and the email is nowhere in the page", "owner@example.com" not in pg.content())
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
