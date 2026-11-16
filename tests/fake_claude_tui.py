#!/usr/bin/env python3
"""tests/fake_claude_tui.py — a tiny stand-in for Claude Code's screens at a usage limit, run in a
pane of a private tmux server (tests/test_resume.py, `tmux -L <test socket>`). Never a real
session: it only draws fixture screens and records the keys it gets.

  fake_claude_tui.py DIR STATE      STATE: dialog | wait | ready | idle | healthy

Writes DIR/pid (its pid) and appends every key to DIR/keys (one JSON line {key, t}).
  dialog  --Escape--> wait (Claude arms the automatic continue and prefills "continue")
  wait    --Escape--> idle (the prefill is taken back; "Automatic continue cancelled" above)
  idle    typing goes into the box; Enter with text -> working
  ready   Enter -> working (submits the prefilled "continue")
  Two Escapes less than a second apart on an input box open a rewind picker (as Claude does).
"""
import json
import os
import select
import sys
import termios
import time
import tty

RULE = "─" * 60
NB = "\u00a0"


class Tui:
    def __init__(self, d, state):
        self.dir, self.state, self.typed, self.last_esc = d, state, "", 0.0
        self.typed = "continue" if state in ("wait", "ready") else ""
        self.echo = None

    def box(self, below=()):
        return [RULE, "❯ " + self.typed if self.typed else "❯" + NB, RULE] + list(below)

    def lines(self):
        head = ["Claude Code v2", "", "❯ fix the flaky upload test", "● Looking at the upload handler first."]
        st = self.state
        if st == "dialog":
            return head + ["  ⎿  Usage limit reached · limit resets 3pm", " ▔▔▔▔", "   What do you want to do?",
                           "   ❯ 1. Stop and wait for limit to reset", "     2. Wait here, then continue automatically at 3pm",
                           "     3. Upgrade your plan", "", "   Enter to confirm · Esc to cancel"]
        if st == "wait":
            return head + ["  ⎿  Usage limit reached · continuing automatically at 3pm · esc to cancel"] + self.box(
                ["  Usage limit reached · limit resets 3pm", "  Continuing automatically at 3pm · Esc to cancel"])
        if st == "ready":
            return head + ["  ⎿  Usage limit reached · limit resets 3pm"] + self.box(
                ["  Your usage limit has reset · Press enter to continue"])
        if st == "idle":
            return head + ["  ⎿  Usage limit reached · limit resets 3pm",
                           "  ⎿  Automatic continue cancelled · /rate-limit-options to re-arm"] + self.box(
                ["  ⏵⏵ auto mode on (shift+tab to cycle)"])
        if st == "healthy":
            return head + ["● Done: the upload test passes 50 runs in a row."] + self.box(["  ⏵⏵ auto mode on (shift+tab to cycle)"])
        if st == "rewind":
            return head + ["   Rewind to which message?", "   ❯ fix the flaky upload test", "", "   Enter to confirm · Esc to cancel"]
        return head + ["❯ " + (self.echo or ""), "✻ Thinking… (3s · esc to interrupt)"] + self.box(
            ["  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt"])

    def draw(self):
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(self.lines()))
        sys.stdout.flush()

    def log(self, key):
        with open(os.path.join(self.dir, "keys"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "t": time.time(), "state": self.state}) + "\n")

    def key(self, k):
        self.log(k)
        st = self.state
        if k == "Escape":
            quick = time.time() - self.last_esc < 1.0
            self.last_esc = time.time()
            if st == "dialog":
                self.state = "wait"
                self.typed = "continue"
            elif st == "wait":
                self.state, self.typed = "idle", ""
            elif quick and st in ("idle", "ready", "healthy"):
                self.state = "rewind"
        elif k == "Enter":
            if (st == "idle" and self.typed) or (st == "ready" and self.typed == "continue"):
                self.echo, self.typed, self.state = self.typed, "", "working"
        elif st in ("idle", "ready", "healthy", "wait"):
            self.typed += k


def main(argv):
    d, state = argv[0], argv[1]
    with open(os.path.join(d, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    t = Tui(d, state)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        t.draw()
        while True:
            data = os.read(fd, 1024)
            if not data:
                return 0
            text = data.decode("utf-8", "replace")
            i = 0
            while i < len(text):
                ch = text[i]
                if ch == "\x1b":
                    if i + 1 < len(text) or select.select([fd], [], [], 0.05)[0]:
                        i = len(text)  # an escape sequence (arrow keys ...): ignored
                        continue
                    t.key("Escape")
                elif ch in "\r\n":
                    t.key("Enter")
                elif ch >= " ":
                    t.key(ch)
                i += 1
            t.draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
