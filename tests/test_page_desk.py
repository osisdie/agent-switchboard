#!/usr/bin/env python3
"""tests/test_page_desk.py — optional headless check of the local asker desk on the board page
(lib/desk.py): the owner's header pill (answering / desk not running / down), the asker's composer
note (and no "paused" note while the desk answers, even on do-not-disturb), the thread's "local
model" status and notes (with the desk, after a timeout, a suggested forward) and the "local model ·
may be less accurate" label on the desk's replies. English and 繁體中文, desktop and phone width.

The desk process is a stand-in (a sleeping python with the desk's launch flags and env), registered
like a Claude session, so desk.view() verifies it as alive; the endpoint's health is a fixture.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and exits 0.

  python3 tests/test_page_desk.py [-v]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERBOSE = "-v" in sys.argv[1:]
BASE_URL = "http://127.0.0.1:9"

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


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def serve(tmp, role, up=True):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess, os.path.join(tmp, "repo")):
        os.makedirs(d)
    port = free_port()
    approvers = ["boss@example.invalid"] + (["local"] if role == "owner" else [])
    owners = ["local"] if role == "owner" else ["boss@example.invalid"]
    askers = ["local"] if role == "asker" else []
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("owners: %s\napprovers: %s\naskers: %s\nsessions:\n  - code: API\n    cwd: /w\n  - code: ASK\n    cwd: /w\n"
                 "local_model: {enabled: true, session: ASK, env_file: local-model.env, repos: [%s]}\n"
                 "serve:\n  enabled: true\n  port: %d\n" % (json.dumps(owners), json.dumps(approvers), json.dumps(askers),
                                              os.path.join(tmp, "repo"), port))
    with open(os.path.join(cfg, "local-model.env"), "w", encoding="utf-8") as fh:
        fh.write("ANTHROPIC_BASE_URL=%s\nANTHROPIC_AUTH_TOKEN=page-test\n" % BASE_URL)
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0",
               CLAUDE_JSON=os.path.join(tmp, "claude.json"))
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
    with open(os.path.join(tmp, "claude.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh)
    # the desk stand-in: exactly the launch's arguments (lib/desk.py launch_argv, after a script
    # path, as "node cli.js ..." would be), its own cwd and env, registered as session ASK
    argv = json.loads(subprocess.run([sys.executable, "-c", (
        "import json, sys; sys.path.insert(0, %r); import desk, fleet; h = fleet.ensure_home(); c = fleet.load_fleet(); "
        "print(json.dumps([desk.launch_argv(h, c, desk.gen(h, c)), desk.cwd_path(h)]))") % os.path.join(REPO, "lib")],
        env=env, capture_output=True, text=True, check=True).stdout)
    # "node cli.js <args>" as an npm install runs it: a "node" that is python, a .js that is python
    node = os.path.join(tmp, "bin", "node")
    os.makedirs(os.path.dirname(node), exist_ok=True)
    os.symlink(sys.executable, node)
    standin = os.path.join(tmp, "cli.js")
    with open(standin, "w") as fh:
        fh.write("import time\ntime.sleep(600)\n")
    desk = subprocess.Popen([node, standin] + argv[0][1:], cwd=argv[1],
                            env=dict(env, ANTHROPIC_BASE_URL=BASE_URL))
    wj(os.path.join(sess, "%d.json" % desk.pid), {"pid": desk.pid, "sessionId": "desk-sid", "name": "ASK",
                                                 "status": "idle", "cwd": "/w"})
    now = int(time.time() * 1000)
    wj(os.path.join(home, "desk", "health.json"), {"state": "up" if up else "down", "checked_at": now,
                                                  "reason": None if up else "unreachable"})
    subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    beat(home)
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return [proc, desk], "http://127.0.0.1:%d" % port, home, env


BEATING = set()


def beat(home):
    """A looping collector stand-in: desk/tick.json fresh while the test runs."""
    def run():
        while home in BEATING:
            wj(os.path.join(home, "desk", "tick.json"), {"at": int(time.time() * 1000), "by": "test"})
            time.sleep(2)
    BEATING.add(home)
    wj(os.path.join(home, "desk", "tick.json"), {"at": int(time.time() * 1000), "by": "test"})
    threading.Thread(target=run, daemon=True).start()


def stop(procs):
    for p in procs:
        p.terminate()
        p.wait(10)


def threads(home):
    """With the desk, answered by it (with a suggestion), and taken back after a timeout."""
    now = int(time.time() * 1000)
    out = {}
    for n, (status, desk, reply) in enumerate((
            ("desk", {"session": "ASK", "state": "delivered", "queued_at": now}, None),
            ("replied", {"session": "ASK", "state": "answered", "queued_at": now, "answered_at": now, "suggest": "API"},
             "LOCAL-ANSWER: see lib/messages.py:42"),
            ("new", {"session": "ASK", "state": "timeout", "queued_at": now - 700000, "left_at": now}, None))):
        mid = "m-%d-dsk%d" % (now - n, n)
        wj(os.path.join(home, "messages", mid + ".json"), {
            "id": mid, "to": "API", "by": "ann@example.invalid", "kind": "asker", "asker": "ann@example.invalid",
            "created_at": now - n, "status": status, "desk": desk, "body": "question %d" % n})
        if reply:
            rid = "r-%d-dsk%d" % (now, n)
            wj(os.path.join(home, "messages", mid, "replies", rid + ".json"),
               {"id": rid, "from": "ASK", "body": reply, "created_at": now, "local_model": True})
        out[status] = mid
    return out


VIEW_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const pill = document.getElementById('lm-pill');
  const t = (id) => vis(document.getElementById(id)) ? document.getElementById(id).textContent : null;
  return { pill: vis(pill) ? document.getElementById('lm-pill-t').textContent : null, pillCls: pill.className,
           local: t('msg-local'), paused: t('msg-paused'), vw: innerWidth, scrollW: document.scrollingElement.scrollWidth };
}"""

