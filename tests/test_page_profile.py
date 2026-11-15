#!/usr/bin/env python3
"""tests/test_page_profile.py — optional headless check of the board header's profile menu and
theme switcher in artifact/board.html: identity, roles, API keys, sign-out link, light / dark /
system, the pre-paint theme script, a host-stamped data-theme, blocked storage, keyboard and focus,
phone width.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_profile.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses the demo adapter (file://) and a real bin/board-serve
on a temporary BOARD_HOME; never touches ~/.agent-switchboard.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PAGE_FILE = os.path.join(REPO, "artifact", "board.html")
DEMO = "file://" + PAGE_FILE + "?demo=1"
VERBOSE = "-v" in sys.argv[1:]
LIGHT_BG, DARK_BG = "rgb(237, 240, 244)", "rgb(15, 20, 26)"

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


MENU_JS = """() => {
  const b = document.getElementById('acct-btn'), p = document.getElementById('acct-panel');
  const r = p.getBoundingClientRect(), a = document.activeElement;
  const vis = (id) => { const e = document.getElementById(id); return !!e && !e.hidden && e.getClientRects().length > 0; };
  return {
    expanded: b.getAttribute('aria-expanded'), controls: b.getAttribute('aria-controls'), tag: b.tagName,
    open: !p.hidden && r.width > 0, active: a ? a.id : null,
    left: r.left, right: r.right, vw: innerWidth, scrollW: document.scrollingElement.scrollWidth,
    name: document.getElementById('acct-name').textContent, via: document.getElementById('acct-via').textContent,
    avatar: document.getElementById('acct-avatar').textContent, label: b.getAttribute('aria-label'),
    roles: [...document.querySelectorAll('#acct-roles li')].map((li) => li.textContent),
    keysOff: vis('acct-keys-off'), keysOffText: document.getElementById('acct-keys-off').textContent,
    keys: [...document.querySelectorAll('#acct-keys li')].map((li) => li.className + ':' + li.querySelector('.k-name')?.textContent),
    signout: vis('acct-out'), signoutHref: document.getElementById('acct-out').getAttribute('href'),
    themeDisabled: document.getElementById('acct-theme').disabled, themeNote: vis('acct-theme-note'),
    checked: (document.querySelector('input[name="sb-theme"]:checked') || {}).value || null,
  };
}"""

THEME_JS = """() => ({
  attr: document.documentElement.getAttribute('data-theme'),
  bg: getComputedStyle(document.body).backgroundColor,
  scheme: getComputedStyle(document.documentElement).colorScheme,
  stored: (() => { try { return localStorage.getItem('switchboard.theme'); } catch (e) { return 'blocked'; } })(),
})"""

# Records, in order, the data-theme change on <html> and the first <style> / stylesheet <link> the
# parser adds: the theme must be set before any style exists (no flash of the wrong theme).
ORDER_INIT = """(() => {
  const seen = []; window.__order = seen;
  new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.type === 'attributes' && r.target === document.documentElement) seen.push('theme:' + document.documentElement.getAttribute('data-theme'));
      for (const n of r.addedNodes || []) if (n.nodeName === 'STYLE' || (n.nodeName === 'LINK' && n.rel === 'stylesheet')) seen.push('style');
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-theme'] });
})();"""

BLOCK_STORAGE = """Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });"""

# The host (claude.ai viewer) stamps data-theme on the root before the page's own scripts run.
HOST_LIGHT = """(() => {
  const stamp = () => { if (document.documentElement) { document.documentElement.setAttribute('data-theme', 'light'); return true; } return false; };
  if (!stamp()) new MutationObserver((_, o) => { if (stamp()) o.disconnect(); }).observe(document, { childList: true });
})();"""


# The host stamps data-theme="light" after the pre-paint script and before the app script runs:
# when the parser adds #toasts, the element right before the app's <script>.
HOST_LIGHT_LATE = """(() => {
  new MutationObserver((recs, o) => {
    for (const r of recs) for (const n of r.addedNodes) if (n.id === 'toasts') { document.documentElement.setAttribute('data-theme', 'light'); o.disconnect(); return; }
  }).observe(document, { subtree: true, childList: true });
})();"""

# A stand-in for the claude.ai viewer: claude.use('user') and claude.use('db') with an empty board
# and a meta/operator document. %s is a JSON object {me, operator}.
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
  const me = Object.assign({ id: 'u_test_me', name: '', avatarUrl: '', color: '#777', email: null, isOwner: false, canEdit: false }, cfg.me);
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


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-profile-test.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            errors = []

            def new_page(init=None, scheme="light", width=390):
                ctx = browser.new_context(viewport={"width": width, "height": 760}, color_scheme=scheme)
                for js in ([init] if isinstance(init, str) else init or []):
                    ctx.add_init_script(js)
                # No network in the test: the Google Fonts stylesheet answers empty.
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            def theme_btn(pg, value):
                pg.click('label:has(input[name="sb-theme"][value="%s"])' % value)
                pg.wait_for_timeout(100)

            print("source:")
            with open(PAGE_FILE, encoding="utf-8") as fh:
                src = fh.read()
            first_script, first_style = src.find("<script>"), min(src.find("<style>"), src.find('rel="stylesheet"'))
            check("pre-paint script comes before every stylesheet", 0 <= first_script < first_style, (first_script, first_style))
            css = re.sub(r"\s+", " ", src)
            check("dark tokens under media + :not([data-theme=light])",
                  '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;' in css)
            check("dark tokens under :root[data-theme=dark]", ':root[data-theme="dark"] { color-scheme: dark;' in css)
            check(":root sets color-scheme light", ":root { color-scheme: light;" in css)

            print("demo adapter, phone width:")
            pg = new_page(ORDER_INIT)
            pg.goto(DEMO)
            pg.wait_for_timeout(500)
            s = pg.evaluate(MENU_JS)
            check("a real button, closed, aria-controls the panel",
                  s["tag"] == "BUTTON" and s["expanded"] == "false" and s["controls"] == "acct-panel" and not s["open"], s)
            check("avatar initials and an accessible name", s["avatar"] == "DV" and "Demo viewer" in (s["label"] or ""), s)
            pg.click("#acct-btn")
            pg.wait_for_timeout(150)
            s = pg.evaluate(MENU_JS)
            check("click opens it: aria-expanded true, focus moves into the panel", s["open"] and s["expanded"] == "true" and s["active"] == "acct-panel", s)
            check("demo identity and roles", s["name"] == "Demo viewer" and s["via"].startswith("Demo data") and "Approver" in s["roles"], s)
            check("demo API keys listed (active and revoked)", s["keys"] == ["active:Phone shortcut", "revoked:Old laptop"] and not s["keysOff"], s)
            check("no sign-out link without Cloudflare Access", not s["signout"], s)
            check("panel fits the phone screen, no sideways scroll", s["left"] >= 0 and s["right"] <= s["vw"] and s["scrollW"] <= s["vw"], s)
            check("theme defaults to system", s["checked"] == "system" and not s["themeDisabled"], s)
            pg.keyboard.press("Tab")
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(100)
            s = pg.evaluate(MENU_JS)
            check("Escape closes it and focus returns to the button", not s["open"] and s["expanded"] == "false" and s["active"] == "acct-btn", s)
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(100)
            check("Enter on the button opens it again", pg.evaluate(MENU_JS)["open"])
            ring = pg.evaluate("() => { const p = document.getElementById('acct-panel'); return { fv: p.matches(':focus-visible'), outline: getComputedStyle(p).outlineStyle }; }")
            check("opened from the keyboard: the panel shows a focus ring", ring == {"fv": True, "outline": "solid"}, ring)
            pg.keyboard.press("Escape")
            pg.mouse.click(10, 700)  # a mouse user: focus on the page, not on the button
            pg.click("#acct-btn")
            pg.wait_for_timeout(100)
            ring = pg.evaluate("() => { const p = document.getElementById('acct-panel'); return { fv: p.matches(':focus-visible'), outline: getComputedStyle(p).outlineStyle }; }")
            check("opened with the mouse: no focus ring", ring["outline"] == "none", ring)
            pg.mouse.click(10, 700)
            pg.wait_for_timeout(100)
            check("a click outside closes it", not pg.evaluate(MENU_JS)["open"])

            print("theme:")
            pg.click("#acct-btn")
            theme_btn(pg, "dark")
            t = pg.evaluate(THEME_JS)
            check("dark: data-theme, palette, color-scheme, stored", t == {"attr": "dark", "bg": DARK_BG, "scheme": "dark", "stored": "dark"}, t)
            check("the menu stays open while choosing", pg.evaluate(MENU_JS)["open"])
            pg.reload()  # the stored choice applies on the next load
            pg.wait_for_timeout(300)
            order = pg.evaluate("() => window.__order")
            check("reload: dark applied before the first stylesheet (no flash)", order[:1] == ["theme:dark"] and "style" in order, order)
            check("reload: menu shows dark", pg.evaluate(MENU_JS)["checked"] == "dark")
            pg.click("#acct-btn")
            theme_btn(pg, "light")
            t = pg.evaluate(THEME_JS)
            check("light: data-theme, palette, stored", t == {"attr": "light", "bg": LIGHT_BG, "scheme": "light", "stored": "light"}, t)
            theme_btn(pg, "system")
            t = pg.evaluate(THEME_JS)
            check("system: attribute and stored choice removed", t["attr"] is None and t["stored"] is None and t["bg"] == LIGHT_BG, t)
            pg.close()

            pg = new_page(scheme="dark")
            pg.goto(DEMO)
            pg.wait_for_timeout(300)
            t = pg.evaluate(THEME_JS)
            check("system on a dark OS: dark palette", t["attr"] is None and t["bg"] == DARK_BG and t["scheme"] == "dark", t)
            pg.click("#acct-btn")
            theme_btn(pg, "light")
            t = pg.evaluate(THEME_JS)
            check("explicit light wins over a dark OS", t["bg"] == LIGHT_BG and t["scheme"] == "light", t)
            theme_btn(pg, "system")
            pg.close()

            pg = new_page(BLOCK_STORAGE)
            pg.goto(DEMO)
            pg.wait_for_timeout(300)
            pg.click("#acct-btn")
            theme_btn(pg, "dark")
            t = pg.evaluate(THEME_JS)
            check("storage blocked: the switcher still works", t["attr"] == "dark" and t["bg"] == DARK_BG and t["stored"] == "blocked", t)
            pg.close()

            print("host-stamped theme (claude.ai viewer):")
            pg = new_page([HOST_LIGHT], scheme="dark")
            pg.goto(DEMO)
            pg.wait_for_timeout(300)
            pg.evaluate("() => localStorage.setItem('switchboard.theme', 'dark')")
            pg.reload()
            pg.wait_for_timeout(300)
            pg.click("#acct-btn")
            pg.wait_for_timeout(100)
            s, t = pg.evaluate(MENU_JS), pg.evaluate(THEME_JS)
            check("host theme kept over the stored choice", t["attr"] == "light" and t["bg"] == LIGHT_BG, t)
            check("switcher disabled, shows the host's theme, says why", s["themeDisabled"] and s["checked"] == "light" and s["themeNote"], s)
            pg.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
            pg.wait_for_timeout(100)
            s = pg.evaluate(MENU_JS)
            check("the host switches to dark: the menu follows at once", s["themeDisabled"] and s["checked"] == "dark", s)
            pg.evaluate("() => document.documentElement.removeAttribute('data-theme')")
            pg.wait_for_timeout(100)
            s, t = pg.evaluate(MENU_JS), pg.evaluate(THEME_JS)
            check("the host removes its stamp: the stored choice applies again", t["attr"] == "dark" and t["bg"] == DARK_BG, t)
            check("  and the switcher is back", not s["themeDisabled"] and s["checked"] == "dark" and not s["themeNote"], s)
            theme_btn(pg, "light")
            t = pg.evaluate(THEME_JS)
            check("  and works again", t["attr"] == "light" and t["stored"] == "light", t)
            pg.evaluate("() => localStorage.removeItem('switchboard.theme')")
            pg.close()

            pg = new_page([HOST_LIGHT_LATE], scheme="dark")
            pg.goto(DEMO)
            pg.wait_for_timeout(300)
            pg.evaluate("() => localStorage.setItem('switchboard.theme', 'dark')")
            pg.reload()
            pg.wait_for_timeout(300)
            pg.click("#acct-btn")
            pg.wait_for_timeout(100)
            s, t = pg.evaluate(MENU_JS), pg.evaluate(THEME_JS)
            check("a stamp between the pre-paint and app scripts is kept", t["attr"] == "light" and t["bg"] == LIGHT_BG, t)
            check("  and the switcher follows it", s["themeDisabled"] and s["checked"] == "light" and s["themeNote"], s)
            pg.evaluate("() => localStorage.removeItem('switchboard.theme')")
            pg.close()

            pg = new_page()
            pg.goto(DEMO)
            pg.wait_for_timeout(300)
            pg.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
            pg.wait_for_timeout(100)
            pg.click("#acct-btn")
            pg.wait_for_timeout(100)
            s = pg.evaluate(MENU_JS)
            check("a host stamp after load: the switcher follows it", s["themeDisabled"] and s["checked"] == "dark" and s["themeNote"], s)
            pg.close()

            print("claude adapter (stubbed viewer):")
            claude_page = "file://" + PAGE_FILE
            for label, cfg, want in (
                ("approver, release_manager_ids empty: both roles (fallback)",
                 {"me": {}, "operator": {"approver_ids": ["u_test_me"], "release_manager_ids": []}},
                 {"name": "You", "avatar": "Y", "via": "Signed in to claude.ai · cannot edit this board", "roles": ["Approver", "Release manager"]}),
                ("named owner who edits, release manager only",
                 {"me": {"name": "Jane Doe", "isOwner": True, "canEdit": True},
                  "operator": {"approver_ids": ["u_other"], "release_manager_ids": ["u_test_me"]}},
                 {"name": "Jane Doe", "avatar": "JD", "via": "Signed in to claude.ai · owner · can edit this board", "roles": ["Release manager"]}),
                ("in neither list: viewer",
                 {"me": {}, "operator": {"approver_ids": ["u_other"], "release_manager_ids": []}},
                 {"name": "You", "avatar": "Y", "via": "Signed in to claude.ai · cannot edit this board", "roles": ["Viewer"]})):
                pg = new_page(CLAUDE_STUB % json.dumps(cfg))
                pg.goto(claude_page)
                pg.wait_for_timeout(500)
                pg.click("#acct-btn")
                pg.wait_for_timeout(200)
                s = pg.evaluate(MENU_JS)
                got = {k: s[k] for k in want}
                check("claude: " + label, got == want, got)
                check("  API keys not enabled, no sign-out", s["keysOff"] and s["keys"] == [] and not s["signout"], s)
                pg.close()

            print("local adapter (board-serve):")
            home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
            for d in (os.path.join(home, "decisions"), cfg, sess):
                os.makedirs(d)
            port = free_port()
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [local]\nrelease_managers: [rm@example.com]\nserve:\n  enabled: true\n  port: %d\n" % port)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess)
            env.pop("BOARD_PUBLIC_URL", None)
            env.pop("TMUX", None)
            subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = "http://127.0.0.1:%d" % port
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            pg = new_page()
            pg.goto(base + "/")
            pg.wait_for_timeout(1200)
            pg.click("#acct-btn")
            pg.wait_for_timeout(400)
            s = pg.evaluate(MENU_JS)
            check("local: identity from /api/me", s["name"] == "local" and s["via"].startswith("Local listener") and s["avatar"] == "L", s)
            check("local: roles", s["roles"] == ["Approver"], s)
            check("local: API keys not enabled, no list", s["keysOff"] and s["keysOffText"] == "API keys are not enabled on this board." and s["keys"] == [], s)
            check("local: no sign-out link", not s["signout"], s)
            pg.click("#acct-btn")
            theme_btn_closed = pg.evaluate(MENU_JS)
            check("local: the button closes it", not theme_btn_closed["open"], theme_btn_closed)
            pg.close()

            # Through the tunnel, /api/me says "access" (and lists keys once they exist).
            me = {"identity": "jane.doe@example.com", "via": "access", "roles": ["approver", "release_manager"],
                  "is_approver": True, "is_release_manager": True,
                  "api_keys": [{"id": "key_1", "name": "CI", "scopes": ["state:read"], "created_at": 1767225600000,
                                "expires_at": None, "last_used_at": None, "status": "active"}]}
            pg = new_page()
            pg.route("**/api/me", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(me)))
            pg.goto(base + "/")
            pg.wait_for_timeout(1200)
            pg.click("#acct-btn")
            pg.wait_for_timeout(400)
            s = pg.evaluate(MENU_JS)
            check("access: identity, via, initials", s["name"] == "jane.doe@example.com" and s["via"] == "Verified by Cloudflare Access" and s["avatar"] == "JD", s)
            check("access: both roles", s["roles"] == ["Approver", "Release manager"], s)
            check("access: keys rendered from api_keys", s["keys"] == ["active:CI"] and not s["keysOff"], s)
            check("access: sign-out link to the Access logout", s["signout"] and s["signoutHref"] == "/cdn-cgi/access/logout", s)
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
