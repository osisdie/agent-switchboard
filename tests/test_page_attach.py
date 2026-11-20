#!/usr/bin/env python3
"""tests/test_page_attach.py — optional headless check of image attachments in the Messages
composer of artifact/board.html (board-serve: POST /api/uploads, `attachments` on POST /api/messages).

  - 📎 Attach image (a file picker), paste and drop add images; each shows as a chip "📎 name · size"
    with a ✕ (a button, keyboard reachable); never a preview (no <img>, no blob: URL);
  - only PNG, JPEG, GIF and WebP, at most 5 MB each and 4 per message, said under the text box;
  - Send carries the upload ids; the thread shows the same chips; the relay lists the paths;
  - no attach control in the demo or the Artifact (nowhere to store a file);
  - phone width in both themes: no side scroll; zh-TW strings.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and
exits 0, so it never blocks anyone. CI does not run it.

  python3 tests/test_page_attach.py [-v]
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

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


def png():
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + b"\x00" * 20 + b"\xff\xd9"
FAKE_TMUX = "#!/usr/bin/env bash\nexit 0\n"
CHIPS = "(sel) => [...document.querySelectorAll(sel + ' .att-chip .n')].map((n) => n.textContent)"
# a pasted screenshot: a File in the clipboard's DataTransfer (Chromium builds one from script)
PASTE = """(b64) => { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  const dt = new DataTransfer(); dt.items.add(new File([u], 'pasted.png', { type: 'image/png' }));
  const ta = document.getElementById('msg-input'); ta.focus();
  const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }); ta.dispatchEvent(ev); return ev.defaultPrevented; }"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def main():
    import base64
    tmp = tempfile.mkdtemp(prefix="sb-attach-page.")
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

            print("demo: no attach control:")
            pg = new_page()
            pg.goto(PAGE + "?demo=1")
            pg.wait_for_timeout(400)
            check("the demo has no attach row (nowhere to store a file)", pg.evaluate("() => document.getElementById('msg-atts-row').hidden"))
            pg.close()

            print("board-serve:")
            home, cfg, fbin = (os.path.join(tmp, x) for x in ("home", "config", "bin"))
            for d in (os.path.join(home, "state"), cfg, fbin, os.path.join(tmp, "sessions")):
                os.makedirs(d)
            port = free_port()
            write(os.path.join(cfg, "fleet.yaml"), "approvers: [local]\nsessions:\n  - code: API\n    cwd: /w\n"
                  "serve:\n  enabled: true\n  port: %d\n" % port)
            now = int(time.time() * 1000)
            write(os.path.join(home, "snapshot.json"), {"generated_at": now, "sessions": [
                {"code": "API", "aliases": ["API"], "state": "working", "updated_at": now}]})
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
            pg = new_page()
            pg.goto(base)
            pg.wait_for_function("() => !document.getElementById('composer').hidden", timeout=10000)
            r = pg.evaluate("""() => { const row = document.getElementById('msg-atts-row'), b = document.getElementById('msg-attach');
              return { row: !row.hidden && row.offsetHeight > 0, btn: b.textContent.trim(), help: document.getElementById('msg-attach-help').textContent,
                accept: document.getElementById('msg-file').accept }; }""")
            check("the composer has 📎 Attach image and says what fits", r["row"] and r["btn"] == "📎 Attach image"
                  and "PNG, JPEG, GIF or WebP, up to 5 MB each, at most 4" in r["help"], r)
            check("  the picker offers the four image types only", r["accept"] == "image/png,image/jpeg,image/gif,image/webp", r)

            pg.set_input_files("#msg-file", files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"text"}])
            check("a text file is refused with a reason, no chip", pg.evaluate(CHIPS, "#msg-atts") == []
                  and pg.text_content("#msg-err") == "Only PNG, JPEG, GIF and WebP images can be attached.")
            pg.set_input_files("#msg-file", files=[{"name": "big.png", "mimeType": "image/png", "buffer": png() + b"\x00" * (5 * 1024 * 1024)}])
            check("an image over 5 MB is refused before upload", pg.evaluate(CHIPS, "#msg-atts") == []
                  and pg.text_content("#msg-err") == "big.png is over 5 MB.")
            with pg.expect_response(lambda x: x.url.endswith("/api/uploads")) as resp:
                pg.set_input_files("#msg-file", files=[{"name": "screen.png", "mimeType": "image/png", "buffer": png()}])
            check("  picking an image uploads it (201)", resp.value.status == 201, resp.value.status)
            pg.wait_for_function("() => !document.querySelector('#msg-atts .att-chip.busy')")
            check("  and shows a chip: 📎 name · size", pg.evaluate(CHIPS, "#msg-atts") == ["📎 screen.png · %d B" % len(png())],
                  pg.evaluate(CHIPS, "#msg-atts"))
            check("  the error line is cleared", pg.text_content("#msg-err") == "")
            prevented = pg.evaluate(PASTE, base64.b64encode(png()).decode())
            pg.wait_for_function("() => document.querySelectorAll('#msg-atts .att-chip').length === 2 && !document.querySelector('#msg-atts .att-chip.busy')")
            check("a pasted screenshot attaches too (the paste puts no text in the box)", prevented and pg.input_value("#msg-input") == ""
                  and pg.evaluate(CHIPS, "#msg-atts")[1].startswith("📎 pasted.png · "), pg.evaluate(CHIPS, "#msg-atts"))
            check("no preview anywhere: no <img>, no blob: URL", pg.evaluate(
                "() => document.querySelectorAll('#messages img').length === 0 && !document.documentElement.innerHTML.includes('blob:')"))
            dropped = pg.evaluate("""(b64) => { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
              const dt = new DataTransfer(); dt.items.add(new File([u], 'dropped.png', { type: 'image/png' }));
              const f = document.getElementById('composer'); f.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true }));
              const on = f.classList.contains('dropping'), ev = new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true });
              f.dispatchEvent(ev); return { on, prevented: ev.defaultPrevented, off: !f.classList.contains('dropping') }; }""", base64.b64encode(png()).decode())
            pg.wait_for_function("() => document.querySelectorAll('#msg-atts .att-chip').length === 3 && !document.querySelector('#msg-atts .att-chip.busy')")
            check("a dropped image attaches too (outlined while dragging over)", dropped == {"on": True, "prevented": True, "off": True}
                  and pg.evaluate(CHIPS, "#msg-atts")[2].startswith("📎 dropped.png · "), (dropped, pg.evaluate(CHIPS, "#msg-atts")))
            pg.locator("#msg-atts .att-chip").nth(2).locator("button.x").click()
            x = pg.locator("#msg-atts .att-chip").nth(1).locator("button.x")
            check("  each chip has a ✕ button named for the file", x.get_attribute("aria-label") == "Remove pasted.png")
            def pending():
                out = []
                for f in os.listdir(os.path.join(home, "uploads")):
                    if f.endswith(".json"):
                        with open(os.path.join(home, "uploads", f), encoding="utf-8") as fh:
                            if not json.load(fh).get("messages"):
                                out.append(f[:-5])
                return sorted(out)
            before = pending()
            x.focus()
            with pg.expect_response(lambda r: "/api/uploads/" in r.url and r.url.endswith("/delete")) as dr:
                pg.keyboard.press("Enter")
            check("  ✕ deletes the server's copy at once (204)", dr.value.status == 204 and len(pending()) == len(before) - 1,
                  (dr.value.status, before, pending()))
            check("  ✕ by keyboard removes it; focus goes back to Attach", pg.evaluate(CHIPS, "#msg-atts") == ["📎 screen.png · %d B" % len(png())]
                  and pg.evaluate("() => document.activeElement.id") == "msg-attach")
            pg.set_input_files("#msg-file", files=[{"name": "p%d.png" % i, "mimeType": "image/png", "buffer": png()} for i in range(4)])
            pg.wait_for_timeout(300)
            check("at most 4 per message: the 5th is refused with a reason", len(pg.evaluate(CHIPS, "#msg-atts")) == 4
                  and pg.text_content("#msg-err") == "At most 4 images per message.", pg.evaluate(CHIPS, "#msg-atts"))
            pg.wait_for_function("() => !document.querySelector('#msg-atts .att-chip.busy')")
            for _ in range(2):
                pg.locator("#msg-atts .att-chip button.x").last.click()
            with pg.expect_response(lambda x: x.url.endswith("/api/uploads")):
                pg.set_input_files("#msg-file", files=[{"name": "photo.jpg", "mimeType": "image/jpeg", "buffer": JPEG}])
            pg.wait_for_function("() => document.querySelectorAll('#msg-atts .att-chip').length === 3 && !document.querySelector('#msg-atts .att-chip.busy')")
            want = pg.evaluate(CHIPS, "#msg-atts")
            pg.fill("#msg-input", "@API what is wrong on these screens?")
            with pg.expect_request(lambda x: x.url.endswith("/api/messages") and x.method == "POST") as rq:
                pg.click("#msg-send")
            sent = json.loads(rq.value.post_data)
            check("Send carries the three upload ids", len(sent.get("attachments") or []) == 3
                  and all(a.startswith("u-") for a in sent["attachments"]), sent)
            check("  and clears the chips", pg.evaluate(CHIPS, "#msg-atts") == [])
            pg.wait_for_function("() => document.querySelector('#threads .att-chips')", timeout=10000)
            th = pg.evaluate("(sel) => [...document.querySelectorAll('#threads > li:first-child .att-chip .n')].map((n) => n.textContent)", "")
            check("the thread shows the same chips (names and sizes)", th == want, (th, want))
            check("  still no <img> in the section", pg.evaluate("() => document.querySelectorAll('#messages img').length") == 0)
            mid = pg.evaluate("() => document.querySelector('#threads > li:first-child').id.replace('thread-', '')")
            show = subprocess.run([os.path.join(REPO, "bin", "board"), "message", "show", mid], env=env, capture_output=True, text=True)
            paths = [os.path.join(home, "uploads", a + ".") for a in sent["attachments"]]
            check("the relay line lists each image's path", all(x in show.stdout for x in paths)
                  and show.stdout.strip().endswith("[re %s]" % mid), show.stdout[:300])
            # an attached image that is gone from the server when Send goes (bad_attachments): the text
            # comes back, the chip does not
            pg.set_input_files("#msg-file", files=[{"name": "gone.png", "mimeType": "image/png", "buffer": png()}])
            pg.wait_for_function("() => document.querySelector('#msg-atts .att-chip') && !document.querySelector('#msg-atts .att-chip.busy')")
            for f in os.listdir(os.path.join(home, "uploads")):
                if f.endswith(".png") and f[:-4] not in sent["attachments"]:
                    os.unlink(os.path.join(home, "uploads", f))
            pg.fill("#msg-input", "@API one more")
            with pg.expect_response(lambda r: r.url.endswith("/api/messages") and r.request.method == "POST") as mr:
                pg.click("#msg-send")
            pg.wait_for_function("() => document.getElementById('msg-input').value !== ''")
            pg.wait_for_timeout(200)
            check("a send refused with bad_attachments: the text is restored, the gone image's chip is not",
                  mr.value.status == 400 and pg.input_value("#msg-input") == "@API one more" and pg.evaluate(CHIPS, "#msg-atts") == [],
                  (mr.value.status, pg.input_value("#msg-input"), pg.evaluate(CHIPS, "#msg-atts")))
            pg.close()

            for theme in ("light", "dark"):
                pg = new_page("try { localStorage.setItem('switchboard.theme', '%s'); localStorage.setItem('switchboard.lang', 'zh-TW'); } catch (e) {}" % theme,
                              viewport={"width": 390, "height": 844}, has_touch=True)
                pg.goto(base)
                pg.wait_for_function("() => !document.getElementById('composer').hidden", timeout=10000)
                pg.set_input_files("#msg-file", files=[{"name": "a-very-long-file-name-" + "x" * 80 + ".png", "mimeType": "image/png", "buffer": png()}])
                pg.wait_for_function("() => document.querySelector('#msg-atts .att-chip') && !document.querySelector('#msg-atts .att-chip.busy')")
                r = pg.evaluate("""() => { const out = [...document.querySelectorAll('#messages *')].filter((e) => { const r = e.getBoundingClientRect(); return r.width && r.right > innerWidth + 1; })
                    .map((e) => e.tagName + '.' + e.className);
                  const x = document.querySelector('#msg-atts .att-chip button.x').getBoundingClientRect();
                  return { sw: document.scrollingElement.scrollWidth, vw: innerWidth, out, btn: document.getElementById('msg-attach').textContent.trim(),
                    x: x.width >= 24 && x.height >= 24, xl: document.querySelector('#msg-atts .att-chip button.x').getAttribute('aria-label').slice(0, 3),
                    list: document.getElementById('msg-atts').getAttribute('aria-label') }; }""")
                check("390 px, %s: no side scroll with a long file name" % theme, r["sw"] <= r["vw"] and not r["out"], r)
                check("  the ✕ is a usable target; zh-TW strings", r["x"] and r["btn"] == "📎 附加圖片" and r["xl"] == "移除 " and r["list"] == "附加的圖片", r)
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
