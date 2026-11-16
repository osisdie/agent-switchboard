#!/usr/bin/env python3
"""tests/test_page_dev.py — optional headless check of the developer portal (artifact/dev.html on
board-serve: /dev and /dev/keys): the API reference from the live OpenAPI document, the MCP tool
table, the snippets (no Cloudflare headers on the local listener), the table of contents, light and
dark themes, phone width without sideways scrolling, the board's profile-menu link to /dev, and the
key page: create (the key in a one-time dialog, gone when it closes, never in browser storage),
revoke with a confirm step, and a non-owner's request (pending, "Withdraw").

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and exits 0,
so it never blocks anyone. CI does not run it.

  python3 tests/test_page_dev.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a real bin/board-serve on a temporary BOARD_HOME; never
touches ~/.agent-switchboard.
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
sys.path.insert(0, os.path.join(REPO, "lib"))
VERBOSE = "-v" in sys.argv[1:]
KEY_RE = re.compile(r"asb_live_[0-9a-z]{12}_[A-Za-z0-9_-]{43}")

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
    p = s.getsockname()[1]
    s.close()
    return p


def serve(tmp, roles):
    """board-serve on a fresh home; `local` gets the given fleet.yaml role lists."""
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    for d in (cfg, sess):
        os.makedirs(d)
    os.makedirs(home, mode=0o700)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("".join("%s: [local]\n" % r for r in roles) + "sessions:\n  - code: API\n    cwd: /w\n"
                 "serve:\n  enabled: true\n  port: %d\n" % port)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_", "CF_ACCESS")) and k not in ("TMUX", "TMUX_PANE")}
    env.update(BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(home, "no-claude.json"))
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d" % port, home


DEV_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  return {
    ops: [...document.querySelectorAll('#ref details.op')].map((d) => d.dataset.op),
    groups: [...document.querySelectorAll('#ref .scope-group h3 .chip')].map((c) => c.textContent),
    tools: [...document.querySelectorAll('#mcp-tools tbody tr')].map((r) => r.dataset.tool),
    snips: [...document.querySelectorAll('#snips .snip, #health-snip .snip')].map((s) => s.id),
    snipText: [...document.querySelectorAll('pre code')].map((c) => c.textContent).join('\\n'),
    errors: [...document.querySelectorAll('#err-table tbody tr')].map((r) => r.dataset.code),
    toc: [...document.querySelectorAll('#toc-list a')].map((a) => a.getAttribute('href')),
    tocTargets: [...document.querySelectorAll('#toc-list a')].every((a) => !!document.getElementById(a.getAttribute('href').slice(1))),
    tocOn: [...document.querySelectorAll('#toc-list a.on')].length,
    who: document.getElementById('who').textContent,
    current: document.querySelector('nav a[aria-current="page"]').getAttribute('href'),
    tryNote: vis(document.querySelector('#try .note')),
    loadError: vis(document.getElementById('load-error')),
    scrollW: document.scrollingElement.scrollWidth, vw: innerWidth,
    bg: getComputedStyle(document.body).backgroundColor,
  };
}"""

