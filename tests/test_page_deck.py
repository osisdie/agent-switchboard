#!/usr/bin/env python3
"""tests/test_page_deck.py — optional headless check of the Deck panel in artifact/board.html:
the keys in all three adapters (demo, the claude.ai Artifact viewer, board-serve), the HITL key
(press scrolls to and focuses the longest wait, a second press within 5 s cycles, a later one
starts over), blink colours by what a session waits for, reduced motion, phone width with no
horizontal page scroll, zh-TW labels, collapse remembered in localStorage, and the approver-only
actions: hidden for non-approvers and on the Artifact page, behind a confirm step on board-serve
(acknowledge all, also by long press), and the alert wave when a session starts waiting.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_deck.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a temporary BOARD_HOME with a fake tmux; never touches
~/.agent-switchboard or ~/.claude.json.
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


KEYS_JS = """() => [...document.querySelectorAll('#deck-grid > .dcell')].map((c) => {
  const k = c.querySelector('.dkey'), cs = getComputedStyle(k);
  return { key: c.dataset.deck, tag: k.tagName, type: k.getAttribute('type'), label: k.getAttribute('aria-label') || '',
    text: k.innerText.replace(/\\s+/g, ' ').trim(), cls: k.className, kc: cs.getPropertyValue('--kc').trim(), anim: cs.animationName,
    sec: [...c.querySelectorAll('.dsec')].map((s) => ({ tag: s.tagName, label: s.getAttribute('aria-label'), href: s.getAttribute('href') })) };
})"""
ACTIVE_JS = """() => { const a = document.activeElement, card = a && a.closest('article');
  const r = card ? card.getBoundingClientRect() : null;
  return { id: a && a.id, card: card && card.id, cls: card && card.className, label: card && card.getAttribute('aria-label'),
    visible: !!r && r.top < innerHeight && r.bottom > 0, expanded: a && a.getAttribute('aria-expanded') }; }"""

CLAUDE_STUB = """(() => {
  const snap = (docs) => ({ docs, metadata: { fromCache: false } });
  const now = Date.now();
  const sessions = [
    { code: 'API', state: 'needs-you', title: 'Paginate', pending: { tool_name: 'Bash', summary: 'npm test' }, remote_url: 'https://claude.ai/code/demo-api', updated_at: now - 120000 },
    { code: 'WEB', state: 'working', title: 'Settings page', remote_url: null, pending: null, updated_at: now - 60000 } ];
  const decisions = [{ id: 'd-1-stub', kind: 'permission', from: 'API', tool_name: 'Bash', summary: 'npm test', status: 'pending', created_at: now - 120000 }];
  const data = { sessions, decisions };
  const query = (path) => { const q = { orderBy: () => q, limit: () => q,
    onSnapshot(cb) { setTimeout(() => cb(snap((data[path] || []).map((d) => ({ id: d.id || d.code, exists: true, data: () => d })))), 0); return () => {}; } }; return q; };
  const docs = { 'meta/operator': { heartbeat_at: now, approver_ids: ['u_x'], usage: { five_hour: { pct: 42, resets_at: now + 3600000 }, seven_day: { pct: 12, resets_at: now + 86400000 }, updated_at: now } },
    'meta/notify': { mutes: { all: { until: now + 3600000, by: 'board:x', include_urgent: false } } },
    'meta/away': { active: true, since: now - 600000, by: 'OPS', said: 'out' } };
  const doc = (path) => ({ onSnapshot(cb) { const d = docs[path] || null; setTimeout(() => cb({ exists: !!d, data: () => d }), 0); return () => {}; },
    set: async () => {}, update: async () => {} });
  const user = { id: async () => 'u_x', canEdit: async () => true, isOwner: async () => true, can: async () => null,
    me: async () => ({ id: 'u_x', name: '', avatarUrl: '', color: '#777', email: null, isOwner: true, canEdit: true }) };
  window.claude = { use: (n) => Promise.resolve(n === 'user' ? user : n === 'db' ? { collection: query, doc } : null) };
})();"""

FAKE_TMUX = "#!/usr/bin/env bash\nexit 0\n"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh)


def by_key(keys):
    return {k["key"]: k for k in keys}


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-deck-test.")
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
                if init:
                    ctx.add_init_script(init)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                return pg

            print("demo adapter: the keys:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            keys = pg.evaluate(KEYS_JS)
            k = by_key(keys)
            check("5 x 3 keys and the wide strip, in order",
                  [x["key"] for x in keys] == ["hitl", "s1", "s2", "s3", "s4", "u-5h", "s5", "s6", "s7", "s8", "u-7d", "burn", "sys", "quiet", "away", "wide"],
                  [x["key"] for x in keys])
            check("every key is a real button with a label", all(x["tag"] == "BUTTON" and x["type"] == "button" and x["label"] for x in keys),
                  [(x["key"], x["tag"], x["label"][:30]) for x in keys if x["tag"] != "BUTTON" or not x["label"]])
            check("HITL: sessions waiting and the longest wait", k["hitl"]["text"].lower().startswith("waiting") and " 5 " in " " + k["hitl"]["text"] + " "
                  and "longest 25m" in k["hitl"]["text"], k["hitl"]["text"])
            check("S1-S8: code, state, time in state, title", "API" in k["s1"]["text"] and "permission · 4m" in k["s1"]["text"]
                  and "Paginate the orders endpoint" in k["s1"]["text"], k["s1"]["text"])
            check("  an offline session sits last", "MOBILE" in k["s8"]["text"] and "offline" in k["s8"]["text"], k["s8"]["text"])
            check("  the Remote Control link is the key's second action", k["s1"]["sec"] == [{"tag": "A", "label": "Open API in Remote Control", "href": "#demo-session-api"}]
                  and k["s4"]["sec"] == [], (k["s1"]["sec"], k["s4"]["sec"]))
            check("usage rings: 5h and 7d with reset countdowns", "91%" in k["u-5h"]["text"] and "↻ 1h 3" in k["u-5h"]["text"]
                  and "63%" in k["u-7d"]["text"] and "↻ 3d 4h" in k["u-7d"]["text"], (k["u-5h"]["text"], k["u-7d"]["text"]))
            check("  the 5h ring is orange at 91%", "lv-orange" in k["u-5h"]["cls"], k["u-5h"]["cls"])
            check("burn rate: will hit the limit", "⚠ 1h 29m" in k["burn"]["text"] and "to the 5h limit" in k["burn"]["text"] and "+6%/h" in k["burn"]["text"], k["burn"]["text"])
            check("system: level, mem / swap bars, OOM badge", "WARN" in k["sys"]["text"] and "OOM 1" in k["sys"]["text"]
                  and "88%" in k["sys"]["text"] and "31%" in k["sys"]["text"], k["sys"]["text"])
            check("quiet: the mute state", "1 muted" in k["quiet"]["text"], k["quiet"]["text"])
            check("away: the state", "Away" in k["away"]["text"] and "since" in k["away"]["text"], k["away"]["text"])
            check("wide strip: four sessions with pills, both bars, the burn rate",
                  k["wide"]["text"].count(" · ") >= 4 and "+4 more" in k["wide"]["text"] and "5h" in k["wide"]["text"] and "7d" in k["wide"]["text"]
                  and "⚠ 1h 29m to limit" in k["wide"]["text"], k["wide"]["text"])
            check("demo viewer is an approver: ack, mute and away actions", [s["label"] for s in k["hitl"]["sec"]] == ["Acknowledge all open alerts"]
                  and [s["label"] for s in k["quiet"]["sec"]] == ["Mute all push for 1 hour"] and [s["label"] for s in k["away"]["sec"]] == ["Turn owner-away off"],
                  (k["hitl"]["sec"], k["quiet"]["sec"], k["away"]["sec"]))

            print("blink colours by kind:")
            tok = pg.evaluate("() => { const cs = getComputedStyle(document.documentElement); return ['--dk-q', '--dk-plan', '--dk-perm'].map((v) => cs.getPropertyValue(v).trim()); }")
            check("S1 API waits on a permission: red", "w-permission" in k["s1"]["cls"] and k["s1"]["kc"] == tok[2], (k["s1"]["cls"], k["s1"]["kc"]))
            check("S2 WEB waits on a question: purple", "w-question" in k["s2"]["cls"] and k["s2"]["kc"] == tok[0], (k["s2"]["cls"], k["s2"]["kc"]))
            check("S3 CP waits on a plan: orange", "w-plan" in k["s3"]["cls"] and k["s3"]["kc"] == tok[1], (k["s3"]["cls"], k["s3"]["kc"]))
            check("  three different colours", len(set(tok)) == 3, tok)
            check("  waiting keys blink", all(k[x]["anim"] == "dk-blink" for x in ("s1", "s2", "s3", "hitl")), [k[x]["anim"] for x in ("s1", "s2", "s3", "hitl")])
            check("  a working key does not", k["s4"]["anim"] == "none" and "waiting" not in k["s4"]["cls"], k["s4"])

            print("HITL press: jump to the longest wait, cycle within 5 s:")
            pg.focus("#deck-grid .dkey.hitl")
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(700)
            a = pg.evaluate(ACTIVE_JS)
            check("Enter scrolls to the longest-waiting decision and focuses its answer field",
                  a["card"] == "dec-d-7-demo" and a["id"] == "ans-d-7-demo" and a["visible"] and "linked" in a["cls"], a)
            pg.click("#deck-grid .dkey.hitl")
            pg.wait_for_timeout(700)
            a = pg.evaluate(ACTIVE_JS)
            check("a second press within 5 s: the next one", a["card"] == "dec-d-6-demo" and a["visible"], a)
            pg.click("#deck-grid .dkey.hitl")
            pg.wait_for_timeout(700)
            check("  and the next", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-2-demo")
            pg.wait_for_timeout(5200)
            pg.click("#deck-grid .dkey.hitl")
            pg.wait_for_timeout(700)
            check("after 5 s it starts over at the longest wait", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-7-demo")
            pg.click("#deck-grid [data-deck=s4] .dkey")
            pg.wait_for_timeout(700)
            a = pg.evaluate(ACTIVE_JS)
            check("a session key without a decision opens its card", a["label"] == "Session DOCS" and a["expanded"] == "true" and a["visible"], a)
            pg.click("#deck-grid [data-deck=s1] .dkey")
            pg.wait_for_timeout(700)
            check("a waiting session key opens the decision of the prompt it shows", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-1-demo")
            pg.click("#deck-grid [data-deck=s2] .dkey")
            pg.wait_for_timeout(700)
            check("  else its oldest pending decision", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-2-demo")

            print("confirm step (demo):")
            pg.click("#deck-grid [data-deck=hitl] .dsec")
            pg.wait_for_timeout(150)
            c = pg.evaluate("() => { const b = document.getElementById('deck-confirm'); return { shown: !b.hidden, text: b.innerText, focus: document.activeElement.id }; }")
            check("✓✓ asks first; focus on Cancel", c["shown"] and "Acknowledge all open alerts?" in c["text"] and c["focus"] == "deck-confirm-no", c)
            pg.click("#deck-confirm-no")
            pg.wait_for_timeout(100)
            check("Cancel closes it", pg.evaluate("() => document.getElementById('deck-confirm').hidden"))
            pg.click("#deck-grid [data-deck=quiet] .dsec")
            pg.click("#deck-confirm-yes")
            pg.wait_for_timeout(200)
            k = by_key(pg.evaluate(KEYS_JS))
            check("mute all 1h after the confirm (demo: on the page only)", "All muted" in k["quiet"]["text"]
                  and [s["label"] for s in k["quiet"]["sec"]] == ["Unmute all push"], k["quiet"])
            pg.click("#deck-grid [data-deck=quiet] .dkey")
            pg.wait_for_timeout(150)
            check("the quiet key opens the Notifications dialog", pg.evaluate("() => document.getElementById('notif').open"))
            pg.keyboard.press("Escape")

            print("collapse, remembered:")
            pg.click("#deck-toggle")
            pg.wait_for_timeout(100)
            col = pg.evaluate("() => [document.getElementById('deck').classList.contains('collapsed'), document.getElementById('deck-toggle').getAttribute('aria-expanded'), localStorage.getItem('switchboard.deck'), getComputedStyle(document.getElementById('deck-body')).display]")
            check("hide: collapsed, aria-expanded false, stored", col == [True, "false", "collapsed", "none"], col)
            pg.reload()
            pg.wait_for_timeout(500)
            check("still collapsed after a reload", pg.evaluate("() => document.getElementById('deck').classList.contains('collapsed')"))
            pg.click("#acct-btn")
            check("the account menu shows the setting", pg.evaluate("() => document.querySelector('input[name=sb-deck][value=collapsed]').checked"))
            pg.click("input[name=sb-deck][value=shown]", force=True)
            pg.wait_for_timeout(100)
            check("  and shows the deck again", pg.evaluate("() => !document.getElementById('deck').classList.contains('collapsed') && localStorage.getItem('switchboard.deck') === null"))
            pg.close()

            print("reduced motion:")
            pg = new_page(reduced_motion="reduce")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            k = by_key(pg.evaluate(KEYS_JS))
            check("waiting keys keep their colour but do not blink", k["s1"]["anim"] == "none" and "waiting" in k["s1"]["cls"] and k["s1"]["kc"], k["s1"])
            pg.close()

            print("phone width:")
            pg = new_page(viewport={"width": 390, "height": 844}, has_touch=True)
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            ph = pg.evaluate("""() => { const g = document.getElementById('deck-grid');
              const cells = [...g.children].filter((c) => c.classList.contains('dcell')).map((c) => c.getBoundingClientRect());
              return { cols: getComputedStyle(g).gridTemplateColumns.split(' ').length, scrollW: document.scrollingElement.scrollWidth, vw: innerWidth,
                inside: cells.every((r) => r.left >= 0 && r.right <= innerWidth), minH: Math.min(...cells.map((r) => r.height)) }; }""")
            check("three columns", ph["cols"] == 3, ph)
            check("no horizontal page scroll", ph["scrollW"] <= ph["vw"], ph)
            check("every key inside the screen, big enough to tap", ph["inside"] and ph["minH"] >= 44, ph)
            pg.tap("#deck-grid .dkey.hitl")
            pg.wait_for_timeout(700)
            check("a tap on HITL jumps too", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-7-demo")
            pg.close()

            print("zh-TW:")
            pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(500)
            k = by_key(pg.evaluate(KEYS_JS))
            z = pg.evaluate("() => [document.getElementById('h-deck').textContent, document.getElementById('deck-toggle').textContent.trim(), document.getElementById('deck-grid').getAttribute('aria-label')]")
            check("heading, toggle and group label", z == ["按鍵面板", "收起面板", "面板按鍵"], z)
            check("HITL label and text", k["hitl"]["text"].startswith("等待中") and "最久" in k["hitl"]["text"] and "個在等" in k["hitl"]["label"], k["hitl"])
            check("kinds, burn, quiet, owner", "權限" in k["s1"]["text"] and "問題" in k["s2"]["text"] and "計畫" in k["s3"]["text"]
                  and "後到 5h 上限" in k["burn"]["text"] and "勿擾" in k["quiet"]["text"] and "主人" in k["away"]["text"],
                  [k[x]["text"] for x in ("s1", "s2", "s3", "burn", "quiet", "away")])
            check("second actions in Chinese", k["hitl"]["sec"][0]["label"] == "確認所有未處理的警示" and k["away"]["sec"][0]["label"] == "關閉主人不在模式",
                  (k["hitl"]["sec"], k["away"]["sec"]))
            pg.close()

            print("claude adapter (Artifact): read-only deck:")
            pg = new_page(CLAUDE_STUB)
            pg.goto(PAGE)
            pg.wait_for_timeout(700)
            k = by_key(pg.evaluate(KEYS_JS))
            check("keys render from the db", len(k) == 16 and "API" in k["s1"]["text"] and "WEB" in k["s2"]["text"] and "42%" in k["u-5h"]["text"], [x["text"][:20] for x in k.values()])
            check("  HITL counts the pending permission", " 1 " in " " + k["hitl"]["text"] + " " and "w-permission" in k["hitl"]["cls"], k["hitl"])
            check("  quiet and away from meta/notify and meta/away", "All muted" in k["quiet"]["text"] and "Away" in k["away"]["text"], (k["quiet"]["text"], k["away"]["text"]))
            check("  no burn rate on the Artifact page", "no burn data" in k["burn"]["text"], k["burn"]["text"])
            check("  no ack, mute or away actions (even for a listed approver)", not pg.query_selector("#deck-grid button.dsec"))
            check("  the Remote Control link stays", k["s1"]["sec"] == [{"tag": "A", "label": "Open API in Remote Control", "href": "https://claude.ai/code/demo-api"}], k["s1"]["sec"])
            pg.click("#deck-grid .dkey.hitl")
            pg.wait_for_timeout(700)
            check("  HITL still jumps", pg.evaluate(ACTIVE_JS)["card"] == "dec-d-1-stub")
            pg.close()

            print("board-serve (local adapter):")
            home, cfg, sess, fbin = (os.path.join(tmp, x) for x in ("home", "config", "sessions", "bin"))
            for d in (os.path.join(home, "state"), cfg, sess, fbin):
                os.makedirs(d)
            port = free_port()

            def fleet(approvers):
                write(os.path.join(cfg, "fleet.yaml"), "approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (approvers, port))

            fleet("[local]")
            live = subprocess.Popen(["sleep", "600"])
            sid = "dddddddd-1111-2222-3333-444444444444"
            now = int(time.time() * 1000)
            write(os.path.join(sess, "%d.json" % live.pid), {"pid": live.pid, "sessionId": sid, "cwd": "/w", "tmux": "CP:@3.%42",
                                                             "name": "CP", "status": "busy", "startedAt": 1})
            state_file = os.path.join(home, "state", sid + ".json")
            write(state_file, {"session_id": sid, "state": "working", "tmux_pane": "%42", "updated_at": now})
            write(os.path.join(home, "usage.json"), {"five_hour": {"pct": 30, "resets_at": now + 3 * 3600 * 1000},
                                                     "seven_day": {"pct": 10, "resets_at": now + 86400 * 1000}, "updated_at": now})
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
            base = "http://127.0.0.1:%d/" % port
            pg = new_page()
            pg.goto(base)
            pg.wait_for_timeout(1000)
            k = by_key(pg.evaluate(KEYS_JS))
            check("keys render from /api/state", "CP" in k["s1"]["text"] and "working" in k["s1"]["text"] and "30%" in k["u-5h"]["text"]
                  and "✓" in k["hitl"]["text"] and "all clear" in k["hitl"]["text"], (k["s1"]["text"], k["u-5h"]["text"], k["hitl"]["text"]))
            check("  burn rate from the collector: too few samples yet", "pace …" in k["burn"]["text"], k["burn"]["text"])
            check("  an approver gets ack, mute and away", [s["label"] for s in k["hitl"]["sec"]] == ["Acknowledge all open alerts"]
                  and [s["label"] for s in k["quiet"]["sec"]] == ["Mute all push for 1 hour"] and [s["label"] for s in k["away"]["sec"]] == ["Turn owner-away on"],
                  (k["hitl"]["sec"], k["quiet"]["sec"], k["away"]["sec"]))
            # A session starts waiting: the alert wave runs once from its key.
            write(state_file, {"session_id": sid, "state": "needs-you", "tmux_pane": "%42", "updated_at": int(time.time() * 1000),
                               "pending": {"tool_use_id": "tu_1", "tool_name": "ExitPlanMode", "summary": "plan", "summary_sha256": "x"}})
            collect()
            wave = None
            for _ in range(40):
                wave = pg.evaluate("() => { const w = document.querySelector('#deck-grid .dwave'); return w ? [w.dataset.from, w.className] : null; }")
                if wave:
                    break
                pg.wait_for_timeout(100)
            check("a session starts waiting: one alert wave from its key, in its colour", wave == ["s1", "dwave w-plan"], wave)
            pg.wait_for_timeout(2700)
            check("  the wave runs once and is gone", pg.evaluate("() => document.querySelectorAll('#deck-grid .dwave').length") == 0)
            k = by_key(pg.evaluate(KEYS_JS))
            check("  the key now blinks orange (plan)", "waiting" in k["s1"]["cls"] and "w-plan" in k["s1"]["cls"], k["s1"]["cls"])
            # acknowledge all: long press -> confirm -> POST /api/alerts/ack
            write(os.path.join(home, "alerts.json"), {"session:CP:needs-you": {"level": "warn", "first_at": now, "last_at": now, "acked_at": None},
                                                       "decision:d-1-x": {"level": "high", "first_at": now, "last_at": now, "acked_at": None}})
            box = pg.locator("#deck-grid .dkey.hitl").bounding_box()
            pg.mouse.move(box["x"] + 20, box["y"] + box["height"] - 20)
            pg.mouse.down()
            pg.wait_for_timeout(1000)
            pg.mouse.up()
            pg.wait_for_timeout(200)
            c = pg.evaluate("() => { const b = document.getElementById('deck-confirm'); return { shown: !b.hidden, text: b.innerText }; }")
            check("long press on HITL asks to acknowledge all (and does not jump)", c["shown"] and "Acknowledge all open alerts?" in c["text"]
                  and pg.evaluate("() => !document.activeElement.closest('article')"), c)
            with open(os.path.join(home, "alerts.json"), encoding="utf-8") as fh:
                check("  nothing acked before the confirm", all(e["acked_at"] is None for e in json.load(fh).values()))
            pg.click("#deck-confirm-yes")
            toast = None
            for _ in range(40):
                toast = pg.evaluate("() => [...document.querySelectorAll('.toast')].map((t) => t.textContent).join(' | ')")
                if "Acknowledged" in toast:
                    break
                pg.wait_for_timeout(100)
            check("  Yes acknowledges them through board ack", "Acknowledged 2 open alerts." in toast, toast)
            with open(os.path.join(home, "alerts.json"), encoding="utf-8") as fh:
                al = json.load(fh)
            check("  recorded as board:local", sorted(e.get("acked_by") for e in al.values()) == ["board:local", "board:local"], al)
            pg.close()

            fleet("[boss@example.com]")  # local is no longer an approver
            pg = new_page()
            pg.goto(base)
            pg.wait_for_timeout(1000)
            check("a non-approver sees the keys but no actions", pg.evaluate("() => document.querySelectorAll('#deck-grid .dkey').length") == 16
                  and not pg.query_selector("#deck-grid button.dsec"))
            pg.mouse.move(*(lambda b: (b["x"] + 20, b["y"] + 20))(pg.locator("#deck-grid .dkey.hitl").bounding_box()))
            pg.mouse.down()
            pg.wait_for_timeout(1000)
            pg.mouse.up()
            check("  a long press asks nothing", pg.evaluate("() => document.getElementById('deck-confirm').hidden"))
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
