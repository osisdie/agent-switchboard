#!/usr/bin/env python3
"""tests/test_page_i18n.py — optional headless check of the language toggle in artifact/board.html.

With the demo data, every surface is opened (banners, decision cards and a confirm row, tasks,
expanded session cards, resources, threads, consensus votes, approvals, the Notifications and
Info dialogs, the profile menu, the usage popover, a toast). Then every text node and every
title / placeholder / aria-label is checked:
  - in 繁體中文, none matches an English UI string of the dictionary (templates included);
  - in English, none matches a Chinese one;
  - data (questions, titles, session codes) stays as it is in both;
  - the choice persists (localStorage), sets <html lang>, and works with storage blocked.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_i18n.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PAGE_FILE = os.path.join(REPO, "artifact", "board.html")
PAGE = "file://" + PAGE_FILE + "?demo=1"
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
            print("       " + str(detail)[:600])


def dictionary():
    src = open(PAGE_FILE, encoding="utf-8").read()
    body = src[src.index("function ZH_TW() {"):]
    body = body[body.index("return {") + len("return "):body.index("\n    };") + 6]
    return json.loads(re.sub(r",\s*}$", "}", body.strip()))


def demo_strings():
    """Every string literal in enterDemo(): demo data, never translated (a header "Plan" there is
    data, not the stage name)."""
    src = open(PAGE_FILE, encoding="utf-8").read()
    body = src[src.index("function enterDemo() {"):]
    body = body[:body.index("\n  }\n")]
    return {m.replace("\\'", "'") for m in re.findall(r"'((?:[^'\\]|\\.)*)'", body)}


def template_re(s):
    """A dictionary string as a full-match regex: {name} matches any text."""
    parts = re.split(r"\{\w+\}", s.strip())
    return re.compile("^" + ".+?".join(re.escape(p) for p in parts) + "$", re.S)


# Every visible text node (trimmed) and the UI attributes, outside <script>/<style> and outside
# elements that state their own language (the language names in the switcher).
TEXTS_JS = """() => {
  const out = [], lang = document.documentElement.lang;
  const skip = (el) => !!el.closest('script, style, [lang]:not(html)') && el.closest('[lang]:not(html)')?.getAttribute('lang') !== lang;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const el = n.parentElement; if (!el || el.closest('script, style') || skip(el)) continue;
    const t = n.textContent.trim(); if (t) out.push(t);
  }
  for (const el of document.body.querySelectorAll('[title], [placeholder], [aria-label]')) {
    if (skip(el)) continue;
    for (const a of ['title', 'placeholder', 'aria-label']) { const v = el.getAttribute(a); if (v && v.trim()) out.push(v.trim()); }
  }
  return out;
}"""


def open_everything(pg):
    for b in pg.query_selector_all(".scard-toggle"):
        b.click()
    pg.evaluate("() => { const d = document.getElementById('dec-history'); if (d) { d.hidden = false; d.open = true; } }")
    pg.evaluate("() => document.querySelectorAll('.ccard details').forEach((d) => { d.open = true; })")
    pg.click('#dec-d-1-demo button:has-text("' + pg.evaluate("() => document.querySelector('#dec-d-1-demo .opts .btn').textContent") + '")')
    pg.hover("#usage")
    pg.wait_for_timeout(100)


def collect(pg):
    texts = pg.evaluate(TEXTS_JS)
    pg.click("#notif-btn")
    pg.wait_for_timeout(150)
    texts += pg.evaluate(TEXTS_JS)
    pg.keyboard.press("Escape")
    pg.click("#info-btn")
    pg.wait_for_timeout(150)
    texts += pg.evaluate(TEXTS_JS)
    pg.keyboard.press("Escape")
    pg.click("#acct-btn")
    pg.wait_for_timeout(150)
    texts += pg.evaluate(TEXTS_JS)
    pg.keyboard.press("Escape")
    # a toast: answer the WEB question with its first option
    pg.click("#dec-d-2-demo .opts .btn")
    pg.wait_for_timeout(150)
    texts += pg.evaluate("() => [...document.querySelectorAll('.toast')].map((t) => t.textContent.trim())")
    return texts


def main():
    zh = dictionary()
    en_res = [(k, template_re(k)) for k, v in zh.items() if k.strip() != v.strip() and re.search(r"[A-Za-z]{2}", k)]
    zh_res = [(v, template_re(v)) for k, v in zh.items() if k.strip() != v.strip() and re.search(r"[㐀-鿿]", v)]
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
            print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
            return 0
        errors = []

        def new_page(init=None):
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, timezone_id="UTC", locale="en-US")
            if init:
                ctx.add_init_script(init)
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=404, body=""))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
            return pg

        def set_lang(pg, value):
            pg.click("#acct-btn")
            pg.click('label:has(input[name="sb-lang"][value="%s"])' % value)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(200)

        print("dictionary: %d strings" % len(zh))
        check("every zh-TW value keeps the placeholders of its key",
              all(sorted(re.findall(r"\{\w+\}", k)) == sorted(re.findall(r"\{\w+\}", v)) for k, v in zh.items()))

        print("English (default):")
        pg = new_page()
        pg.goto(PAGE)
        pg.wait_for_timeout(500)
        check("<html lang=en> by default", pg.evaluate("() => document.documentElement.lang") == "en")
        open_everything(pg)
        texts = collect(pg)
        check("walked a real page", len(texts) > 300, len(texts))
        left = sorted({t for t in texts for v, rx in zh_res if rx.match(t)})
        check("no Chinese UI string in English", not left, left[:20])
        check("the language names show in their own language", pg.evaluate("() => document.querySelector('span[lang=\"zh-TW\"]').textContent") == "繁體中文")

        print("繁體中文:")
        set_lang(pg, "zh-TW")
        check("<html lang=zh-TW>, stored", pg.evaluate("() => [document.documentElement.lang, localStorage.getItem('switchboard.lang')]") == ["zh-TW", "zh-TW"])
        pg.reload()
        pg.wait_for_timeout(500)
        check("kept after a reload", pg.evaluate("() => document.documentElement.lang") == "zh-TW")
        open_everything(pg)
        texts = collect(pg)
        demo = demo_strings()
        left = sorted({t for t in texts if t not in demo for k, rx in en_res if rx.match(t)})
        check("no English UI string left in 繁體中文", not left, left[:30])
        check("headings and the header in Chinese", pg.inner_text("#h-decisions") == "決策" and pg.inner_text("#h-board") == "看板"
              and pg.inner_text("#needs-tile .t") == "待你處理", [pg.inner_text("#h-decisions"), pg.inner_text("#h-board")])
        page_text = "\n".join(texts)
        check("data stays as it is: questions, titles, codes",
              "Keep the old settings URL as a redirect, or remove it?" in page_text and "Paginate the orders endpoint" in page_text
              and "API" in texts, None)
        check("the product name stays", pg.inner_text("h1") == "Agent Switchboard")
        set_lang(pg, "en")
        check("back to English", pg.inner_text("#h-decisions") == "Decisions" and pg.evaluate("() => document.documentElement.lang") == "en")
        pg.close()

        print("storage blocked:")
        pg = new_page("Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });")
        pg.goto(PAGE)
        pg.wait_for_timeout(400)
        set_lang(pg, "zh-TW")
        check("the toggle still works", pg.inner_text("#h-decisions") == "決策")
        pg.close()

        check("no page errors", not errors, errors)
        browser.close()
    print()
    print("passed: %d  failed: %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