KEYS_JS = """() => ({
  rows: [...document.querySelectorAll('#keys-table tbody tr[data-kid]')].map((r) => ({ id: r.dataset.kid,
    text: r.textContent, acts: [...r.querySelectorAll('button')].map((b) => b.textContent) })),
  submit: document.getElementById('k-submit').textContent,
  ownerField: !document.getElementById('k-owner-l').hidden,
  ownerCol: [...document.querySelectorAll('.col-owner')].some((c) => !c.hidden),
  modal: !document.getElementById('once').hidden,
  modalKey: document.getElementById('once-key').textContent,
  modalWhat: document.getElementById('once-what').textContent,
  status: document.getElementById('keys-status').textContent,
  storage: (() => { try { return JSON.stringify(localStorage) + JSON.stringify(sessionStorage); } catch (e) { return ''; } })(),
  scrollW: document.scrollingElement.scrollWidth, vw: innerWidth,
})"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-dev.")
    srv = None
    errors = []
    try:
        with sync_playwright() as p:
            try:
                exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0

            def new_page(width=1280, scheme="light"):
                pg = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                return pg

            print("owner on the local listener:")
            srv, base, home = serve(os.path.join(tmp, "owner"), ("owners", "approvers"))
            import api_spec
            import mcp_tools
            spec_ops = sorted("%s %s" % (m.upper(), path) for path, ops in api_spec.openapi()["paths"].items() for m in ops)

            pg = new_page()
            pg.goto(base + "/dev")
            pg.wait_for_selector("#ref details.op", timeout=10000)
            d = pg.evaluate(DEV_JS)
            check("every route of the live spec is rendered", sorted(d["ops"]) == spec_ops, (d["ops"], spec_ops))
            check("routes grouped by scope, public first", d["groups"][:2] == ["public", "any"] and "scope read:state" in d["groups"], d["groups"])
            check("the MCP tool table", d["tools"] == [t["name"] for t in mcp_tools.TOOLS], d["tools"])
            check("snippets: Claude Code HTTP + stdio, Claude Desktop, curl, health check",
                  d["snips"] == ["snip-claude-code-http", "snip-claude-code-stdio", "snip-claude-desktop", "snip-curl", "snip-health"], d["snips"])
            check("snippets carry this board's URL", base + "/mcp" in d["snipText"], d["snipText"][:300])
            check("no Cloudflare headers on the local listener", "CF-Access" not in d["snipText"] and "CF_ACCESS" not in d["snipText"])
            check("no key in any snippet", not KEY_RE.search(d["snipText"]))
            check("troubleshooting codes", {"unauthenticated", "token_key_mismatch", "identity_key_mismatch", "insufficient_scope", "rate_limited"} <= set(d["errors"]), d["errors"])
            check("the try-it console is marked not available yet", d["tryNote"])
            check("table of contents: every link has its section, one is current", d["tocTargets"] and len(d["toc"]) >= 6 and d["tocOn"] == 1, d)
            check("nav marks /dev", d["current"] == "/dev", d["current"])
            check("signed in as local", "local" in d["who"], d["who"])
            check("light theme", d["bg"] == "rgb(237, 240, 244)", d["bg"])
            check("no sideways scroll on a wide screen", d["scrollW"] <= d["vw"], d)
            pg.click("#toc-list a[href='#errors']")
            pg.wait_for_timeout(500)
            on = pg.evaluate("() => document.querySelector('#toc-list a.on')?.getAttribute('href')")
            check("scrolling marks the current section", on == "#errors", on)
            pg.close()

            pg = new_page(width=390, scheme="dark")
            pg.goto(base + "/dev")
            pg.wait_for_selector("#ref details.op", timeout=10000)
            pg.evaluate("() => document.querySelectorAll('#ref details.op').forEach((d) => { d.open = true; })")
            d = pg.evaluate(DEV_JS)
            check("phone width: no sideways scroll (details open)", d["scrollW"] <= d["vw"], (d["scrollW"], d["vw"]))
            check("dark theme from the system", d["bg"] == "rgb(15, 20, 26)", d["bg"])
            toc_open = pg.evaluate("() => document.getElementById('toc').open")
            check("phone width: the table of contents is collapsed", toc_open is False)
            pg.close()

            pg = new_page()
            pg.add_init_script("try { localStorage.setItem('switchboard.theme', 'dark'); } catch (e) {}")
            pg.goto(base + "/dev")
            pg.wait_for_timeout(300)
            bg = pg.evaluate("() => getComputedStyle(document.body).backgroundColor")
            check("the board's own theme choice wins (dark on a light system)", bg == "rgb(15, 20, 26)", bg)
            pg.close()

            # the board's profile menu links to the portal
            pg = new_page()
            pg.goto(base + "/")
            pg.wait_for_timeout(1200)
            pg.click("#acct-btn")
            pg.wait_for_timeout(300)
            link = pg.evaluate("""() => { const p = document.getElementById('acct-dev');
              return { vis: !p.hidden && p.getClientRects().length > 0, hrefs: [...p.querySelectorAll('a')].map((a) => a.getAttribute('href')),
                       none: document.querySelector('#acct-keys li.none')?.textContent || null }; }""")
            check("board: the profile menu links to /dev and /dev/keys", link["vis"] and link["hrefs"] == ["/dev/keys", "/dev"], link)
            check("board: api_keys from /api/me (none yet)", link["none"] == "No keys yet.", link)
            pg.click("#acct-dev-portal")
            pg.wait_for_url(base + "/dev", timeout=5000)
            check("board: the link opens the portal", pg.url == base + "/dev", pg.url)
            pg.close()

            # the key page, as an owner
            pg = new_page()
            pg.goto(base + "/dev/keys")
            pg.wait_for_selector("#k-scopes input", timeout=10000)
            k = pg.evaluate(KEYS_JS)
            check("owner: create (not request), an owner field and column", k["submit"] == "Create key" and k["ownerField"] and k["ownerCol"], k)
            pg.fill("#k-name", "CI reader")
            pg.check("#k-scopes input[value='read:tasks']")
            pg.click("#k-submit")
            pg.wait_for_selector("#once:not([hidden])", timeout=15000)
            k = pg.evaluate(KEYS_JS)
            key = k["modalKey"]
            check("the new key in a one-time dialog", KEY_RE.fullmatch(key) is not None and k["modal"], k["modalWhat"])
            check("  with what it is for", "CI reader" in k["modalWhat"] and "read:tasks" in k["modalWhat"], k["modalWhat"])
            check("  never in browser storage", key not in k["storage"], k["storage"][:200])
            pg.click("#once-done")
            pg.wait_for_timeout(300)
            k = pg.evaluate(KEYS_JS)
            html = pg.content()
            check("closing the dialog drops the key from the page", not k["modal"] and k["modalKey"] == "" and key not in html)
            row = next((r for r in k["rows"] if "CI reader" in r["text"]), None)
            check("the new key is listed, active", row is not None and "active" in row["text"], k["rows"])
            pg.reload()
            pg.wait_for_selector("#keys-table tbody tr[data-kid]", timeout=10000)
            check("a reload never shows the key again", key not in pg.content())
            disk = ""
            for root, _ds, fs in os.walk(home):
                for f in fs:
                    with open(os.path.join(root, f), "rb") as fh:
                        disk += fh.read().decode("utf-8", "replace")
            check("the key is nowhere in BOARD_HOME", key not in disk and key.split("_")[-1] not in disk)
            sel = "#keys-table tr[data-kid='%s'] button[data-act='revoke']" % row["id"]
            pg.click(sel)
            armed = pg.text_content(sel)
            check("revoke asks to confirm first", armed.startswith("Confirm"), armed)
            pg.click(sel)
            pg.wait_for_timeout(1500)
            k = pg.evaluate(KEYS_JS)
            row = next((r for r in k["rows"] if r["id"] == row["id"]), None)
            check("confirmed: revoked", row is not None and "revoked" in row["text"] and not row["acts"], (row, k["status"]))
            pg.close()

            pg = new_page(width=390)
            pg.goto(base + "/dev/keys")
            pg.wait_for_selector("#keys-table tbody tr[data-kid]", timeout=10000)
            k = pg.evaluate(KEYS_JS)
            check("key page at phone width: no sideways scroll", k["scrollW"] <= k["vw"], (k["scrollW"], k["vw"]))
            pg.close()
            srv.terminate()
            srv.wait(10)
            srv = None

            print("no role on the local listener (a viewer):")
            srv, base, home = serve(os.path.join(tmp, "viewer"), ())
            pg = new_page()
            pg.goto(base + "/dev/keys")
            pg.wait_for_selector("#k-scopes input", timeout=10000)
            k = pg.evaluate(KEYS_JS)
            check("viewer: request (not create), no owner field or column", k["submit"] == "Request key" and not k["ownerField"] and not k["ownerCol"], k)
            pg.fill("#k-name", "my script")
            pg.click("#k-submit")
            pg.wait_for_selector("#once:not([hidden])", timeout=15000)
            k = pg.evaluate(KEYS_JS)
            check("viewer: the requested key, once, with the approval note",
                  KEY_RE.fullmatch(k["modalKey"]) is not None and "works once an owner approves it" in k["modalWhat"], k["modalWhat"])
            pg.click("#once-done")
            pg.wait_for_timeout(500)
            k = pg.evaluate(KEYS_JS)
            row = k["rows"][0] if k["rows"] else None
            check("viewer: pending, and they can withdraw it", row is not None and "pending" in row["text"] and row["acts"] == ["Withdraw"], k["rows"])
            pg.close()

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv:
            srv.terminate()
            srv.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\npassed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
