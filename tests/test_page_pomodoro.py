#!/usr/bin/env python3
"""tests/test_page_pomodoro.py — optional headless check of the pomodoro surfaces in
artifact/board.html: the tomato ring on a session card (mm:ss of 25:00, ticking while the session
works, paused otherwise, "🍅×N today", the teammates' combined figure), the Deck key "🍅 Today" (fleet total, focus, 7-day
sparkline) and its panel (sessions today, 14 days), the celebration when a pomodoro completes
(none under reduced motion), zh-TW, phone width with no horizontal page scroll, the Artifact page
(meta/pomodoro, read-only, no clock) and board-serve end to end (the real collector writes
pomodoro.json, a completion reaches the page over SSE).

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_pomodoro.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a temporary BOARD_HOME with a fake tmux; never touches
~/.agent-switchboard or ~/.claude.json.
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


# Counts every celebration burst that is added to the page.
BURST_SPY = """(() => { window.__bursts = 0; new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes)
  if (n.nodeType === 1 && n.classList.contains('pomo-burst')) window.__bursts++; }).observe(document, { childList: true, subtree: true }); })();"""
ROWS_JS = """() => Object.fromEntries([...document.querySelectorAll('.spomo[data-pomo]')].map((e) => [e.dataset.pomo,
  { text: e.innerText.replace(/\\s+/g, ' ').trim(), ring: !!e.querySelector('svg.pring'), live: e.classList.contains('live'), label: e.getAttribute('aria-label') || '',
    dash: (e.querySelector('.pr-val') || { getAttribute: () => '' }).getAttribute('stroke-dasharray') }]))"""
KEY_JS = """() => { const c = document.querySelector('#deck-grid .dcell[data-deck="pomo"]'), k = c && c.querySelector('.dkey');
  const w = document.querySelector('#deck-grid .dcell[data-deck="wide"]');
  if (!k) return null; const r = c.getBoundingClientRect(), rw = w.getBoundingClientRect(), g = document.getElementById('deck-grid').getBoundingClientRect();
  return { text: k.innerText.replace(/\\s+/g, ' ').trim(), label: k.getAttribute('aria-label'), tag: k.tagName, cls: k.className,
    bars: [...k.querySelectorAll('.k-spark i')].map((i) => ({ h: i.style.height, today: i.classList.contains('today') })),
    sameRow: Math.abs(r.top - rw.top) < 2, wideRight: Math.abs(rw.right - g.right) < 14, leftEdge: Math.abs(r.left - g.left) < 14 }; }"""
PANEL_JS = """() => { const d = document.getElementById('pomo'), r = d.getBoundingClientRect();
  return { open: d.open, title: document.getElementById('pomo-title').innerText.trim(), text: d.innerText.replace(/\\s+/g, ' '),
    rows: [...d.querySelectorAll('.pm-list li')].map((li) => li.querySelector('.pm-code').textContent + '×' + li.querySelector('.pm-cnt').textContent.slice(1)),
    days: d.querySelectorAll('.pm-day').length, today: !!d.querySelector('.pm-day:last-child.today'), width: r.width, vw: innerWidth,
    scrollW: document.scrollingElement.scrollWidth }; }"""
FAKE_TMUX = "#!/usr/bin/env bash\nexit 0\n"


def claude_stub(pomo):
    return """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const now = Date.now();
  const data = { sessions: [
    { code: 'API', state: 'working', title: 'Paginate', remote_url: null, pending: null, updated_at: now - 60000 },
    { code: 'WEB', state: 'done', title: 'Settings', remote_url: null, pending: null, updated_at: now - 90000 } ], decisions: [] };
  const query = (path) => { const q = { orderBy: () => q, limit: () => q,
    onSnapshot(cb) { setTimeout(() => cb(snap((data[path] || []).map((d) => ({ id: d.id || d.code, exists: true, data: () => d })))), 0); return () => {}; } }; return q; };
  const docs = { 'meta/operator': { heartbeat_at: now, approver_ids: ['u_x'] }, 'meta/pomodoro': %s };
  const doc = (path) => ({ onSnapshot(cb) { const d = docs[path] || null; setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; },
    set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();""" % json.dumps(pomo)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def wait_for(pg, js, timeout_ms=15000):
    t = 0
    while t < timeout_ms:
        if pg.evaluate(js):
            return True
        pg.wait_for_timeout(200)
        t += 200
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-pomo-page.")
    srv = live = None
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
                for script in (BURST_SPY, init):
                    if script:
                        ctx.add_init_script(script)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("demo: session cards")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(600)
            rows = pg.evaluate(ROWS_JS)
            d = rows.get("DOCS") or {}
            check("a working session: ring, mm:ss of 25:00, today's count", d.get("ring") and d.get("live")
                  and re.search(r"\b1[67]:\d\d / 25:00\b", d.get("text", "")) and "🍅×5 today" in d.get("text", ""), d)
            check("  labelled for screen readers", d.get("label", "").startswith("DOCS · Pomodoro 1") and "of 25:00" in d.get("label", "")
                  and "5 🍅 today" in d.get("label", ""), d.get("label"))
            first = d.get("text")
            pg.wait_for_timeout(1300)
            d2 = pg.evaluate(ROWS_JS).get("DOCS") or {}
            check("  the clock ticks without a re-render", d2.get("text") != first and d2.get("dash") != d.get("dash"), (first, d2.get("text")))
            a = rows.get("API") or {}
            check("waiting for you: paused, progress kept", a.get("ring") and not a.get("live") and "9:00 / 25:00" in a.get("text", "")
                  and "(paused)" in a.get("label", ""), a)
            q = rows.get("QA") or {}
            check("no current pomodoro: the count only", q and not q.get("ring") and q.get("text") == "🍅×3 today", q)
            check("nothing today and no progress: no row", "DATA" not in rows and "MOBILE" not in rows, sorted(rows))
            check("the teammates' combined count and focus on their lead's card", re.search(r"teammates \+5 🍅 · 2h 2\dm", d.get("text", ""))
                  and "2 teammates: 5 🍅, 2h 2" in d.get("label", ""), d)
            check("  a teammate stream has no card row of its own", not any("/" in c for c in rows), sorted(rows))
            check("  no teammates, no figure", "teammates" not in a.get("text", "") and "teammates" not in q.get("text", ""), (a, q))

            print("demo: the Deck key and its panel")
            k = pg.evaluate(KEY_JS)
            check("🍅 Today key: fleet total and focus", k and k["tag"] == "BUTTON" and k["text"].upper().startswith("🍅 TODAY") and " 19 " in " " + k["text"] + " "
                  and "9h 28m focus" in k["text"], k)   # 14 by the sessions + 5 by DOCS's teammates
            check("  aria-label", k and k["label"] == "AI fleet: 19 🍅 · 9h 28m focus. Press for the pomodoro tally.", k and k["label"])
            check("  7-day sparkline, today highlighted", k and len(k["bars"]) == 7 and k["bars"][-1]["today"] and not k["bars"][0]["today"], k and k["bars"])
            check("  shares the last row with the wide strip", k and k["sameRow"] and k["wideRight"] and k["leftEdge"], k)
            pg.click("#deck-grid .dkey.pomo")
            pg.wait_for_timeout(300)
            pn = pg.evaluate(PANEL_JS)
            check("pressing it opens the panel", pn["open"] and "Pomodoro" in pn["title"], pn["title"])
            check("  fleet total and focus", "AI fleet: 19 🍅 · 9h 28m focus" in pn["text"], pn["text"][:200])
            check("  the sessions / teammates breakdown", "Sessions 14 🍅 · 7h 3m focus + teammates 5 🍅 · 2h 2" in pn["text"], pn["text"][:300])
            check("  per-session counts today, most first, teammates under their session",
                  pn["rows"][:4] == ["DOCS×5", "checker×2", "writer×3", "API×3"] and len(pn["rows"]) == 8, pn["rows"])
            check("  14 days, today last", pn["days"] == 14 and pn["today"], (pn["days"], pn["today"]))
            check("  the rules are explained", "Every session and every teammate runs its own pomodoro. Each 25 minutes" in pn["text"]
                  and "a break never resets it" in pn["text"], pn["text"][-260:])
            pg.click("#pomo-close")
            check("  Close closes it", not pg.evaluate("() => document.getElementById('pomo').open"))

            print("demo: a pomodoro completes (OPS is 9 s from the end)")
            ok = wait_for(pg, "() => (document.querySelector('.spomo[data-pomo=\"OPS\"]') || {}).innerText?.includes('🍅×3')", 15000)
            check("the count goes up when the clock reaches 25:00", ok, pg.evaluate(ROWS_JS).get("OPS"))
            check("  a celebration on the card and the Deck key", pg.evaluate("() => window.__bursts") >= 2, pg.evaluate("() => window.__bursts"))
            check("  fleet total follows", " 20 " in " " + pg.evaluate(KEY_JS)["text"] + " ", pg.evaluate(KEY_JS)["text"])
            pg.wait_for_timeout(1800)
            check("  the burst is gone afterwards", pg.evaluate("() => document.querySelectorAll('.pomo-burst, .pomo-pop').length") == 0)
            pg.close()

            print("reduced motion")
            pg = new_page(reduced_motion="reduce")
            pg.goto(PAGE + "?demo=1")
            ok = wait_for(pg, "() => (document.querySelector('.spomo[data-pomo=\"OPS\"]') || {}).innerText?.includes('🍅×3')", 15000)
            check("the count still goes up", ok)
            check("  no celebration", pg.evaluate("() => window.__bursts") == 0, pg.evaluate("() => window.__bursts"))
            pg.close()

            print("phone width")
            pg = new_page(viewport={"width": 390, "height": 860}, is_mobile=True, has_touch=True)
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(600)
            k = pg.evaluate(KEY_JS)
            check("the key and the wide strip share a row on three columns", k and k["sameRow"] and k["wideRight"] and k["leftEdge"], k)
            ov = pg.evaluate("""() => [...document.querySelectorAll('.spomo')].filter((e) => { const r = e.getBoundingClientRect(), c = e.closest('article').getBoundingClientRect();
              return r.right > c.right + 1 || r.left < c.left - 1; }).length""")
            check("  card rows stay inside their cards", ov == 0, ov)
            check("  no horizontal page scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            pg.click("#deck-grid .dkey.pomo")
            pg.wait_for_timeout(300)
            pn = pg.evaluate(PANEL_JS)
            check("  the panel fits the screen", pn["open"] and pn["width"] <= pn["vw"] and pn["scrollW"] <= pn["vw"], pn)
            hist = pg.evaluate("() => { const h = document.querySelector('.pm-hist'); return [h.scrollWidth, h.clientWidth]; }")
            check("  all 14 bars fit", hist[0] <= hist[1] + 1, hist)
            pg.close()

            print("zh-TW")
            pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(600)
            k = pg.evaluate(KEY_JS)
            check("key in Chinese", k and k["text"].startswith("🍅 今日") and "專注 9 小時 28 分" in k["text"] and k["label"].startswith("AI 團隊：19 🍅"), k)
            r = pg.evaluate(ROWS_JS)
            check("card rows in Chinese", "今日 🍅×5" in r["DOCS"]["text"] and "teammate +5 🍅" in r["DOCS"]["text"] and r["API"]["label"] == "API · 番茄鐘 9:00 / 25:00 (暫停中) · 今日 3 🍅", (r["DOCS"], r["API"]["label"]))
            pg.click("#deck-grid .dkey.pomo")
            pg.wait_for_timeout(300)
            pn = pg.evaluate(PANEL_JS)
            check("panel in Chinese", "番茄鐘" in pn["title"] and "今日各 session" in pn["text"].lower() and "最近 14 天" in pn["text"]
                  and "AI 團隊：19 🍅" in pn["text"] and "teammate 5 🍅" in pn["text"] and "每個 session 和每個 teammate 都有自己的番茄鐘" in pn["text"], pn["text"][:300])
            left = [w for w in ("sessions today", "last 14 days", "focus", "today is", "a pomodoro", "close") if w in pn["text"].lower()]
            check("  no English left in the panel", not left, left)
            pg.close()

            print("claude adapter (Artifact): meta/pomodoro")
            mirror = {"today": "2026-11-15", "tz": "", "focus_ms": 1500000, "fleet": {"done": 4, "focus_ms": 7200000},
                      "sessions": {"API": {"done": 3, "focus_ms": 5400000}, "WEB": {"done": 1, "focus_ms": 1800000}},
                      "days": [{"date": "2026-11-%02d" % (2 + i), "done": i, "focus_ms": i * 1500000, "sessions": {}} for i in range(14)], "updated_at": None}
            mirror["days"][-1].update(done=4, focus_ms=7200000)
            pg = new_page(claude_stub(mirror))
            pg.goto(PAGE)
            pg.wait_for_timeout(800)
            r = pg.evaluate(ROWS_JS)
            check("cards show the mirrored count, no clock", r.get("API", {}).get("text") == "🍅×3 today" and not r["API"]["ring"]
                  and r.get("WEB", {}).get("text") == "🍅×1 today", r)
            k = pg.evaluate(KEY_JS)
            check("the key shows the fleet total", k and " 4 " in " " + k["text"] + " " and "2h 0m focus" in k["text"], k)
            pg.click("#deck-grid .dkey.pomo")
            pg.wait_for_timeout(300)
            pn = pg.evaluate(PANEL_JS)
            check("  the panel says it is a mirror", "Mirrored by the operator" in pn["text"] and pn["rows"] == ["API×3", "WEB×1"], (pn["rows"], pn["text"][-120:]))
            pg.close()
            print("the clock stops when the collector's data is stale")
            now_ms = int(time.time() * 1000)
            for label, seen, want_live in (("fresh", now_ms - now_ms % 300000, True), ("stale", now_ms - 20 * 60000, False)):
                view = {"enabled": True, "focus_ms": 1500000, "grace_ms": 120000, "tz": "", "today": "2026-11-15", "day_start": now_ms - 3600000,
                        "seen": seen, "seen_step_ms": 300000, "fleet": {"done": 0, "focus_ms": 0},
                        "sessions": {"API": {"done": 0, "focus_ms": 0, "running": True, "state": "working", "started_at": now_ms - (600000 if want_live else 18 * 60000),
                                             "progress_ms": None, "done_at": None}},
                        "days": [{"date": "2026-11-15", "done": 0, "focus_ms": 0, "sessions": {}}]}
                pg = new_page(claude_stub(view))
                pg.goto(PAGE)
                pg.wait_for_timeout(800)
                a1 = pg.evaluate(ROWS_JS).get("API") or {}
                pg.wait_for_timeout(1300)
                a2 = pg.evaluate(ROWS_JS).get("API") or {}
                if want_live:
                    check("  fresh: the clock runs from started_at", a1.get("live") and re.search(r"\b10:0\d / 25:00", a1.get("text", "")) and a2.get("text") != a1.get("text"), (a1, a2))
                else:
                    check("  stale: the clock holds at the end of the last bucket, marked paused", not a1.get("live") and a1.get("text") == a2.get("text")
                          and a1.get("text", "").startswith("3:00 / 25:00") and "(paused)" in a1.get("label", ""), (a1, a2))
                pg.close()

            pg = new_page(claude_stub(None))
            pg.goto(PAGE)
            pg.wait_for_timeout(800)
            k = pg.evaluate(KEY_JS)
            check("no meta/pomodoro: the key says so", k and "—" in k["text"] and "no data yet" in k["text"], k)
            pg.click("#deck-grid .dkey.pomo")
            pg.wait_for_timeout(300)
            check("  and the panel points at board pomodoro", "meta/pomodoro" in pg.evaluate(PANEL_JS)["text"])
            check("  no card rows", pg.evaluate(ROWS_JS) == {})
            pg.close()

            print("board-serve end to end")
            home, cfg, sess, fbin = (os.path.join(tmp, x) for x in ("home", "config", "sessions", "bin"))
            for dd in (os.path.join(home, "state"), cfg, sess, fbin):
                os.makedirs(dd)
            port = free_port()
            write(os.path.join(cfg, "fleet.yaml"), "approvers: [local]\nserve:\n  enabled: true\n  port: %d\npomodoro:\n  timezone: UTC\n" % port)
            live = subprocess.Popen(["sleep", "600"])
            sid = "eeeeeeee-1111-2222-3333-444444444444"
            now = int(time.time() * 1000)
            write(os.path.join(sess, "%d.json" % live.pid), {"pid": live.pid, "sessionId": sid, "cwd": "/w", "tmux": "CP:@3.%42",
                                                             "name": "CP", "status": "busy", "startedAt": 1})
            write(os.path.join(home, "state", sid + ".json"), {"session_id": sid, "state": "working", "tmux_pane": "%42", "updated_at": now})
            today = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
            # CP is 4 s from the end of its third pomodoro (as the collector would have left it).
            write(os.path.join(home, "pomodoro.json"), {"version": 1, "at": now, "settings": {"focus_ms": 1500000, "grace_ms": 120000, "tz": "UTC"},
                                                        "sessions": {"CP": {"state": "working", "acc": 1500000 - 4000, "away_since": None, "done_at": now - 1496000}},
                                                        "days": {today: {"CP": {"done": 2, "focus_ms": 2 * 1500000 + 1496000}}}, "notified": {}})
            write(os.path.join(fbin, "tmux"), FAKE_TMUX)
            os.chmod(os.path.join(fbin, "tmux"), 0o755)
            proc = os.path.join(tmp, "proc")
            subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), proc], check=True)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, BOARD_PROC_ROOT=proc,
                       ALERT_DESKTOP="0", PATH=fbin + os.pathsep + os.environ.get("PATH", ""))
            for v in [x for x in env if x.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) and x != "ALERT_DESKTOP"] + ["TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"]:
                env.pop(v, None)

            def collect():
                subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], env=env, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            collect()
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            pg = new_page()
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(1000)
            r = pg.evaluate(ROWS_JS).get("CP") or {}
            check("the collector's pomodoro.json reaches the card", r.get("ring") and r.get("live") and "/ 25:00" in r.get("text", "")
                  and "🍅×2 today" in r.get("text", ""), r)
            k = pg.evaluate(KEY_JS)
            check("  and the Deck key", k and " 2 " in " " + k["text"] + " ", k)
            time.sleep(max(0, (now + 4500 - int(time.time() * 1000)) / 1000.0))
            collect()  # the run that crosses 25:00
            with open(os.path.join(home, "pomodoro.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            check("  the collector counted the third", doc["days"][today]["CP"]["done"] == 3, doc["days"])
            ok = wait_for(pg, "() => (document.querySelector('.spomo[data-pomo=\"CP\"]') || {}).innerText?.includes('🍅×3')", 10000)
            check("  the completion reaches the page over SSE", ok, pg.evaluate(ROWS_JS))
            check("  with a celebration", pg.evaluate("() => window.__bursts") >= 1, pg.evaluate("() => window.__bursts"))
            check("  the next pomodoro started", re.search(r"\b0:0\d / 25:00", (pg.evaluate(ROWS_JS).get("CP") or {}).get("text", "")), pg.evaluate(ROWS_JS).get("CP"))
            pg.close()

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        for x in (srv, live):
            if x:
                x.terminate()
                x.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
