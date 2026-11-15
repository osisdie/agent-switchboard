#!/usr/bin/env python3
"""tests/test_page_question.py — optional headless check of pane-question cards (an AskUserQuestion
picker waiting in a session, decision source "pane") in artifact/board.html.

Demo data: a single-select question shows "<CODE> asks:", one button per option (label and
description) and a "Type something" field; a form with a multi-select question is read-only with
"Answer in the session (multi-select)". Local adapter: clicking an option goes through a real bin/board-serve, which runs
`board press CODE answer`; a fake tmux records the number key.

Needs python playwright and a Chromium it can launch; without them it prints "skipped" and exits 0.
CI does not run it.

  python3 tests/test_page_question.py [-v]

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell. Uses a temporary
BOARD_HOME; never touches ~/.agent-switchboard.
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


CARD_JS = """(id) => {
  const c = document.getElementById('dec-' + id);
  if (!c) return null;
  return {
    title: (c.querySelector('h3') || {}).textContent || '',
    buttons: [...c.querySelectorAll('.pq-opt')].map((b) => b.textContent),
    readonly: [...c.querySelectorAll('.pq-ro')].map((b) => b.textContent),
    input: !!c.querySelector('input[placeholder="Type something"]'),
    notes: [...c.querySelectorAll('.note')].map((n) => n.textContent),
    wide: document.documentElement.scrollWidth > window.innerWidth,
  };
}"""

Q1 = [{"question": "Which color do you prefer for the test?", "header": "Color", "multiSelect": False,
       "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"},
                   {"label": "Green", "description": "natural"}]}]
SCREEN = (" ☐ Color\n\nWhich color do you prefer for the test?\n\n❯ 1. Red\n     warm\n  2. Blue\n     cool\n"
          "  3. Green\n     natural\n  4. Type something.\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n")
FAKE_TMUX = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  display-message) echo CP ;;
  capture-pane) cat "$FAKE_TMUX_SCREEN" ;;
esac
exit 0
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def main():
    tmp = tempfile.mkdtemp(prefix="switchboard-question-test.")
    srv = live = None
    try:
        with sync_playwright() as p:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            try:
                browser = p.chromium.launch(headless=True, executable_path=exe)
            except Exception as exc:  # noqa: BLE001 - no browser installed: skip, don't fail
                print("skipped: cannot launch Chromium (%s)" % str(exc).splitlines()[0][:160])
                return 0
            page = browser.new_context(viewport={"width": 390, "height": 760}).new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            print("demo:")
            page.goto(PAGE + "?demo=1", wait_until="domcontentloaded")
            page.wait_for_selector("#dec-d-9-demo")
            c = page.evaluate(CARD_JS, "d-9-demo")
            check("single question: '<CODE> asks:' title", c["title"].startswith("CP asks:"), c)
            check("one button per option, label + description",
                  c["buttons"] == ["1. Approve, run in orderRun all steps one after another",
                                   "2. Only steps 1–3Stop before the deploy step", "3. Not yetWait for more review"], c)
            check("free-text 'Type something' field", c["input"], c)
            check("no horizontal scroll at phone width", not c["wide"], c)
            m = page.evaluate(CARD_JS, "d-10-demo")
            check("multi-question: read-only, no buttons, no input", not m["buttons"] and not m["input"] and m["readonly"], m)
            check("form with a multi-select question: answer in the session",
                  any("Answer in the session (multi-select)" in n for n in m["notes"]), m)
            page.click("#dec-d-9-demo .pq-opt >> nth=1")
            page.wait_for_timeout(300)
            check("demo click answers the card", page.evaluate("() => !document.getElementById('dec-d-9-demo')"))

            print("local adapter (board-serve + board press):")
            home, cfg, sess, fbin = (os.path.join(tmp, x) for x in ("home", "config", "sessions", "bin"))
            for d in (os.path.join(home, "decisions"), os.path.join(home, "state"), cfg, sess, fbin):
                os.makedirs(d)
            port = free_port()
            write(os.path.join(cfg, "fleet.yaml"), "approvers: [local]\nserve:\n  enabled: true\n  port: %d\n" % port)
            live = subprocess.Popen(["sleep", "600"])
            sid = "cccccccc-1111-2222-3333-444444444444"
            now = int(time.time() * 1000)
            write(os.path.join(sess, "%d.json" % live.pid),
                  {"pid": live.pid, "sessionId": sid, "cwd": "/w", "tmux": "CP:@3.%42", "name": "CP",
                   "status": "waiting", "startedAt": 1})
            pend = {"tool_use_id": "tu_q", "tool_name": "AskUserQuestion", "summary": Q1[0]["question"],
                    "summary_sha256": "sha-q", "questions": Q1}
            write(os.path.join(home, "state", sid + ".json"),
                  {"session_id": sid, "state": "needs-you", "tmux_pane": "%42", "pending": pend, "updated_at": now})
            did = "d-1794650400-qq11"
            write(os.path.join(home, "decisions", did + ".json"),
                  {"id": did, "kind": "question", "source": "pane", "from": None, "session_id": sid,
                   "tool_use_id": "tu_q", "tool_name": "AskUserQuestion", "question": Q1[0]["question"],
                   "options": ["Red", "Blue", "Green"], "questions": Q1, "pressable": True,
                   "summary": Q1[0]["question"], "summary_sha256": "sha-q", "status": "pending",
                   "answer": None, "created_at": now})
            write(os.path.join(fbin, "tmux"), FAKE_TMUX)
            os.chmod(os.path.join(fbin, "tmux"), 0o755)
            write(os.path.join(tmp, "screen"), SCREEN)
            log = os.path.join(tmp, "tmux.log")
            write(log, "")
            proc = os.path.join(tmp, "proc")
            subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), proc], check=True)
            env = dict(os.environ, BOARD_HOME=home, BOARD_CONFIG_DIR=cfg, CLAUDE_SESSIONS_DIR=sess, CLAUDE_JSON=os.path.join(tmp, "no-claude.json"),
                       BOARD_PROC_ROOT=proc, FAKE_TMUX_LOG=log, FAKE_TMUX_SCREEN=os.path.join(tmp, "screen"),
                       PATH=fbin + os.pathsep + os.environ.get("PATH", ""))
            for k in ("BOARD_PUBLIC_URL", "TMUX", "TMUX_PANE"):
                env.pop(k, None)
            subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            page.goto("http://127.0.0.1:%d/" % port, wait_until="domcontentloaded")
            page.wait_for_selector("#dec-%s .pq-opt" % did, timeout=5000)
            c = page.evaluate(CARD_JS, did)
            check("local: 'CP asks:' with three option buttons",
                  c["title"].startswith("CP asks:") and len(c["buttons"]) == 3, c)
            page.click("#dec-%s .pq-opt >> nth=1" % did)
            for _ in range(50):
                with open(log, encoding="utf-8") as fh:
                    keys = [x for x in fh.read().splitlines() if x.startswith("send-keys")]
                if keys:
                    break
                page.wait_for_timeout(100)
            check("local: board press sent the option's number key", keys == ["send-keys -t %42 2"], keys)
            # board press marks the decision relayed after send-keys returns: poll for it, as for the keys.
            for _ in range(50):
                with open(os.path.join(home, "decisions", did + ".json"), encoding="utf-8") as fh:
                    rec = json.load(fh)
                if rec.get("status") == "relayed":
                    break
                page.wait_for_timeout(100)
            check("local: decision relayed, answered Blue",
                  (rec.get("status"), rec.get("answer"), rec.get("answer_index")) == ("relayed", "Blue", 2), rec)

            check("no page errors", not errors, errors)
            browser.close()
    finally:
        if srv:
            srv.terminate()
            srv.wait()
        if live:
            live.terminate()
            live.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("passed: %d  failed: %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
