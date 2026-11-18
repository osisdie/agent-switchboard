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

  STATE question: an AskUserQuestion picker whose question is taller than the pane (tests/
  test_press_tall.py): the question in a box ("│ ..."), its options and the picker footer, written
  once without clearing the screen, so the start of the question scrolls into tmux's history.
  question-turn: the same, with another turn ("● ...") drawn between the question and its options.
  question-cjk: the same, the question in Chinese with a full-width space (U+3000) in its start.
  Keys are only recorded there.

  STATE form2 | form3 | form4 | form4-tall | form3-q2: a form of several questions (tests/test_press_form.py), drawn as
  Claude Code 2.1.3xx draws it: a tab bar, one question at a time in a box ("│ ..." on every line,
  wrapped lines too: a question holding a newline gets this gutter), its options, the "Type
  something" row, and the picker footer wrapped onto two lines ("... Esc to" / "cancel"). A digit
  picks an option and moves on; the "Type something" digit focuses that row, typed text replaces
  its label, Enter confirms it and moves on. Then the review screen (the questions boxed too,
  "→ answer" under each) with "1. Submit answers" and "2. Cancel", then a horizontal rule ("────",
  as the TUI draws it under the rows): 1 submits (DIR/answers gets the answers). form2 asks
  FORM[:2], form3 FORM[:3], form4 all four questions, its review screen with the picker footer
  wrapped under the rule too; form4-tall the same in a 16-column box, its review screen more than
  30 lines tall (run it in a pane of 50 rows); form3-q2 opens on question 2 (question 1 answered in the session
  already).
