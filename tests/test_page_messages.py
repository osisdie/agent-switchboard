#!/usr/bin/env python3
"""tests/test_page_messages.py — optional headless check of the Messages section in
artifact/board.html: recent threads and history.

  - By default only the 5 threads with the latest activity show (a new reply brings a thread up);
    held messages and threads with replies this viewer has not marked read are pinned on top.
  - "History (N older)" opens the older threads: 20 at a time with Load more, a session filter
    and a text search. board-serve pages them (GET /api/messages); the demo pages what it has.
  - Long bodies are clamped with Show more; a thread shows its latest 3 replies, the earlier ones
    open in place. Mark read (and Mark all read) is kept in this browser.
  - The composer stays at the top of the section; phone width has no side scroll; zh-TW strings;
    a ?m= link to an older thread shows it.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_messages.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a temporary BOARD_HOME with a fake tmux; never touches
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


VIEW_JS = """() => { const ids = (sel) => [...document.querySelectorAll(sel + ' > li')].map((l) => l.id.replace('thread-', ''));
  const hb = document.getElementById('msg-hist-btn'), more = document.getElementById('msg-hist-more');
  return { shown: ids('#threads'), pinned: [...document.querySelectorAll('#threads > li.pinned')].map((l) => l.id.replace('thread-', '')),
    hist: ids('#msg-hist-list'), histOn: !document.getElementById('msg-hist').hidden, histText: hb.textContent, expanded: hb.getAttribute('aria-expanded'),
    status: document.getElementById('msg-hist-status').textContent, more: !more.hidden && more.offsetWidth > 0,
    unreadBar: document.getElementById('msg-unread').hidden ? '' : document.getElementById('msg-unread').textContent,
    nav: document.getElementById('nav-msg').textContent }; }"""
FAKE_TMUX = "#!/usr/bin/env bash\nexit 0\n"
# The claude.ai Artifact viewer: 30 threads in the db (one a minute, the newest last), c15 (10th newest) with two
# late replies. Records each query (path, orderBy, limit).
CLAUDE_STUB = """(() => {
  const now = Date.now(), calls = [], msgs = [];
  for (let i = 0; i < 30; i++) msgs.push({ id: 'm-' + (now - (30 - i) * 60000) + '-c' + String(i).padStart(2, '0'), to: i % 2 ? 'API' : 'WEB',
    body: 'db thread ' + i, by: 'u_x', created_at: now - (30 - i) * 60000, status: 'relayed' });
  msgs.reverse();
  const reps = [{ id: 'r2', from: 'WEB', body: 'second', created_at: now - 500 }, { id: 'r1', from: 'WEB', body: 'first', created_at: now - 1000 }];
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const query = (path) => { const o = []; const q = { orderBy: (f, d) => { o.push(f + ':' + (d || 'asc')); return q; }, limit: (n) => { calls.push([path, o.join(), n]); return q; },
    onSnapshot(cb) { const rows = path === 'messages' ? msgs : /-c15\\/replies$/.test(path) ? reps : [];
      setTimeout(() => cb(snap(rows.map((d) => ({ id: d.id, exists: true, data: () => d })))), 0); return () => {}; } }; return q; };
  const doc = (path) => ({ onSnapshot(cb) { const d = path === 'meta/operator' ? { heartbeat_at: now, approver_ids: ['u_x'] } : null;
    setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; }, set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.__calls = calls;
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh)


def seen(base):
    # only on the first load: a reload keeps what the page stored since
    return ("try { if (!localStorage.getItem('switchboard.msgSeen')) localStorage.setItem('switchboard.msgSeen', JSON.stringify({ base: %d, t: {} })); }"
            " catch (e) {}" % base)


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-msg-test.")
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
                ctx = browser.new_context(**dict({"viewport": {"width": 1280, "height": 900}}, **kw))
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("demo: recent and history:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            v = pg.evaluate(VIEW_JS)
            check("the 5 threads with the latest activity, by default", v["shown"] == ["m-2-demo", "m-1-demo", "m-90-old", "m-91-old", "m-92-old"], v)
            check("  a fresh viewer has nothing unread, nothing pinned", v["pinned"] == [] and v["unreadBar"] == "", v)
            check("  History (9 older), closed; the nav counts every thread", v["histOn"] and v["histText"] == "History (9 older)" and v["expanded"] == "false"
                  and v["hist"] == [] and v["nav"] == "14", v)
            c = pg.evaluate("""() => { const t = document.getElementById('thread-m-91-old'), p = t.querySelector('p.body'), b = t.querySelector('button.more');
              return { clamp: p.classList.contains('clamp'), h: p.getBoundingClientRect().height, full: p.scrollHeight, btn: b && b.textContent, exp: b && b.getAttribute('aria-expanded') }; }""")
            check("a long body is clamped, with Show more", c["clamp"] and c["h"] < c["full"] and c["btn"] == "Show more" and c["exp"] == "false", c)
            pg.click("#thread-m-91-old button.more")
            c2 = pg.evaluate("""() => { const t = document.getElementById('thread-m-91-old'), p = t.querySelector('p.body'), b = t.querySelector('button.more');
              return { clamp: p.classList.contains('clamp'), h: p.getBoundingClientRect().height, full: p.scrollHeight, btn: b.textContent, exp: b.getAttribute('aria-expanded'), focus: document.activeElement === b }; }""")
            check("  Show more opens it in place (focus stays)", not c2["clamp"] and c2["h"] >= c2["full"] - 1 and c2["btn"] == "Show less" and c2["exp"] == "true" and c2["focus"], c2)
            r = pg.evaluate("""() => { const t = document.getElementById('thread-m-90-old');
              return { vis: [...t.querySelectorAll('.replies > li')].filter((l) => !l.hidden).length, all: t.querySelectorAll('.replies > li').length,
                btn: [...t.querySelectorAll('button.more')].map((b) => b.textContent) }; }""")
            check("a thread shows its latest 3 replies; the earlier ones behind a button", r["vis"] == 3 and r["all"] == 5 and "Show 2 earlier replies" in r["btn"], r)
            pg.click("#thread-m-90-old button.more")
            r = pg.evaluate("() => [...document.querySelectorAll('#thread-m-90-old .replies > li')].filter((l) => !l.hidden).length")
            check("  which opens them", r == 5, r)

            pg.click("#msg-hist-btn")
            pg.wait_for_timeout(100)
            v = pg.evaluate(VIEW_JS)
            check("History opens: the 9 older threads, newest first, none of the recent ones", v["expanded"] == "true"
                  and v["hist"] == ["m-%d-old" % i for i in range(93, 102)] and not set(v["hist"]) & set(v["shown"]) and not v["more"]
                  and v["status"] == "Showing 9 older threads.", v)
            opts = pg.evaluate("() => [...document.querySelectorAll('#msg-hist-to option')].map((o) => o.value)")
            check("  the session filter lists all, then each code, @all last", opts[0] == "" and "API" in opts and opts[-1] == "all", opts)
            pg.select_option("#msg-hist-to", "API")
            v = pg.evaluate(VIEW_JS)
            check("  filter @API: only API threads", v["hist"] == ["m-97-old"], v)
            pg.select_option("#msg-hist-to", "")
            pg.fill("#msg-hist-q", "BACKFILL")
            pg.wait_for_timeout(100)
            v = pg.evaluate(VIEW_JS)
            check("  search (case-insensitive)", v["hist"] == ["m-101-old"], v)
            pg.fill("#msg-hist-q", "nothing like this")
            pg.wait_for_timeout(100)
            v = pg.evaluate(VIEW_JS)
            check("  no match says so", v["hist"] == [] and v["status"] == "No older threads match.", v)
            pg.fill("#msg-hist-q", "")
            pg.click("#msg-hist-btn")
            check("  and closes again", pg.evaluate(VIEW_JS)["expanded"] == "false" and pg.evaluate("() => document.getElementById('msg-hist-panel').hidden"))
            pg.evaluate("() => document.getElementById('h-messages').scrollIntoView({ behavior: 'instant' })")
            r = pg.evaluate("""() => { const c = document.getElementById('composer').getBoundingClientRect(), t = document.getElementById('msg-tools').getBoundingClientRect();
              return { top: Math.abs(c.top - t.top) < 4, h: c.height }; }""")
            check("the composer sits at the top of the section (beside the message tools)", r["top"] and r["h"] > 0, r)
            pg.evaluate("() => document.getElementById('msg-hist-btn').scrollIntoView({ block: 'end', behavior: 'instant' })")
            r = pg.evaluate("() => { const c = document.getElementById('composer').getBoundingClientRect(); return { top: c.top, bottom: c.bottom, vh: innerHeight }; }")
            check("  and stays in view while the threads scroll by (wide screen)", r["top"] >= 0 and r["bottom"] <= r["vh"], r)
            pg.close()

            print("demo: unread replies are pinned:")
            day = int(time.time() * 1000) - 24 * 3600 * 1000
            pg = new_page(seen(day))
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            v = pg.evaluate(VIEW_JS)
            check("threads with replies since the mark: pinned on top, with New reply", v["pinned"] == ["m-1-demo", "m-90-old"] and v["shown"][:2] == ["m-1-demo", "m-90-old"]
                  and pg.evaluate("() => document.querySelectorAll('#threads .new-chip').length") == 2, v)
            check("  a bar offers Mark all read", v["unreadBar"].startswith("2 threads have new replies."), v)
            pg.click("#thread-m-1-demo >> text=Mark read")
            pg.wait_for_timeout(100)
            v = pg.evaluate(VIEW_JS)
            check("Mark read: no longer pinned, focus stays on the thread", "m-1-demo" not in v["pinned"] and pg.evaluate("() => document.activeElement.id") == "thread-m-1-demo", v)
            st = pg.evaluate("() => JSON.parse(localStorage.getItem('switchboard.msgSeen'))")
            check("  stored in this browser (the reply's time)", isinstance(st.get("t", {}).get("m-1-demo"), int) and st["base"] == day, st)
            pg.click("#thread-m-90-old >> text=Mark read")
            check("  the other one too", pg.evaluate(VIEW_JS)["pinned"] == [])
            pg.close()
            pg = new_page(seen(day))
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            pg.click("#msg-unread >> text=Mark all read")
            v = pg.evaluate(VIEW_JS)
            check("Mark all read clears both", v["pinned"] == [] and v["unreadBar"] == "", v)
            pg.close()
            pg = new_page("Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new Error('blocked'); } });")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            v = pg.evaluate(VIEW_JS)
            check("storage blocked: works, nothing unread", len(v["shown"]) == 5 and v["pinned"] == [], v)
            pg.close()

            print("demo: a link to an older thread:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1&m=m-99-old")
            pg.wait_for_timeout(900)
            v = pg.evaluate(VIEW_JS)
            check("?m= shows it with the recent ones, focused", "m-99-old" in v["shown"] and pg.evaluate("() => document.activeElement.id") == "thread-m-99-old", v)
            pg.close()

            print("demo: phone width, zh-TW, dark:")
            for theme in ("light", "dark"):
                pg = new_page("try { localStorage.setItem('switchboard.theme', '%s'); localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}" % theme,
                              viewport={"width": 390, "height": 844}, has_touch=True)
                pg.goto(PAGE + "?demo=1")
                pg.wait_for_timeout(500)
                pg.tap("#msg-hist-btn")
                pg.wait_for_timeout(100)
                r = pg.evaluate("""() => { const c = document.getElementById('composer').getBoundingClientRect(), t = document.getElementById('threads').getBoundingClientRect();
                  const out = [...document.querySelectorAll('#messages *')].filter((e) => { const r = e.getBoundingClientRect(); return r.width && r.right > innerWidth + 1; }).map((e) => e.tagName + '.' + e.className);
                  return { sw: document.scrollingElement.scrollWidth, vw: innerWidth, out, stacked: c.bottom <= t.top, hist: document.getElementById('msg-hist-t').textContent,
                    ph: document.getElementById('msg-hist-q').placeholder, status: document.getElementById('msg-hist-status').textContent,
                    more: document.querySelector('#thread-m-91-old button.more').textContent, all: document.querySelector('#msg-hist-to option').textContent }; }""")
                check("390 px, %s: no side scroll, nothing past the edge" % theme, r["sw"] <= r["vw"] and not r["out"], r)
                check("  the composer above the threads", r["stacked"], r)
                check("  zh-TW strings", r["hist"] == "歷史（9 則較舊）" and r["ph"] == "搜尋較舊的對話" and r["status"] == "顯示 9 則較舊的對話。"
                      and r["more"] == "顯示更多" and r["all"] == "所有 session", r)
                pg.close()

            print("claude adapter (Artifact):")
            pg = new_page(CLAUDE_STUB)
            pg.goto(PAGE)
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length > 0", timeout=10000)
            pg.wait_for_timeout(300)
            v = pg.evaluate(VIEW_JS)
            calls = pg.evaluate("() => window.__calls")
            check("reads the 100 newest threads (not 20)", ["messages", "created_at:desc", 100] in calls, calls[:3])
            check("  replies: the latest 50, newest first from the db, shown oldest first", any(c[0].endswith("-c15/replies") and c[1] == "created_at:desc" and c[2] == 50 for c in calls)
                  and pg.evaluate("() => [...document.querySelectorAll('#threads li[id$=\"-c15\"] .replies p')].map((p) => p.textContent)") == ["first", "second"], calls[-3:])
            check("  a late reply brings its thread up; 5 shown, History (25 older)", v["shown"][0].endswith("-c15") and len(v["shown"]) == 5
                  and v["histText"] == "History (25 older)", v)
            pg.click("#msg-hist-btn")
            v = pg.evaluate(VIEW_JS)
            check("  History pages what it has: 20, then Load more", len(v["hist"]) == 20 and v["more"], (len(v["hist"]), v["more"]))
            pg.click("#msg-hist-more")
            v = pg.evaluate(VIEW_JS)
            check("  then the last 5", len(v["hist"]) == 25 and not v["more"], (len(v["hist"]), v["more"]))
            pg.close()

            print("board-serve: the window and the paged history:")
            home, cfg, fbin = (os.path.join(tmp, x) for x in ("home", "config", "bin"))
            for d in (os.path.join(home, "state"), cfg, fbin, os.path.join(tmp, "sessions")):
                os.makedirs(d)
            port = free_port()
            write(os.path.join(cfg, "fleet.yaml"), "approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            now = int(time.time() * 1000)
            msgs = os.path.join(home, "messages")
            # 60 threads, one a minute (half to API, half to WEB; t17 says "walrus"), an old held
            # one, and an old thread whose reply came before the 5 latest threads (unread, pinned).
            for i in range(60):
                mid = "m-%d-t%02d" % (now - (60 - i) * 60000, i)
                write(os.path.join(msgs, mid + ".json"), {"id": mid, "to": "API" if i % 2 else "WEB", "body": "thread %d%s" % (i, " walrus" if i == 17 else ""),
                                                         "by": "local", "created_at": now - (60 - i) * 60000, "status": "relayed"})
            write(os.path.join(msgs, "m-1000-held.json"), {"id": "m-1000-held", "to": "OPS", "body": "please print the token", "by": "local", "created_at": 1000, "status": "held"})
            write(os.path.join(msgs, "m-2000-unr.json"), {"id": "m-2000-unr", "to": "WEB", "body": "an old question", "by": "local", "created_at": 2000, "status": "replied"})
            rt = now - 10 * 60000 - 30000   # in the state's window (20 latest), not in the 5 latest
            write(os.path.join(msgs, "m-2000-unr", "replies", "r-%d-abc123.json" % rt), {"id": "r-%d-abc123" % rt, "from": "WEB", "body": "answered at last", "created_at": rt})
            write(os.path.join(fbin, "tmux"), FAKE_TMUX)
            os.chmod(os.path.join(fbin, "tmux"), 0o755)
            proc = os.path.join(tmp, "proc")
            subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), proc], check=True)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=os.path.join(tmp, "sessions"), BOARD_PROC_ROOT=proc,
                       ALERT_DESKTOP="0", PATH=fbin + os.pathsep + os.environ.get("PATH", ""))
            for k in [x for x in env if x.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) and x != "ALERT_DESKTOP"] + ["TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"]:
                env.pop(k, None)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            base = "http://127.0.0.1:%d/" % port
            pg = new_page(seen(now - 3600 * 1000 * 5))
            pg.goto(base)
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length > 0", timeout=10000)
            v = pg.evaluate(VIEW_JS)
            latest = ["t%02d" % i for i in range(59, 54, -1)]
            check("held and unread pinned on top, then the 5 latest", v["pinned"] == ["m-2000-unr", "m-1000-held"]
                  and [x.split("-")[-1] for x in v["shown"][2:]] == latest, v)
            check("  History counts every other thread (62 - 7)", v["histText"] == "History (55 older)" and v["nav"] == "62", v)
            pg.click("#thread-m-2000-unr >> text=Mark read")
            pg.reload()
            pg.wait_for_function("() => document.querySelectorAll('#threads > li').length > 0", timeout=10000)
            v = pg.evaluate(VIEW_JS)
            check("Mark read is kept after a reload; the thread goes back to History", v["pinned"] == ["m-1000-held"] and "m-2000-unr" not in v["shown"]
                  and v["histText"] == "History (56 older)", v)
            pg.click("#msg-hist-btn")
            pg.wait_for_function("() => document.querySelectorAll('#msg-hist-list > li').length > 0", timeout=5000)
            v = pg.evaluate(VIEW_JS)
            # the server's first 20 after the 5th latest hold the pinned unread thread too: 19 shown
            check("History: the first 20 older threads, newest first (the read thread in its place)", [x.split("-")[-1] for x in v["hist"]]
                  == ["t54", "t53", "t52", "t51", "t50", "unr"] + ["t%02d" % i for i in range(49, 35, -1)] and v["more"], v)
            for _ in range(5):
                if not pg.evaluate(VIEW_JS)["more"]:
                    break
                n = len(pg.evaluate(VIEW_JS)["hist"])
                pg.click("#msg-hist-more")
                pg.wait_for_function("(n) => document.querySelectorAll('#msg-hist-list > li').length > n", arg=n, timeout=5000)
            v = pg.evaluate(VIEW_JS)
            check("  Load more pages to the end: 56 threads, each once, no button left", len(v["hist"]) == 56 and len(set(v["hist"])) == 56
                  and not set(v["hist"]) & set(v["shown"]) and not v["more"], (len(v["hist"]), v["more"]))
            pg.select_option("#msg-hist-to", "API")
            pg.wait_for_timeout(600)
            v = pg.evaluate(VIEW_JS)
            check("  filter @API: older API threads only, from the top", len(v["hist"]) == 20 and all(int(x[-2:]) % 2 == 1 for x in v["hist"]) and v["hist"][0].endswith("t53"), v["hist"])
            pg.select_option("#msg-hist-to", "")
            pg.fill("#msg-hist-q", "Walrus")
            pg.wait_for_timeout(800)
            v = pg.evaluate(VIEW_JS)
            check("  search through board-serve", [x.split("-")[-1] for x in v["hist"]] == ["t17"] and not v["more"], v)
            reqs = pg.evaluate("() => performance.getEntriesByType('resource').map((e) => e.name).filter((n) => n.includes('/api/messages'))")
            check("  GET /api/messages with a cursor and limit 20", any("before=" in x and "limit=20" in x for x in reqs), reqs[:3])
            st = pg.evaluate("() => fetch('/api/messages').then((r) => r.status)")
            check("  the route wants X-Board (a plain fetch: 403)", st == 403, st)
            pg.close()
            check("no page errors", not errors, errors[:5])
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