THREAD_JS = """(id) => {
  const li = document.getElementById('thread-' + id);
  if (!li) return null;
  const st = li.querySelector('.thread-top .status'), note = li.querySelector('.held.desk');
  const lab = li.querySelector('.replies .auto.local');
  return { status: st ? st.textContent : null, note: note ? note.textContent : null, label: lab ? lab.textContent : null };
}"""

L = {"en": {"answering": "Local model answering", "down": "Local model down", "status": "local model",
            "with": "With the local model", "timeout": "didn't answer in time", "suggest": "suggests that API",
            "label": "local model · may be less accurate", "local": "A local model answers"},
     "zh-TW": {"answering": "本機模型回答中", "down": "本機模型離線", "status": "本機模型",
               "with": "交給本機模型", "timeout": "沒有及時回答", "suggest": "建議由 API",
               "label": "本機模型 · 可能不夠準確", "local": "目前由本機模型"}}


def open_page(browser, base, lang, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    if lang == "zh-TW":
        ctx.add_init_script("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
    page = ctx.new_page()
    page.goto(base + "/")
    page.wait_for_function("() => !document.getElementById('composer').hidden || "
                           "!document.getElementById('msg-readonly').hidden", timeout=10000)
    return ctx, page


def main():
    tmp = tempfile.mkdtemp(prefix="sb-page-desk.")
    procs = []
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            print("owner, desk up:")
            procs, base, home, env = serve(os.path.join(tmp, "owner"), "owner")
            ids = threads(home)
            for lang, width in (("en", 1280), ("zh-TW", 1280), ("en", 390)):
                T, tag = L[lang], "owner %s %d" % (lang, width)
                ctx, page = open_page(browser, base, lang, width)
                page.wait_for_function("() => !document.getElementById('lm-pill').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("%s: the pill says answering" % tag, v["pill"] == T["answering"] and " up" in v["pillCls"], v)
                check("%s: no sideways scroll" % tag, v["scrollW"] <= v["vw"], v)
                page.wait_for_selector("#thread-" + ids["replied"], timeout=10000)
                t = page.evaluate(THREAD_JS, ids["desk"])
                check("%s: a thread with the desk says so" % tag,
                      t and t["status"] == T["status"] and T["with"] in (t["note"] or ""), t)
                t = page.evaluate(THREAD_JS, ids["replied"])
                check("%s: the desk's reply is labelled" % tag, t and t["label"] == T["label"], t)
                check("%s: the suggested forward is noted" % tag, t and T["suggest"] in (t["note"] or ""), t)
                t = page.evaluate(THREAD_JS, ids["new"])
                check("%s: a timed-out thread says it went the usual way" % tag,
                      t and T["timeout"] in (t["note"] or "") and t["label"] is None, t)
                ctx.close()
            stop(procs)

            print("owner, desk down:")
            procs, base, home, env = serve(os.path.join(tmp, "down"), "owner", up=False)
            ctx, page = open_page(browser, base, "en", 1280)
            page.wait_for_function("() => !document.getElementById('lm-pill').hidden", timeout=10000)
            v = page.evaluate(VIEW_JS)
            check("owner: the pill says down", v["pill"] == L["en"]["down"] and " down" in v["pillCls"], v)
            ctx.close()
            stop(procs)

            print("asker, desk up:")
            procs, base, home, env = serve(os.path.join(tmp, "asker"), "asker")
            for lang, width in (("en", 1280), ("zh-TW", 390)):
                ctx, page = open_page(browser, base, lang, width)
                page.wait_for_function("() => !document.getElementById('msg-local').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("asker %s: no owner pill" % lang, v["pill"] is None, v)
                check("asker %s %d: the composer says a local model answers" % (lang, width),
                      v["local"] and L[lang]["local"] in v["local"] and v["scrollW"] <= v["vw"], v)
                ctx.close()
            subprocess.run([os.path.join(REPO, "bin", "board"), "dnd", "on", "--by", "board:boss@example.invalid"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            ctx, page = open_page(browser, base, "en", 1280)
            page.wait_for_function("() => !document.getElementById('msg-local').hidden", timeout=10000)
            v = page.evaluate(VIEW_JS)
            check("asker: on do-not-disturb the desk still answers (no paused note)", v["paused"] is None and v["local"], v)
            page.fill("#msg-input", "@API where is the rate limit set?")
            page.click("#msg-send")
            names = []
            for _ in range(100):
                names = [n[:-5] for n in os.listdir(os.path.join(home, "messages")) if n.endswith(".json")] \
                    if os.path.isdir(os.path.join(home, "messages")) else []
                if names:
                    break
                time.sleep(0.1)
            rec = json.load(open(os.path.join(home, "messages", names[0] + ".json"), encoding="utf-8")) if names else {}
            check("asker: the message goes to the desk, no canned reply",
                  rec.get("status") == "desk" and not os.path.isdir(os.path.join(home, "messages", names[0], "replies")),
                  rec)
            page.wait_for_selector("#thread-" + names[0], timeout=10000)
            t = page.evaluate(THREAD_JS, names[0])
            check("asker: the new thread says it is with the local model", t and L["en"]["with"] in (t["note"] or ""), t)
            ctx.close()
            stop(procs)
            procs = []
            browser.close()
    finally:
        stop(procs)
        BEATING.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    print("passed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