"""
import json
import os
import select
import sys
import termios
import time
import tty

RULE = "─" * 60
# The question of state question-cjk (tests/test_press_tall.py asks the same one): a full-width
# space (U+3000) within its first 40 characters, and long enough to be clipped by the hook.
QUESTION_CJK = "請選擇\u3000這次測試要用的顏色：" + "背景說明" * 90
NB = "\u00a0"
# The questions of the form states (tests/test_press_form.py asks the same ones). Each holds a
# newline, as a question drawn in a box does; the first is long enough to wrap in its box.
FORM = [
    {"question": "TICKET-12：產品內的人員管理（邀請、改角色、停用、重設密碼）要在「由系統管理在管」的情況下鎖住。\n這個判定要用哪一層？",
     "header": "鎖定判定", "multiSelect": False,
     "options": [{"label": "公司層", "description": "只要公司連了就鎖"}, {"label": "人層", "description": "看每個人"}]},
    {"question": "Which size for the banner?\nPick the one that fits the header.", "header": "Size", "multiSelect": False,
     "options": [{"label": "Small", "description": "compact"}, {"label": "Large", "description": "wide"}]},
    {"question": "Which shape for the banner?\nRound or square corners.", "header": "Shape", "multiSelect": False,
     "options": [{"label": "Round", "description": "soft"}, {"label": "Square", "description": "sharp"}]},
    {"question": "Who signs off on the banner copy?\nThe team that owns the header.", "header": "Owner", "multiSelect": False,
     "options": [{"label": "Design", "description": "the design team"}, {"label": "Marketing", "description": "the campaign team"}]},
]
FORM_WIDTH = 52   # columns of a boxed question line after "│ " (a full-width character takes 2)


def cells(ch):
    return 2 if ord(ch) >= 0x1100 and (0x2E80 <= ord(ch) <= 0xA4CF or 0xAC00 <= ord(ch) <= 0xD7A3
                                       or 0xF900 <= ord(ch) <= 0xFAFF or 0xFF00 <= ord(ch) <= 0xFF60
                                       or 0x3000 <= ord(ch) <= 0x303F) else 1


def wrap(text, width):
    """Lines of at most WIDTH columns, broken between words (anywhere in CJK text), like the TUI."""
    out = []
    for para in text.split("\n"):
        cur, w = "", 0
        for word in para.replace(" ", " \0").split("\0"):
            pieces = [word] if all(cells(c) == 1 for c in word) else list(word)
            for piece in pieces:
                pw = sum(cells(c) for c in piece)
                if w + pw > width and cur.strip():
                    out.append(cur.rstrip())
                    cur, w = piece.lstrip(), sum(cells(c) for c in piece.lstrip())
                else:
                    cur, w = cur + piece, w + pw
        out.append(cur.rstrip())
    return out


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

    def question(self):
        if self.state == "question-cjk":  # full-width characters: 30 per box line (60 columns)
            text = QUESTION_CJK
            box = ["│ " + text[i:i + 30] for i in range(0, len(text), 30)]
        else:
            words = ("Which color do you prefer for the test? " + "context " * 60).split()
            box, cur = [], ""
            for w in words:
                if len(cur) + len(w) + 1 > 40:
                    box.append("│ " + cur)
                    cur = w
                else:
                    cur = (cur + " " + w).strip()
            box.append("│ " + cur)
        turn = ["", "● Meanwhile: the release notes are written."] if self.state == "question-turn" else []
        return (["Claude Code v2", "", "❯ pick a color for the test", "● I need one decision.", "", " ☐ Color", ""]
                + box + turn + ["", "❯ 1. Red", "     warm", "  2. Blue", "     cool", "  3. Green", "     natural",
                                "  4. Type something.", "───", "  5. Chat about this", "",
                                "Enter to select · ↑/↓ to navigate · Esc to cancel"])

    def form_init(self):
        self.qs = FORM[:4] if self.state.startswith("form4") else FORM[:2] if self.state == "form2" else FORM[:3]
        self.review_footer = self.state.startswith("form4")
        # form4-tall: the questions wrap in a narrow box, so the review screen is taller than 30 lines
        self.width = 16 if self.state == "form4-tall" else FORM_WIDTH
        self.answers = [None] * len(self.qs)
        self.cur = 0
        if self.state == "form3-q2":
            self.answers[0], self.cur = self.qs[0]["options"][0]["label"], 1
        self.editing, self.typed, self.state = False, "", "form"

    def form_lines(self):
        head = ["Claude Code v2", "", "❯ ask me a few things", "● I need some decisions.", "", "─" * 56]
        tabs = "  ".join(("☒" if a is not None else "☐") + " " + q["header"] for q, a in zip(self.qs, self.answers))
        out = head + ["←  " + tabs + "  ✔ Submit  →", ""]
        if self.state == "submitted":
            return head + ["● Answers submitted: " + " · ".join(self.answers)]
        if self.state == "review":
            out += ["Review your answers", ""]
            for q, a in zip(self.qs, self.answers):
                out += [" │ " + ln for ln in wrap(q["question"], self.width)] + ["   → " + a]
            out += ["", "Ready to submit your answers?", "", "❯ 1. Submit answers", "  2. Cancel", "─" * 56]
            if self.review_footer:
                out += ["Enter to select · Tab/Arrow keys to navigate · Esc to", "cancel"]
            return out
        q = self.qs[self.cur]
        out += ["│ " + ln for ln in wrap(q["question"], self.width)] + [""]
        n = len(q["options"])
        for k, o in enumerate(q["options"], 1):
            out += ["%s %d. %s" % ("❯" if k == 1 and not self.editing else " ", k, o["label"]), "     " + o["description"]]
        out += ["%s %d. %s" % ("❯" if self.editing else " ", n + 1, self.typed or "Type something."), "─" * 7,
                "  %d. Chat about this" % (n + 2), "",
                "Enter to select · Tab/Arrow keys to navigate · Esc to", "cancel"]
        return out

    def form_answer(self, ans):
        self.answers[self.cur] = ans
        self.editing, self.typed = False, ""
        if self.cur + 1 < len(self.qs):
            self.cur += 1
        else:
            self.state = "review"

    def form_key(self, k):
        if self.state == "review":
            if k == "1":
                self.state = "submitted"
                with open(os.path.join(self.dir, "answers"), "w", encoding="utf-8") as fh:
                    json.dump(self.answers, fh, ensure_ascii=False)
            return
        if self.state != "form":
            return
        n = len(self.qs[self.cur]["options"])
        if self.editing:
            if k == "Enter":
                if self.typed:
                    self.form_answer(self.typed)
            elif k != "Escape":
                self.typed += k
        elif k.isdigit() and 1 <= int(k) <= n:
            self.form_answer(self.qs[self.cur]["options"][int(k) - 1]["label"])
        elif k == str(n + 1):
            self.editing = True

    def draw(self):
        if self.state in ("form", "review", "submitted"):
            sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(self.form_lines()))
            sys.stdout.flush()
            return
        if self.state.startswith("question"):
            if not getattr(self, "drawn", False):  # once, no clear: the top scrolls into history
                self.drawn = True
                sys.stdout.write("\r\n".join(self.question()))
                sys.stdout.flush()
            return
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(self.lines()))
        sys.stdout.flush()

    def log(self, key):
        with open(os.path.join(self.dir, "keys"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "t": time.time(), "state": self.state}) + "\n")

    def key(self, k):
        self.log(k)
        st = self.state
        if st.startswith("question"):
            return
        if st in ("form", "review", "submitted"):
            return self.form_key(k)
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
    if state.startswith("form"):
        t.form_init()
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
