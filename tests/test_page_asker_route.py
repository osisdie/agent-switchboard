#!/usr/bin/env python3
"""tests/test_page_asker_route.py — optional headless check of "who answers askers" on the board page
(lib/asker_route.py): the owner's control in the profile menu (local model by default; Claude
sessions while usage is below the 5-hour / 7-day levels; the levels saved), the owner's header pill,
a non-owner who never sees the control, and the asker's composer note following the effective route.
English and 繁體中文, light and dark, desktop and phone width.

Uses the desk stand-in of tests/test_page_desk.py (the desk is up, so "local" really means the desk).
Needs python playwright and a Chromium it can launch; without them it prints "skipped" and exits 0.

  python3 tests/test_page_asker_route.py [-v]
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import test_page_desk as pd  # noqa: E402  (exits 0 "skipped" without playwright)
from playwright.sync_api import sync_playwright  # noqa: E402

check = pd.check
L = {"en": {"legend": "Askers answered by", "default": "Now: the local model (the default).",
            "claude": "Now: Claude sessions answer askers.", "above": "5-hour 10%; it switches at 3% or lower",
            "away_label": "While you're away, askers' requests:", "away_now": "You are away now.",
            "away_note": "The owner is away; the sessions answer together.",
            "away_off": "The owner is away and not taking requests.", "took": "The owner is away; the sessions took it.",
            "pill": "Askers: Claude sessions", "note": "A Claude session answers askers"},
     "zh-TW": {"legend": "提問者由誰回答", "default": "目前：本機模型（預設）。",
               "claude": "目前：由 Claude session 回答提問者。", "above": "5 小時 10%；降到 3% 以下才會切換",
               "away_label": "你不在時，提問者的請求：", "away_now": "你現在不在。",
               "away_note": "主人不在；由各 session 一起回答。", "away_off": "主人不在，目前不接受詢問或派工。",
               "took": "主人不在；已交給各 session 處理。",
               "pill": "提問者：Claude session", "note": "目前由 Claude session 回答"}}

VIEW_JS = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const box = document.getElementById('acct-askroute');
  const t = (id) => vis(document.getElementById(id)) ? document.getElementById(id).textContent : null;
  const r = vis(box) ? box.getBoundingClientRect() : null;
  const inp = document.getElementById('ar-5h'), cs = getComputedStyle(inp);
  return { menu: vis(box), legend: vis(box) ? box.querySelector('legend').textContent : null,
           checked: (document.querySelector('input[name="sb-askroute"]:checked') || {}).value || null,
           levels: vis(document.getElementById('ar-th')), v5: inp.value, v7: document.getElementById('ar-7d').value,
           note: t('acct-ar-note'), pill: vis(document.getElementById('lm-pill')) ? document.getElementById('lm-pill-t').textContent : null,
           claudeNote: t('msg-claude'), localNote: t('msg-local'), box: r ? [r.left, r.right] : null,
           awayLabel: t('ar-away-label'), awayNote: t('acct-ar-away-note'), arNote: t('acct-ar-note'),
           awayChecked: (document.querySelector('input[name="sb-awayaskers"]:checked') || {}).value || null,
           composerAway: t('msg-away'), composerAwayOff: t('msg-away-off'), paused: t('msg-paused'),
           inColor: cs.color, inBg: cs.backgroundColor, vw: innerWidth, scrollW: document.scrollingElement.scrollWidth };
}"""


def usage(home, p5, p7):
    now = int(time.time() * 1000)
    pd.wj(os.path.join(home, "usage.json"), {"five_hour": {"pct": p5, "resets_at": now + 7200000},
                                             "seven_day": {"pct": p7, "resets_at": now + 172800000}, "updated_at": now})


