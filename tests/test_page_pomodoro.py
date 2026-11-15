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
    rows: [...d.querySelectorAll('.pm-list li:not(.pm-more)')].map((li) => (li.classList.contains('pm-sub') ? '+' : '') + li.querySelector('.pm-code').textContent + '×' + li.querySelector('.pm-cnt').textContent.slice(1)),
    toggles: [...d.querySelectorAll('.pm-tgl')].map((b) => b.dataset.tm + ':' + b.getAttribute('aria-expanded') + ':' + b.textContent),
    days: d.querySelectorAll('.pm-day').length, today: !!d.querySelector('.pm-day:last-child.today'), width: r.width, vw: innerWidth,
    scrollW: document.scrollingElement.scrollWidth }; }"""
NAMES_JS = """() => [...document.querySelectorAll('#pomo .pm-list .pm-code')].map((e) => ({ text: e.textContent, title: e.title,
  clip: e.classList.contains('clip'), tab: e.getAttribute('tabindex'), cut: e.scrollWidth > e.clientWidth + 1 }))"""
SUM_JS = """() => { const s = document.querySelector('#pomo .pm-sum'); if (!s) return null; const c = s.querySelector('.pm-champ');
  return { text: s.innerText.replace(/\\s+/g, ' ').trim(), champ: c && c.dataset.champ, runner: (s.querySelector('[data-runner]') || { dataset: {} }).dataset.runner,
    lead: (s.querySelector('[data-lead]') || { dataset: {} }).dataset.lead, none: !!s.querySelector('.pm-none') }; }"""
POP_JS = """(id) => { const p = document.getElementById(id); return { shown: !p.hidden && p.getBoundingClientRect().width > 0, text: p.innerText.replace(/\\s+/g, ' ').trim(),
  right: p.getBoundingClientRect().right, vw: document.documentElement.clientWidth }; }"""
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


# Demo pages run on a paused page clock (Playwright's fake timers): page time moves only when
# the test calls run_for, so a loaded machine changes how long a check takes, never what it sees.
# Midday, UTC: far from a day boundary.
T0 = 1768478400.0  # 2026-01-15 12:00:00 UTC, epoch seconds
SLOW_MS = 120000   # real-time budget for anything that waits on the browser or board-serve


def open_paused(pg, url, ready, at=T0):
    """Open url with the page clock paused at `at` (epoch seconds); step only the timers due now
    (the stub's setTimeout(0) snapshots) until `ready` holds. True once it does."""
    # install() starts the clock running: begin an hour early so pause_at(at) is always ahead of it.
    pg.clock.install(time=at - 3600)
    pg.clock.pause_at(at)
    pg.goto(url)
    for _ in range(200):
        if pg.evaluate(ready):
            return True
        pg.clock.run_for(16)
        pg.wait_for_timeout(50)
    return False


def has(pg, js):
    """Wait (real time, generous) until js is truthy; False on timeout."""
    try:
        pg.wait_for_function(js, timeout=SLOW_MS, polling=100)
        return True
    except Exception:  # noqa: BLE001 - playwright TimeoutError: report it as a failed check
        return False


CARDS_UP = "() => !!document.querySelector('.spomo[data-pomo]')"
KEY_UP = "() => !!document.querySelector('#deck-grid .dcell[data-deck=\"pomo\"] .dkey')"
OPS3 = "() => (document.querySelector('.spomo[data-pomo=\"OPS\"]') || {}).innerText?.includes('🍅×3')"


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
                ctx = browser.new_context(**dict({"viewport": {"width": 1280, "height": 900}, "timezone_id": "UTC"}, **kw))
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
            check("the demo renders its cards", open_paused(pg, PAGE + "?demo=1", CARDS_UP))
            rows = pg.evaluate(ROWS_JS)
            d = rows.get("DOCS") or {}
            check("a working session: ring, mm:ss of 25:00, today's count", d.get("ring") and d.get("live")
                  and re.search(r"\b1[67]:\d\d / 25:00\b", d.get("text", "")) and "🍅×5 today" in d.get("text", ""), d)
            check("  labelled for screen readers", d.get("label", "").startswith("DOCS · Pomodoro 1") and "of 25:00" in d.get("label", "")
                  and "5 🍅 today" in d.get("label", ""), d.get("label"))
            first = d.get("text")
            pg.clock.run_for(1300)
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
            pg.clock.run_for(300)
            pn = pg.evaluate(PANEL_JS)
            check("pressing it opens the panel", pn["open"] and "Pomodoro" in pn["title"], pn["title"])
            check("  fleet total and focus", "AI fleet: 19 🍅 · 9h 28m focus" in pn["text"], pn["text"][:200])
            check("  the sessions / teammates breakdown", "Sessions 14 🍅 · 7h 3m focus + teammates 5 🍅 · 2h 2" in pn["text"], pn["text"][:300])
            check("  one row per session, its teammates included, most first", pn["rows"] == ["DOCS×10", "API×3", "QA×3", "OPS×2", "WEB×1", "DATA×0"], pn["rows"])
            check("  the teammates folded under their session", pn["toggles"] == ["DOCS:false:▸ 2 teammates"], pn["toggles"])
            pg.click("#pomo .pm-tgl")
            pg.clock.run_for(100)
            pn2 = pg.evaluate(PANEL_JS)
            check("  a click unfolds them under it", pn2["rows"][:3] == ["DOCS×10", "+checker×2", "+writer×3"] and pn2["toggles"] == ["DOCS:true:▾ 2 teammates"]
                  and pg.evaluate("() => document.activeElement.classList.contains('pm-tgl')"), (pn2["rows"], pn2["toggles"]))
            check("  the chart before the list, the totals first", pg.evaluate("""() => { const b = document.getElementById('pomo-body');
              const y = (s) => b.querySelector(s).getBoundingClientRect().top; return y('.pm-head') < y('.pm-champ') && y('.pm-champ') < y('.pm-hist') && y('.pm-hist') < y('.pm-list'); }"""))
            check("  14 days, today last", pn["days"] == 14 and pn["today"], (pn["days"], pn["today"]))
            check("  the rules are explained", "Every session and every teammate runs its own pomodoro. Each 25 minutes" in pn["text"]
                  and "a break never resets it" in pn["text"], pn["text"][-260:])
            pg.click("#pomo-close")
            check("  Close closes it", not pg.evaluate("() => document.getElementById('pomo').open"))

            print("demo: a pomodoro completes (OPS is 9 s from the end)")
            pg.clock.run_for(10000)
            check("the count goes up when the clock reaches 25:00", pg.evaluate(OPS3), pg.evaluate(ROWS_JS).get("OPS"))
            check("  a celebration on the card and the Deck key", pg.evaluate("() => window.__bursts") >= 2, pg.evaluate("() => window.__bursts"))
            check("  fleet total follows", " 20 " in " " + pg.evaluate(KEY_JS)["text"] + " ", pg.evaluate(KEY_JS)["text"])
            pg.clock.run_for(1800)
            check("  the burst is gone afterwards", pg.evaluate("() => document.querySelectorAll('.pomo-burst, .pomo-pop').length") == 0)
            pg.close()

            print("reduced motion")
            pg = new_page(reduced_motion="reduce")
            check("reduced motion: the demo renders", open_paused(pg, PAGE + "?demo=1", CARDS_UP))
            pg.clock.run_for(10000)
            check("the count still goes up", pg.evaluate(OPS3))
            check("  no celebration", pg.evaluate("() => window.__bursts") == 0, pg.evaluate("() => window.__bursts"))
            pg.close()

            print("phone width")
            pg = new_page(viewport={"width": 390, "height": 860}, is_mobile=True, has_touch=True)
            check("phone: the demo renders", open_paused(pg, PAGE + "?demo=1", CARDS_UP))
            k = pg.evaluate(KEY_JS)
            check("the key and the wide strip share a row on three columns", k and k["sameRow"] and k["wideRight"] and k["leftEdge"], k)
            ov = pg.evaluate("""() => [...document.querySelectorAll('.spomo')].filter((e) => { const r = e.getBoundingClientRect(), c = e.closest('article').getBoundingClientRect();
              return r.right > c.right + 1 || r.left < c.left - 1; }).length""")
            check("  card rows stay inside their cards", ov == 0, ov)
            check("  no horizontal page scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
            pn = pg.evaluate(PANEL_JS)
            check("  the panel fits the screen", pn["open"] and pn["width"] <= pn["vw"] and pn["scrollW"] <= pn["vw"], pn)
            hist = pg.evaluate("() => { const h = document.querySelector('.pm-hist'); return [h.scrollWidth, h.clientWidth]; }")
            check("  all 14 bars fit", hist[0] <= hist[1] + 1, hist)
            pg.close()

            print("zh-TW")
            pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
            check("zh-TW: the demo renders", open_paused(pg, PAGE + "?demo=1", CARDS_UP))
            k = pg.evaluate(KEY_JS)
            check("key in Chinese", k and k["text"].startswith("🍅 今日") and "專注 9 小時 28 分" in k["text"] and k["label"].startswith("AI 團隊：19 🍅"), k)
            r = pg.evaluate(ROWS_JS)
            check("card rows in Chinese", "今日 🍅×5" in r["DOCS"]["text"] and "teammate +5 🍅" in r["DOCS"]["text"] and r["API"]["label"] == "API · 番茄鐘 9:00 / 25:00 (暫停中) · 今日 3 🍅", (r["DOCS"], r["API"]["label"]))
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
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
            check("mirror: the cards render", open_paused(pg, PAGE, CARDS_UP))
            r = pg.evaluate(ROWS_JS)
            check("cards show the mirrored count, no clock", r.get("API", {}).get("text") == "🍅×3 today" and not r["API"]["ring"]
                  and r.get("WEB", {}).get("text") == "🍅×1 today", r)
            k = pg.evaluate(KEY_JS)
            check("the key shows the fleet total", k and " 4 " in " " + k["text"] + " " and "2h 0m focus" in k["text"], k)
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
            pn = pg.evaluate(PANEL_JS)
            check("  the panel says it is a mirror", "Mirrored by the operator" in pn["text"] and pn["rows"] == ["API×3", "WEB×1"], (pn["rows"], pn["text"][-120:]))
            pg.close()
            print("panel: teammate names, the summary, the explainer")
            # Two teammates tie on 🍅 (3 each): the one with more focus is the champion, though it sorts
            # after the other by name. A long teammate name is clipped at phone width, never mangled.
            long_name = "a-very-long-teammate-name-for-the-panel"
            team = {"today": "2026-11-15", "tz": "", "focus_ms": 1500000, "mode": "accumulate", "fleet": {"done": 8, "focus_ms": 15000000},
                    "sessions": {"API": {"done": 1, "focus_ms": 1800000}, "API/perf-next": {"done": 3, "focus_ms": 4800000},
                                 "API/ux-actions": {"done": 3, "focus_ms": 5040000}, "API/" + long_name: {"done": 1, "focus_ms": 1600000},
                                 "WEB": {"done": 0, "focus_ms": 1760000}},
                    "days": [{"date": "2026-11-%02d" % (2 + i), "done": i, "focus_ms": 0, "sessions": {}} for i in range(14)], "updated_at": None}
            pg = new_page(claude_stub(team), viewport={"width": 390, "height": 860}, is_mobile=True, has_touch=True)
            check("phone: the cards render", open_paused(pg, PAGE, CARDS_UP))
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
            pg.locator("#pomo .pm-tgl").tap()
            pg.clock.run_for(100)
            names = pg.evaluate(NAMES_JS)
            by = {n["text"]: n for n in names}
            check("teammate rows: the whole name, under its session", [n["text"] for n in names] == ["API", long_name, "perf-next", "ux-actions", "WEB"], [n["text"] for n in names])
            check("  each name's title is the full stream name", by.get("perf-next", {}).get("title") == "API · perf-next" and by.get("API", {}).get("title") == "API"
                  and by.get(long_name, {}).get("title") == "API · " + long_name, names)
            ln = by.get(long_name, {})
            check("  a name cut short is marked and takes the focus", ln.get("cut") and ln.get("clip") and ln.get("tab") == "0", ln)
            check("  a name that fits is not", not by.get("perf-next", {}).get("clip") and by.get("perf-next", {}).get("tab") is None, by.get("perf-next"))
            pg.locator("#pomo .pm-code.clip").first.tap()
            check("  a tap shows all of it", pg.evaluate("() => { const e = document.activeElement; return e.classList.contains('pm-code') && e.scrollWidth <= e.clientWidth + 1; }"))
            check("  no horizontal page scroll", pg.evaluate("() => document.scrollingElement.scrollWidth <= innerWidth"))
            sm = pg.evaluate(SUM_JS)
            check("summary: streams and person-hours", sm and "5 streams today: 2 sessions + 3 teammates" in sm["text"]
                  and "≈ 4.2 person-hours of focus" in sm["text"], sm)
            check("  the champion: most 🍅, a tie broken by focus", sm and sm["champ"] == "API/ux-actions" and sm["runner"] == "API/perf-next"
                  and "🏆 Champion API · ux-actions · 3 🍅 · 1h 24m focus" in sm["text"] and "Runner-up API · perf-next · 3 🍅 · 1h 20m focus" in sm["text"], sm)
            pg.locator("#pomo-why").tap()
            w = pg.evaluate(POP_JS, "pomo-why-pop")
            check("the 🍅 in the panel header explains it (a tap)", w["shown"] and "25 minutes of focus" in w["text"] and "teammate active" in w["text"]
                  and "Mode accumulate" in w["text"] and "never resets" in w["text"], w)
            check("  inside a 390 px screen", w["right"] <= w["vw"], w)
            pg.close()

            pg = new_page(claude_stub(team))
            open_paused(pg, PAGE, CARDS_UP)
            key = pg.locator("#deck-grid .dkey.pomo")
            check("the Deck key is described by the explanation", key.get_attribute("aria-describedby") == "pomo-key-pop"
                  and "25 minutes of focus" in pg.evaluate(POP_JS, "pomo-key-pop")["text"])
            key.hover()
            check("  shown on hover", pg.evaluate(POP_JS, "pomo-key-pop")["shown"])
            pg.mouse.move(5, 5)
            check("  gone on leaving", not pg.evaluate(POP_JS, "pomo-key-pop")["shown"])
            key.focus()
            pg.keyboard.press("Shift+Tab")
            pg.keyboard.press("Tab")
            check("  shown on keyboard focus", pg.evaluate(POP_JS, "pomo-key-pop")["shown"])
            pg.keyboard.press("Escape")
            check("  Escape closes it", not pg.evaluate(POP_JS, "pomo-key-pop")["shown"])
            pg.keyboard.press("Enter")
            pg.clock.run_for(300)
            check("  the key still opens the panel, the explanation stays shut", pg.evaluate("() => document.getElementById('pomo').open")
                  and not pg.evaluate(POP_JS, "pomo-key-pop")["shown"] and not pg.evaluate(POP_JS, "pomo-why-pop")["shown"])
            pg.close()

            pg = new_page("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}" + claude_stub(team))
            open_paused(pg, PAGE, CARDS_UP)
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
            sm = pg.evaluate(SUM_JS)
            check("zh-TW: the summary", sm and "今日 5 個計時：2 個 session + 3 個 teammate" in sm["text"] and "≈ 相當於一個人專注 4.2 小時" in sm["text"]
                  and "🏆 冠軍 API · ux-actions · 3 🍅 · 專注 1 小時 24 分" in sm["text"] and "亞軍 API · perf-next" in sm["text"], sm)
            pg.hover("#pomo-why")
            w = pg.evaluate(POP_JS, "pomo-why-pop")
            check("  the explanation", w["shown"] and "專注 25 分鐘" in w["text"] and "teammate 正在活動" in w["text"] and "accumulate 模式" in w["text"], w)
            check("  the 🍅 button's name", pg.get_attribute("#pomo-why", "aria-label") == "什麼算一個番茄鐘")
            pg.close()

            print("panel: 160 streams (20 sessions x 7 teammates)")
            big = {"today": "2026-11-15", "tz": "", "focus_ms": 1500000, "mode": "accumulate", "sessions": {},
                   "days": [{"date": "2026-11-%02d" % (2 + i), "done": i, "focus_ms": 0, "sessions": {}} for i in range(14)], "updated_at": None}
            for i in range(20):
                lead, quiet = "S%02d" % i, i >= 15            # S15..S19: nothing today
                big["sessions"][lead] = {"done": 0 if quiet else i % 4, "focus_ms": 0 if quiet else (i % 4) * 1500000 + i * 60000}
                for j in range(7):
                    name = "helper-%d-%d" % (i, j) if j else "solo-%02d" % i
                    big["sessions"]["%s/%s" % (lead, name)] = {"done": 0 if quiet else (i + j) % 3, "focus_ms": 0 if quiet else ((i + j) % 3) * 1500000 + j * 30000}
            fleet_done = sum(r["done"] for r in big["sessions"].values())
            big["fleet"] = {"done": fleet_done, "focus_ms": sum(r["focus_ms"] for r in big["sessions"].values())}
            big["days"][-1].update(done=fleet_done)
            pg = new_page(claude_stub(big), viewport={"width": 390, "height": 860}, is_mobile=True, has_touch=True)
            check("  the page renders the data", open_paused(pg, PAGE, "() => !!document.querySelector('#deck-grid .dkey.pomo.on')"))
            t_open = time.perf_counter()   # real time: the paused page clock fakes performance.now too
            pg.evaluate("() => document.querySelector('#deck-grid .dkey.pomo').click()")
            ms = (time.perf_counter() - t_open) * 1000
            check("opens in reasonable time (%d ms)" % ms, ms < 1500, ms)
            pn = pg.evaluate(PANEL_JS)
            rows = [r for r in pn["rows"] if not r.startswith("+")]
            check("  the top 10 sessions, teammates folded", len(pn["rows"]) == 10 and len(rows) == 10 and len(pn["toggles"]) == 10, pn["rows"])
            check("  the totals are exact", "AI fleet: %d 🍅" % fleet_done in pn["text"], (fleet_done, pn["text"][:80]))
            check("  no horizontal scroll at 390 px", pn["scrollW"] <= pn["vw"] and pg.evaluate("() => { const d = document.getElementById('pomo'); return d.scrollWidth <= d.clientWidth; }"))
            check("  Show all (20), the quiet ones counted", pg.evaluate("() => document.querySelector('#pomo .pm-all').textContent") == "Show all (20)"
                  and "5 with nothing today are hidden." in pn["text"], pn["text"][-200:])
            pg.click("#pomo .pm-all")
            pn = pg.evaluate(PANEL_JS)
            counts = [int(r.split("×")[1]) for r in pn["rows"]]
            check("  Show all: every session, and their counts add up to the fleet", len(pn["rows"]) == 20 and sum(counts) == fleet_done
                  and pg.evaluate("() => document.querySelector('#pomo .pm-all').textContent") == "Show the top 10", (len(pn["rows"]), sum(counts), fleet_done))
            pg.click("#pomo .pm-all")
            q = pg.locator("#pomo-q")
            check("  a filter box (more than 10 rows)", q.count() == 1)
            q.fill("S07")
            pn = pg.evaluate(PANEL_JS)
            check("  filter by session code", [r.split("×")[0] for r in pn["rows"]] == ["S07"] and pg.evaluate("() => document.activeElement.id") == "pomo-q", pn["rows"])
            q.fill("helper-3-4")
            pn = pg.evaluate(PANEL_JS)
            check("  filter by teammate name: its session, unfolded to the match", [r.split("×")[0] for r in pn["rows"]] == ["S03", "+helper-3-4"], pn["rows"])
            q.fill("solo-1")
            pn = pg.evaluate(PANEL_JS)
            check("  several matches, quiet sessions included", sorted(r.split("×")[0] for r in pn["rows"] if not r.startswith("+")) == ["S%02d" % i for i in range(10, 20)], pn["rows"])
            q.fill("no-such-name")
            check("  nothing matches: said so", 'Nothing matches "no-such-name".' in pg.evaluate(PANEL_JS)["text"])
            q.fill("")
            check("  cleared: the top 10 again", len(pg.evaluate(PANEL_JS)["rows"]) == 10)
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
                check("  %s: the card renders" % label, open_paused(pg, PAGE, CARDS_UP, at=now_ms / 1000.0))
                a1 = pg.evaluate(ROWS_JS).get("API") or {}
                pg.clock.run_for(1300)
                a2 = pg.evaluate(ROWS_JS).get("API") or {}
                if want_live:
                    pg.click("#deck-grid .dkey.pomo")
                    pg.clock.run_for(300)
                    sm = pg.evaluate(SUM_JS)
                    check("  no pomodoro yet: said so, with the stream closest to its first", sm and sm["none"] and not sm["champ"] and sm["lead"] == "API"
                          and "No pomodoro yet today: the first one is on its way." in sm["text"] and re.search(r"Closest API · 10:0\d of 25:00", sm["text"]), sm)
                    pg.hover("#pomo-why")
                    check("  strict mode explained (no mode in the view)", "Mode strict: a break longer than 2m resets it." in pg.evaluate(POP_JS, "pomo-why-pop")["text"])
                    pg.click("#pomo-close")
                    check("  fresh: the clock runs from started_at", a1.get("live") and re.search(r"\b10:0\d / 25:00", a1.get("text", "")) and a2.get("text") != a1.get("text"), (a1, a2))
                else:
                    check("  stale: the clock holds at the end of the last bucket, marked paused", not a1.get("live") and a1.get("text") == a2.get("text")
                          and a1.get("text", "").startswith("3:00 / 25:00") and "(paused)" in a1.get("label", ""), (a1, a2))
                pg.close()

            pg = new_page(claude_stub(None))
            check("no meta/pomodoro: the key renders", open_paused(pg, PAGE, KEY_UP))
            k = pg.evaluate(KEY_JS)
            check("no meta/pomodoro: the key says so", k and "—" in k["text"] and "no data yet" in k["text"], k)
            pg.click("#deck-grid .dkey.pomo")
            pg.clock.run_for(300)
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
            # CP is 10 min from the end of its third pomodoro (as the collector would have left it):
            # no slow start-up can cross 25:00 before the first checks. The crossing is set up below.
            write(os.path.join(home, "pomodoro.json"), {"version": 1, "at": now, "settings": {"focus_ms": 1500000, "grace_ms": 120000, "tz": "UTC"},
                                                        "sessions": {"CP": {"state": "working", "acc": 900000, "away_since": None, "done_at": now - 900000}},
                                                        "days": {today: {"CP": {"done": 2, "focus_ms": 2 * 1500000 + 900000}}}, "notified": {}})
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
            for _ in range(600):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            pg = new_page()
            pg.goto("http://127.0.0.1:%d/" % port)
            has(pg, "() => !!document.querySelector('.spomo[data-pomo=\"CP\"] svg.pring')")
            r = pg.evaluate(ROWS_JS).get("CP") or {}
            check("the collector's pomodoro.json reaches the card", r.get("ring") and r.get("live") and "/ 25:00" in r.get("text", "")
                  and "🍅×2 today" in r.get("text", ""), r)
            k = pg.evaluate(KEY_JS)
            check("  and the Deck key", k and " 2 " in " " + k["text"] + " ", k)
            # Its last run was 2 s ago with 1 s to go: the next run crosses 25:00 whatever the load.
            with open(os.path.join(home, "pomodoro.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            t = int(time.time() * 1000)
            doc["at"] = doc["sessions"]["CP"]["seen_at"] = t - 2000
            doc["sessions"]["CP"]["acc"] = 1500000 - 1000
            doc["days"][today]["CP"]["focus_ms"] = 2 * 1500000 + 1500000 - 1000
            write(os.path.join(home, "pomodoro.json"), doc)
            collect()  # the run that crosses 25:00
            with open(os.path.join(home, "pomodoro.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            check("  the collector counted the third", doc["days"][today]["CP"]["done"] == 3, doc["days"])
            ok = has(pg, "() => (document.querySelector('.spomo[data-pomo=\"CP\"]') || {}).innerText?.includes('🍅×3')")
            check("  the completion reaches the page over SSE", ok, pg.evaluate(ROWS_JS))
            check("  with a celebration", pg.evaluate("() => window.__bursts") >= 1, pg.evaluate("() => window.__bursts"))
            check("  the next pomodoro started", re.search(r"\b0:[0-5]\d / 25:00", (pg.evaluate(ROWS_JS).get("CP") or {}).get("text", "")), pg.evaluate(ROWS_JS).get("CP"))
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
