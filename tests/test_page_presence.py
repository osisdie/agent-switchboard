#!/usr/bin/env python3
"""tests/test_page_presence.py — optional headless check of the owner's status on the board page
(lib/availability.py): the chip on the profile button (Online / Do not disturb / Away), the status
control in the profile menu (owners only; it writes owner-dnd and owner-away through board-serve
and keeps them exclusive), the asker's composer notice while askers are declined, and a declined
thread (the note and the automatic reply from BOARD) for the asker and the owner. English and
繁體中文, desktop and phone width.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_presence.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a real bin/board-serve on a temporary BOARD_HOME and
config; never touches ~/.agent-switchboard.
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
    p = s.getsockname()[1]
    s.close()
    return p


def serve(tmp, role):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    approvers = ["boss@example.invalid"] + (["local"] if role in ("owner", "approver") else [])
    owners = ["local"] if role == "owner" else ["boss@example.invalid"]
    askers = ["local"] if role == "asker" else []
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("owners: %s\napprovers: %s\naskers: %s\nsessions:\n  - code: API\n    cwd: /w\n"
                 "serve:\n  enabled: true\n  port: %d\n" % (json.dumps(owners), json.dumps(approvers), json.dumps(askers), port))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(tmp, "claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
    with open(os.path.join(tmp, "claude.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh)
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


def declined_fixture(home):
    """An asker's message declined while the owner was on do not disturb, with its canned reply."""
    now = int(time.time() * 1000)
    mid, rid = "m-%d-abc123" % now, "r-%d-abc123" % now
    os.makedirs(os.path.join(home, "messages", mid, "replies"))
    with open(os.path.join(home, "messages", mid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": mid, "to": "API", "by": "ann@example.invalid", "kind": "asker", "asker": "ann@example.invalid",
                   "created_at": now, "status": "declined", "declined_reason": "owner_dnd", "declined_at": now,
                   "body": "is the export done?"}, fh)
    with open(os.path.join(home, "messages", mid, "replies", rid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": rid, "from": "BOARD", "body": "CANNED-REPLY-TEXT", "created_at": now, "canned": "owner_dnd"}, fh)
    return mid


VIEW_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const chip = document.getElementById('acct-pres'), box = document.getElementById('acct-presence');
  const r = (el) => { const b = el.getBoundingClientRect(); return [b.left, b.right]; };
  return { chip: vis(chip) ? chip.textContent : null, chipCls: chip.className, menu: vis(box),
           checked: (document.querySelector('input[name="sb-presence"]:checked') || {}).value || null,
           note: (document.getElementById('acct-pres-note') || {}).textContent || '',
           paused: vis(document.getElementById('msg-paused')) ? document.getElementById('msg-paused').textContent : null,
           box: vis(box) ? r(box) : null, chipBox: vis(chip) ? r(chip) : null, vw: innerWidth,
           scrollW: document.scrollingElement.scrollWidth };
}"""

THREAD_JS = """(id) => {
  const li = document.getElementById('thread-' + id);
  if (!li) return null;
  const st = li.querySelector('.thread-top .status'), note = li.querySelector('.held.declined');
  const canned = li.querySelector('.replies li.canned');
  return { status: st ? st.textContent : null, note: note ? note.textContent : null,
           canned: canned ? canned.textContent : null };
}"""


def wait_file(path, exists, timeout=10):
    for _ in range(int(timeout * 10)):
        if os.path.exists(path) == exists:
            return True
        time.sleep(0.1)
    return False


def open_page(browser, base, lang, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    if lang == "zh-TW":
        ctx.add_init_script("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
    page = ctx.new_page()
    page.goto(base + "/")
    page.wait_for_function("() => !document.getElementById('composer').hidden || "
                           "!document.getElementById('msg-readonly').hidden", timeout=10000)
    return ctx, page


L = {"en": {"online": "Online", "dnd": "Do not disturb", "away": "Away",
            "paused": "The owner isn't taking requests right now", "note": "Declined automatically, not relayed",
            "auto": "automatic reply"},
     "zh-TW": {"online": "在線", "dnd": "勿擾", "away": "不在", "paused": "目前不接受詢問或派工",
               "note": "已自動婉拒，沒有轉送", "auto": "自動回覆"}}


def owner_checks(browser, base, home, lang, width):
    T, tag = L[lang], "owner %s %d" % (lang, width)
    ctx, page = open_page(browser, base, lang, width)
    page.wait_for_function("() => !document.getElementById('acct-pres').hidden", timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: the chip says online" % tag, v["chip"] == T["online"], v)
    check("%s: the chip is inside the viewport" % tag, v["chipBox"] and v["chipBox"][0] >= 0 and v["chipBox"][1] <= v["vw"], v)
    page.click("#acct-btn")
    v = page.evaluate(VIEW_JS)
    check("%s: the status control is in the profile menu" % tag, v["menu"] and v["checked"] == "online", v)
    check("%s: the menu fits, no sideways scroll" % tag,
          v["box"] and v["box"][0] >= 0 and v["box"][1] <= v["vw"] and v["scrollW"] <= v["vw"], v)
    dnd, away = os.path.join(home, "owner-dnd"), os.path.join(home, "owner-away")
    page.click("#acct-presence label:has(input[value=dnd])")
    check("%s: do not disturb writes owner-dnd" % tag, wait_file(dnd, True), os.listdir(home))
    check("%s: do not disturb is not away" % tag, not os.path.exists(away))
    page.wait_for_function("(t) => document.getElementById('acct-pres').textContent === t", arg=T["dnd"], timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: chip and menu show do not disturb" % tag, v["chip"] == T["dnd"] and "dnd" in v["chipCls"] and v["checked"] == "dnd", v)
    if not page.evaluate("() => !document.getElementById('acct-panel').hidden"):
        page.click("#acct-btn")
    page.click("#acct-presence label:has(input[value=away])")
    check("%s: away writes owner-away and clears do not disturb" % tag,
          wait_file(away, True) and wait_file(dnd, False), os.listdir(home))
    page.wait_for_function("(t) => document.getElementById('acct-pres').textContent === t", arg=T["away"], timeout=10000)
    if not page.evaluate("() => !document.getElementById('acct-panel').hidden"):
        page.click("#acct-btn")
    page.click("#acct-presence label:has(input[value=online])")
    check("%s: online clears both" % tag, wait_file(away, False) and wait_file(dnd, False), os.listdir(home))
    page.wait_for_function("(t) => document.getElementById('acct-pres').textContent === t", arg=T["online"], timeout=10000)
    ctx.close()


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-presence.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            print("owner:")
            srv, base, home, env = serve(os.path.join(tmp, "owner"), "owner")
            for lang, width in (("en", 1280), ("zh-TW", 1280), ("en", 390)):
                owner_checks(browser, base, home, lang, width)
            mid = declined_fixture(home)
            for lang in ("en", "zh-TW"):
                ctx, page = open_page(browser, base, lang, 1280)
                page.wait_for_selector("#thread-" + mid, timeout=10000)
                t = page.evaluate(THREAD_JS, mid)
                check("owner %s: a declined thread is shown, marked declined" % lang,
                      t and t["status"] == "declined" and L[lang]["note"] in (t["note"] or ""), t)
                check("owner %s: the canned reply is in the thread" % lang,
                      t and "CANNED-REPLY-TEXT" in (t["canned"] or "") and L[lang]["auto"] in t["canned"], t)
                ctx.close()
            srv.terminate()
            srv.wait(10)

            print("approver, not owner:")
            srv, base, home, env = serve(os.path.join(tmp, "approver"), "approver")
            ctx, page = open_page(browser, base, "en", 1280)
            page.click("#acct-btn")
            v = page.evaluate(VIEW_JS)
            check("approver: no chip, no status control", v["chip"] is None and not v["menu"], v)
            ctx.close()
            srv.terminate()
            srv.wait(10)

            print("asker:")
            srv, base, home, env = serve(os.path.join(tmp, "asker"), "asker")
            ctx, page = open_page(browser, base, "en", 1280)
            v = page.evaluate(VIEW_JS)
            check("asker: no notice while requests are taken", v["paused"] is None, v)
            check("asker: no chip", v["chip"] is None, v)
            subprocess.run([os.path.join(REPO, "bin", "board"), "dnd", "on", "--by", "board:boss@example.invalid"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            page.wait_for_function("() => !document.getElementById('msg-paused').hidden", timeout=10000)
            v = page.evaluate(VIEW_JS)
            check("asker en: the composer notice is shown", v["paused"] and L["en"]["paused"] in v["paused"], v)
            page.fill("#msg-input", "@API is the export done?")
            page.click("#msg-send")
            page.wait_for_function("() => [...document.querySelectorAll('.toast')].some((t) => /not relayed/.test(t.textContent))",
                                   timeout=10000)
            names = [n[:-5] for n in os.listdir(os.path.join(home, "messages")) if n.endswith(".json")]
            mid = names[0] if names else ""
            page.wait_for_function("(id) => !!document.querySelector('#thread-' + id + ' .replies li.canned')", arg=mid,
                                   timeout=10000)
            t = page.evaluate(THREAD_JS, mid)
            check("asker en: the declined thread shows the note and the canned reply",
                  t and t["status"] == "declined" and L["en"]["note"] in (t["note"] or "")
                  and L["en"]["paused"] in (t["canned"] or ""), t)
            ctx.close()
            for width in (1280, 390):
                ctx, page = open_page(browser, base, "zh-TW", width)
                page.wait_for_function("() => !document.getElementById('msg-paused').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("asker zh-TW %d: the composer notice is shown" % width,
                      v["paused"] and L["zh-TW"]["paused"] in v["paused"] and v["scrollW"] <= v["vw"], v)
                page.wait_for_selector("#thread-" + mid, timeout=10000)
                t = page.evaluate(THREAD_JS, mid)
                check("asker zh-TW %d: the declined note is translated" % width,
                      t and L["zh-TW"]["note"] in (t["note"] or "") and L["zh-TW"]["auto"] in (t["canned"] or ""), t)
                ctx.close()
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\npassed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