def setting(home):
    try:
        with open(os.path.join(home, "asker-route.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def wait(fn, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def open_menu(page):
    if not page.evaluate("() => !document.getElementById('acct-panel').hidden"):
        page.click("#acct-btn")


def owner_checks(browser, base, home, lang, width, dark):
    T, tag = L[lang], "owner %s %d%s" % (lang, width, " dark" if dark else "")
    for n in ("asker-route.json", "asker-route-state.json"):
        try:
            os.unlink(os.path.join(home, n))
        except OSError:
            pass
    usage(home, 10, 10)
    ctx, page = pd.open_page(browser, base, lang, width)
    if dark:
        page.emulate_media(color_scheme="dark")
    page.wait_for_function("() => !document.getElementById('acct-pres').hidden", timeout=10000)
    open_menu(page)
    page.wait_for_function("() => !document.getElementById('acct-askroute').hidden", timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: the control is in the profile menu, local by default" % tag,
          v["menu"] and v["legend"] == T["legend"] and v["checked"] == "local" and not v["levels"]
          and (v["note"] or "").startswith(T["default"]), v)
    check("%s: the menu fits, no sideways scroll" % tag,
          v["box"] and v["box"][0] >= 0 and v["box"][1] <= v["vw"] and v["scrollW"] <= v["vw"], v)
    page.click("#acct-askroute label:has(input[value=claude_when_ample])")
    check("%s: Claude sessions is saved" % tag, wait(lambda: setting(home).get("mode") == "claude_when_ample"),
          setting(home))
    open_menu(page)
    page.wait_for_function("(t) => (document.getElementById('acct-ar-note').textContent || '').startsWith(t)",
                           arg=T["claude"], timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: levels shown with the defaults, Claude in effect, header pill" % tag,
          v["levels"] and v["v5"] == "50" and v["v7"] == "70" and v["checked"] == "claude_when_ample"
          and v["pill"] == T["pill"], v)
    check("%s: the level inputs are readable" % tag, v["inColor"] != v["inBg"], v)
    page.fill("#ar-5h", "8")
    page.click("#ar-save")
    check("%s: the levels are saved" % tag,
          wait(lambda: setting(home).get("five_hour_below") == 8 and setting(home).get("seven_day_below") == 70),
          setting(home))
    # 10% is not below 8%: back to the local model, said so
    open_menu(page)
    page.wait_for_function("(t) => (document.getElementById('acct-ar-note').textContent || '').includes(t)",
                           arg=T["above"], timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: above the level the local model answers" % tag, v["pill"] != T["pill"] and v["v5"] == "8", v)
    page.fill("#ar-5h", "0")
    page.click("#ar-save")
    time.sleep(0.5)
    check("%s: an invalid level is not sent" % tag, setting(home).get("five_hour_below") == 8, setting(home))
    open_menu(page)
    page.click("#acct-askroute label:has(input[value=local])")
    check("%s: back to the local model" % tag, wait(lambda: setting(home).get("mode") == "local"), setting(home))
    # while the owner is away: committee (default) / decline / wait
    pd.wj(os.path.join(home, "owner-away"), {"since": int(time.time() * 1000), "by": "board:local"})
    open_menu(page)
    page.wait_for_function("(t) => (document.getElementById('acct-ar-away-note').textContent || '').startsWith(t)",
                           arg=T["away_now"], timeout=10000)
    v = page.evaluate(VIEW_JS)
    check("%s: the away setting shows committee by default, and that the owner is away" % tag,
          v["awayLabel"] == T["away_label"] and v["awayChecked"] == "committee", v)
    page.click("#acct-askroute label:has(input[value=decline])")
    check("%s: decline while away is saved" % tag, wait(lambda: setting(home).get("away_askers") == "decline"),
          setting(home))
    open_menu(page)
    page.click("#acct-askroute label:has(input[value=committee])")
    check("%s: committee is saved again" % tag, wait(lambda: setting(home).get("away_askers") == "committee"),
          setting(home))
    os.unlink(os.path.join(home, "owner-away"))
    ctx.close()


def main():
    tmp = tempfile.mkdtemp(prefix="sb-page-askroute.")
    procs = []
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            print("owner:")
            procs, base, home, env = pd.serve(os.path.join(tmp, "owner"), "owner")
            for lang, width, dark in (("en", 1280, False), ("zh-TW", 390, True)):
                owner_checks(browser, base, home, lang, width, dark)
            pd.stop(procs)

            print("asker:")
            procs, base, home, env = pd.serve(os.path.join(tmp, "asker"), "asker")
            usage(home, 10, 10)
            pd.wj(os.path.join(home, "asker-route.json"), {"mode": "claude_when_ample", "five_hour_below": 50,
                                                           "seven_day_below": 70})
            for lang, width in (("en", 1280), ("zh-TW", 390)):
                ctx, page = pd.open_page(browser, base, lang, width)
                page.wait_for_function("() => !document.getElementById('msg-claude').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("asker %s: the composer says a Claude session answers" % lang,
                      L[lang]["note"] in (v["claudeNote"] or "") and v["localNote"] is None
                      and v["scrollW"] <= v["vw"], v)
                page.click("#acct-btn")
                v = page.evaluate(VIEW_JS)
                check("asker %s: no route control, no owner pill" % lang, not v["menu"] and v["pill"] is None, v)
                ctx.close()
            pd.wj(os.path.join(home, "asker-route.json"), {"mode": "local"})
            ctx, page = pd.open_page(browser, base, "en", 1280)
            page.wait_for_function("() => !document.getElementById('msg-local').hidden", timeout=10000)
            v = page.evaluate(VIEW_JS)
            check("asker: local mode shows the local-model note", v["claudeNote"] is None and v["localNote"], v)
            ctx.close()
            # the owner is away: committee, then decline
            pd.wj(os.path.join(home, "owner-away"), {"since": int(time.time() * 1000), "by": "board:x"})
            now = int(time.time() * 1000) + 5000
            mid = "m-%d-away1" % now
            pd.wj(os.path.join(home, "messages", mid + ".json"), {
                "id": mid, "to": "API", "by": "local", "kind": "asker", "asker": "local", "created_at": now,
                "status": "new", "body": "@API please review !7", "away_route": "committee", "away_at": now})
            for lang, width in (("en", 1280), ("zh-TW", 390)):
                ctx, page = pd.open_page(browser, base, lang, width)
                page.wait_for_function("() => !document.getElementById('msg-away').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("asker %s: away + committee says the sessions answer together" % lang,
                      v["composerAway"] == L[lang]["away_note"] and v["composerAwayOff"] is None
                      and v["scrollW"] <= v["vw"], v)
                page.wait_for_selector("#thread-" + mid, timeout=10000)
                t = page.evaluate(pd.THREAD_JS, mid)
                check("asker %s: the thread says the sessions took it" % lang, t and L[lang]["took"] in (t["note"] or ""), t)
                ctx.close()
            pd.wj(os.path.join(home, "asker-route.json"), {"mode": "local", "away_askers": "decline"})
            for lang in ("en", "zh-TW"):
                ctx, page = pd.open_page(browser, base, lang, 1280)
                page.wait_for_function("() => !document.getElementById('msg-away-off').hidden", timeout=10000)
                v = page.evaluate(VIEW_JS)
                check("asker %s: away + decline says the owner isn't taking requests (one note)" % lang,
                      L[lang]["away_off"] in (v["composerAwayOff"] or "") and v["paused"] is None
                      and v["localNote"] is None and v["composerAway"] is None, v)
                ctx.close()
            pd.stop(procs)
            procs = []
            browser.close()
    finally:
        pd.stop(procs)
        pd.BEATING.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    print("passed: %d  failed: %d" % (pd.passed, pd.failed))
    return 1 if pd.failed else 0


if __name__ == "__main__":
    sys.exit(main())
