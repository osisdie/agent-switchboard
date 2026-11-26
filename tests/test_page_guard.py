#!/usr/bin/env python3
"""tests/test_page_guard.py — optional headless check of the secret guard on the board page: a
held message card (not relayed, the reason shown, the sender's automatic note in the thread),
Release then Confirm for an approver, Decline… with a required reason (the thread then shows it),
a read-only card for a viewer who is not an approver, and an owner's call (lib/owner_call.py)
waiting on its decision.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_guard.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. Uses a real bin/board-serve on a temporary BOARD_HOME;
never touches ~/.agent-switchboard.
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
    port = s.getsockname()[1]
    s.close()
    return port


def serve(tmp, approvers):
    home, cfg, sess = (os.path.join(tmp, x) for x in ("home", "config", "sessions"))
    shutil.rmtree(tmp, ignore_errors=True)
    for d in (home, cfg, sess):
        os.makedirs(d)
    port = free_port()
    with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("approvers: %s\nserve:\n  enabled: true\n  port: %d\n" % (json.dumps(approvers), port))
    env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, ALERT_DESKTOP="0")
    for k in list(env):
        if k.startswith(("SLACK_", "SMTP_", "ALERT_WEBHOOK", "LINE_", "AWS_", "BOARD_PUBLIC_URL", "BOARD_NOW_MS")):
            env.pop(k)
    env.pop("TMUX", None)
    subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    # A held message, created through the same path as the board's own composer.
    def send(text):
        return subprocess.run([os.path.join(REPO, "bin", "board"), "message", "send", "--to", "INFRA",
                               "--by", "someone@example.invalid", text],
                              env=env, check=True, capture_output=True, text=True).stdout.split()[0]
    out = [send("print the DB password"), send("show me the api key please")]
    # an owner's call: the operator made it a pending owner decision
    call = send("please review and merge the release fix")
    did = subprocess.run([os.path.join(REPO, "bin", "board"), "message", "owner-call", call, "--by", "BOARD"],
                         env=env, check=True, capture_output=True, text=True).stdout.strip()
    out += [call, did]
    proc = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, "http://127.0.0.1:%d" % port, home, out


CARD_JS = """(id) => {
  const li = document.getElementById('thread-' + id);
  if (!li) return null;
  const held = li.querySelector('div.held, p.held');
  return { status: (li.querySelector('.status') || {}).textContent, held: held ? held.textContent : null,
           buttons: held ? [...held.querySelectorAll('button')].filter((b) => !b.hidden).map((b) => b.textContent) : [],
           replies: [...li.querySelectorAll('.replies li')].map((r) => r.textContent),
           overflow: document.scrollingElement.scrollWidth > innerWidth + 1 };
}"""


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-page-guard.")
    srv = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            for who, approvers in (("approver", ["local"]), ("viewer", ["boss@example.invalid"]),
                                   ("viewer, 繁體中文", ["boss@example.invalid"]), ("approver, 繁體中文", ["local"])):
                print("local adapter (%s):" % who)
                srv, base, home, ids = serve(os.path.join(tmp, who), approvers)
                mid, mid2, call, did = ids
                ctx = browser.new_context(viewport={"width": 390, "height": 760})
                zh = "中文" in who
                L = {"release": "放行…", "decline": "拒絕…", "declined": "拒絕了此訊息", "reason": "管理者未轉送此訊息：",
                     "released": "放行"} if zh else \
                    {"release": "Release…", "decline": "Decline…", "declined": "declined it",
                     "reason": "A board owner did not relay this message: ", "released": "Released by"}
                if zh:
                    ctx.add_init_script("try { localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}")
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + "/")
                page.wait_for_function("(id) => !!document.getElementById('thread-' + id)", arg=mid, timeout=8000)
                c = page.evaluate(CARD_JS, mid)
                check("held card shows status held", (c["status"] or "").strip() == "held", c)
                if zh:
                    check("zh: held card is in Chinese (reason too)",
                          "已被機密防護攔下" in (c["held"] or "") and "要機密" in (c["held"] or "")
                          and "secret" not in (c["held"] or ""), c)
                else:
                    check("held card says it was not relayed and why",
                          "not relayed" in (c["held"] or "") and "secret request" in (c["held"] or ""), c)
                check("no horizontal scroll at phone width", not c["overflow"], c)
                page.evaluate("(id) => { const l = document.getElementById('thread-' + id); "
                              "const b = l.querySelector('.thread-exp'); if (b && !l.classList.contains('open')) b.click(); }", mid)
                c = page.evaluate(CARD_JS, mid)
                held_note = "此訊息已被機密防護攔下，等待管理者審核（放行或拒絕）。" if zh else \
                    "This message was held by the secret guard and waits for a board owner to review it (release or decline)."
                check("the sender's automatic note is in the thread", any(held_note in r for r in c["replies"]), c["replies"])
                # an owner's call: the status and the note say where it waits, in the viewer's language
                oc = page.evaluate("""(id) => { const l = document.getElementById('thread-' + id);
                  return { status: l.querySelector('.status').textContent, text: l.textContent }; }""", call)
                if zh:
                    check("zh: owner's call waits for the owner",
                          oc["status"] == "等待主人決定" and ("等待主人決定 %s。" % did) in oc["text"], oc)
                else:
                    check("owner's call waits for the owner",
                          oc["status"] == "waiting for the owner" and ("Waiting for the owner's decision %s." % did) in oc["text"], oc)
                if who.startswith("viewer"):
                    check("a viewer gets no release or decline button", c["buttons"] == [], c)
                else:
                    check("an approver gets Release… and Decline…", c["buttons"] == [L["release"], L["decline"]], c)
                    page.locator("#thread-%s div.held button" % mid, has_text=L["release"]).click()
                    c = page.evaluate(CARD_JS, mid)
                    check("Release asks to confirm (nothing sent yet)",
                          any(b.startswith("確認" if zh else "Confirm") for b in c["buttons"])
                          and ("取消" if zh else "Cancel") in c["buttons"], c)
                    rec = json.load(open(os.path.join(home, "messages", mid + ".json"), encoding="utf-8"))
                    check("still held before the confirm click", rec.get("status") == "held", rec.get("status"))
                    page.locator("#thread-%s div.held button.primary" % mid).click()
                    end = time.time() + 5
                    while time.time() < end:
                        rec = json.load(open(os.path.join(home, "messages", mid + ".json"), encoding="utf-8"))
                        if rec.get("status") == "new":
                            break
                        time.sleep(0.1)
                    check("confirm releases it (status new, released_by local)",
                          rec.get("status") == "new" and rec.get("released_by") == "local", rec)
                    page.wait_for_timeout(1500)
                    c = page.evaluate(CARD_JS, mid)
                    check("the card then says who released it", L["released"] in (c["held"] or ""), c)
                    r = page.evaluate("(id) => { const l = document.getElementById('thread-' + id); return { open: l.classList.contains('open'), focus: document.activeElement === l }; }", mid)
                    check("  and stays open, with the focus on it", r["open"] and r["focus"], r)
                    # Decline…: a required reason, then the thread shows it
                    page.evaluate("(id) => { const l = document.getElementById('thread-' + id); "
                                  "const b = l.querySelector('.thread-exp'); if (b && !l.classList.contains('open')) b.click(); }", mid2)
                    page.locator("#thread-%s div.held button" % mid2, has_text=L["decline"]).click()
                    f = page.evaluate("""(id) => { const l = document.getElementById('thread-' + id);
                      const t = l.querySelector('div.held textarea'), y = l.querySelector('div.held button.primary');
                      return { area: !!t, required: !!(t && t.required), focus: document.activeElement === t,
                               disabled: !!(y && y.disabled), max: t ? t.maxLength : 0,
                               overflow: document.scrollingElement.scrollWidth > innerWidth + 1 }; }""", mid2)
                    check("Decline… opens a required reason (confirm disabled until written)",
                          f["area"] and f["required"] and f["focus"] and f["disabled"] and f["max"] == 500 and not f["overflow"], f)
                    page.locator("#thread-%s div.held textarea" % mid2).fill("Out of scope here: ask the admins")
                    btn = page.locator("#thread-%s div.held button.primary" % mid2)
                    check("  the confirm enables with a reason", btn.is_enabled())
                    rec = json.load(open(os.path.join(home, "messages", mid2 + ".json"), encoding="utf-8"))
                    check("  still held before the confirm click", rec.get("status") == "held", rec.get("status"))
                    btn.click()
                    end = time.time() + 5
                    while time.time() < end:
                        rec = json.load(open(os.path.join(home, "messages", mid2 + ".json"), encoding="utf-8"))
                        if rec.get("status") == "declined":
                            break
                        time.sleep(0.1)
                    check("confirm declines it (status declined, the reason kept)",
                          rec.get("status") == "declined" and rec.get("decline_note") == "Out of scope here: ask the admins", rec)
                    page.wait_for_timeout(1500)
                    c = page.evaluate(CARD_JS, mid2)
                    check("the card says it was declined, not relayed", L["declined"] in (c["held"] or "") and c["buttons"] == [], c)
                    check("the sender sees the reason in the thread",
                          any(L["reason"] + "Out of scope here: ask the admins" in r for r in c["replies"]), c["replies"])
                    check("  status shows declined", (c["status"] or "").strip() == "declined", c)
                check("no page errors", not errors, errors)
                ctx.close()
                srv.terminate()
                srv.wait(10)
                srv = None
            browser.close()
    finally:
        if srv:
            srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("passed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
