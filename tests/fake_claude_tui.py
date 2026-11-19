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

  STATE msel-NAME[-preDIGITS][-stuckD][-tsfocus][-wW]: multi-select questions (tests/
  test_press_multiselect.py), drawn as Claude Code 2.1.32x draws them (tests/fixtures/multiselect/
  holds real captures; the test checks this drawing against them): MS_FORMS[NAME], alone or in a
  form, a tab bar even for one question, "N. [ ] label" / "N. [✔] label" rows (label and
  description wrapped under it, 9 columns in), "N+1. [ ] Type something", a "Next" row ("Submit"
  on the last question), a rule, "Chat about this" and the footer. A digit toggles that box and the
  cursor stays; with "Type something" focused, keys are typed into it (its box gets checked);
  Enter toggles the focused row (on the Next/Submit row it moves on); Right or Tab moves on, Left
  goes back; Up/Down move the focus. A single-select question of the form takes a digit and moves
  on. The review screen lists a multi-select answer as "→ A, B" in the order the boxes were
  checked and leaves out a question with none (with "⚠ Some questions are unanswered"); "1"
  submits (DIR/answers: one string per question, as Claude Code reports them: labels joined by
  ", " in that order, "" for none). mischief: on the way to the review, question 1's boxes move
  to question 2 (as if changed in the pane meanwhile). preDIGITS: boxes of the first question checked by a human
  before the press, in that order; stuckD: digit D is ignored (its box never flips); tsfocus: the
  focus starts on "Type something"; wW: width W (default 100).

  Environment (form and msel states): FAKE_TUI_RULE_LABEL=NAME draws the rule as Claude Code
  2.1.327 does when the session has a name, "──────── NAME ─" (review-labelled-
  rule.txt under tests/fixtures/press/, from a live pane): under the review screen's rows in place of the plain rule, and as
  the last line of every question screen. FAKE_TUI_REVIEW_TIGHT=1 draws the review's rows right
  under the last answer, 3 columns in ("   ❯ 1. Submit answers"); FAKE_TUI_REVIEW_EXTRA=TEXT adds
  TEXT as a line right under the first answer (a 3-column continuation line, say).
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
# Multi-select forms (state msel-NAME), the questions asked in the real captures under
# tests/fixtures/multiselect/.
MS_FORMS = {
    "fruits": [{"question": "Which fruits should go into the basket today?", "header": "Fruits", "multiSelect": True,
                "options": [{"label": "Apple", "description": "Crisp and sweet, a classic choice"},
                            {"label": "Banana", "description": "Soft, potassium-rich, easy to eat"},
                            {"label": "Cherry", "description": "Small, sweet and juicy"},
                            {"label": "Grape", "description": "Bite-sized and great for snacking"}]}],
    "pizza": [{"question": "Which toppings go on the pizza tonight?", "header": "Toppings", "multiSelect": True,
               "options": [{"label": "Cheese", "description": "Classic mozzarella topping"},
                           {"label": "Olives", "description": "Briny black or green olives"},
                           {"label": "Peppers", "description": "Sweet bell peppers for crunch"},
                           {"label": "Onions", "description": "Sharp, savory onion rings"}]},
              {"question": "Which crust do you want?", "header": "Crust", "multiSelect": False,
               "options": [{"label": "Thin", "description": "Light and crispy base"},
                           {"label": "Thick", "description": "Soft, bready and filling"}]},
              {"question": "Which sides should we order with it?", "header": "Sides", "multiSelect": True,
               "options": [{"label": "Salad", "description": "Fresh green side salad"},
                           {"label": "Fries", "description": "Crispy golden potato fries"},
                           {"label": "Soup", "description": "Warm bowl of soup of the day"}]}],
    "checks": [{"question": "Which checks should run before the release?\nPick every check that applies.",
                "header": "Checks", "multiSelect": True,
                "options": [{"label": "Unit tests across every supported runtime and platform combination",
                             "description": "Run the full unit test suite on every supported runtime and platform"},
                            {"label": "Lint", "description": "Run the linter to catch style and code quality issues"},
                            {"label": "Docs build", "description": "Build the documentation to confirm it compiles"}]}],
    # two questions with the same options, the second's text holding the first's (pairing on the
    # review screen; -mischief moves the first's boxes to the second on the way to the review)
    "pair": [{"question": "Which sides?", "header": "Sides", "multiSelect": True,
              "options": [{"label": "Salad", "description": ""}, {"label": "Fries", "description": ""}]},
             {"question": "For the kids too: Which sides?", "header": "Kids", "multiSelect": True,
              "options": [{"label": "Salad", "description": ""}, {"label": "Fries", "description": ""}]}],
    # labels that hold ", " and one that starts another: the review screen is read as a set
    "commas": [{"question": "Which regions should the report cover this quarter?", "header": "Regions", "multiSelect": True,
                "options": [{"label": "North, East", "description": "the northern and eastern offices"},
                            {"label": "North", "description": "the northern office only"},
                            {"label": "East", "description": "the eastern office only"}]}],
}


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

    @staticmethod
    def rule(width):
        """A rule; with FAKE_TUI_RULE_LABEL, the labelled one: "─…─ NAME ─"."""
        name = os.environ.get("FAKE_TUI_RULE_LABEL")
        if not name:
            return "─" * width
        return "─" * max(3, width - len(name) - 3) + " " + name + " ─"

    @staticmethod
    def with_extra(block):
        """FAKE_TUI_REVIEW_EXTRA: a line right under the first answer of a review screen."""
        extra = os.environ.get("FAKE_TUI_REVIEW_EXTRA")
        k = next((i for i, ln in enumerate(block) if ln.lstrip().startswith("→")), None)
        return block if not extra or k is None else block[:k + 1] + [extra] + block[k + 1:]

    def form_lines(self):
        head = ["Claude Code v2", "", "❯ ask me a few things", "● I need some decisions.", "", "─" * 56]
        tabs = "  ".join(("☒" if a is not None else "☐") + " " + q["header"] for q, a in zip(self.qs, self.answers))
        out = head + ["←  " + tabs + "  ✔ Submit  →", ""]
        if self.state == "submitted":
            return head + ["● Answers submitted: " + " · ".join(self.answers)]
        if self.state == "review":
            out += ["Review your answers", ""]
            block = []
            for q, a in zip(self.qs, self.answers):
                block += [" │ " + ln for ln in wrap(q["question"], self.width)] + ["   → " + a]
            out += self.with_extra(block)
            if os.environ.get("FAKE_TUI_REVIEW_TIGHT"):
                out += ["   ❯ 1. Submit answers", "     2. Cancel", self.rule(56)]
            else:
                out += ["", "Ready to submit your answers?", "", "❯ 1. Submit answers", "  2. Cancel", self.rule(56)]
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
        if os.environ.get("FAKE_TUI_RULE_LABEL"):
            out += [self.rule(56)]
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

    # ---- multi-select forms (state msel-NAME...)
    def ms_init(self):
        parts = self.state.split("-")
        self.qs = MS_FORMS[parts[1]]
        self.width, self.stuck, pre, tsfocus, self.mischief = 100, None, "", False, False
        for x in parts[2:]:
            if x.startswith("pre"):
                pre = x[3:]
            elif x.startswith("stuck"):
                self.stuck = x[5:]
            elif x == "tsfocus":
                tsfocus = True
            elif x == "mischief":
                self.mischief = True
            elif x.startswith("w"):
                self.width = int(x[1:])
        # per question: checked option numbers in the order checked (multi), the picked label (single)
        self.order = [[] for _ in self.qs]
        self.single = [None for _ in self.qs]
        self.typed = ["" for _ in self.qs]
        self.cur, self.focus, self.state = 0, 1, "ms"
        for c in pre:
            self.order[0].append(int(c))
        if tsfocus:
            self.focus = len(self.qs[0]["options"]) + 1

    def ms_answered(self, i):
        q = self.qs[i]
        if q["multiSelect"]:
            return bool(self.order[i]) or bool(self.typed[i])
        return self.single[i] is not None

    def ms_answer_text(self, i):
        q = self.qs[i]
        if not q["multiSelect"]:
            return self.single[i] or ""
        return ", ".join([q["options"][k - 1]["label"] for k in self.order[i]] + ([self.typed[i]] if self.typed[i] else []))

    def ms_lines(self):
        w = self.width
        out = ["─" * w, "←  " + "  ".join(("☒" if self.ms_answered(i) else "☐") + " " + q["header"]
                                          for i, q in enumerate(self.qs)) + "  ✔ Submit  →", ""]
        if self.state == "ms-submitted":
            return ["● Answered: " + " · ".join(self.ms_answer_text(i) for i in range(len(self.qs)))]
        if self.state == "ms-review":
            out += ["Review your answers", ""]
            if not all(self.ms_answered(i) for i in range(len(self.qs))):
                out += ["⚠ Some questions are unanswered", ""]
            block = []
            for i, q in enumerate(self.qs):
                if not self.ms_answered(i):
                    continue
                ql = q["question"].split("\n")
                if len(ql) > 1:
                    block += [" │ " + ("● " if k == 0 else "  ") + ln for k, ln in enumerate(ql)]
                else:
                    block += [" ● " + ql[0]]
                # a long answer wraps 3 columns in, under the "→" (as captured: boxed-review-wrapped)
                block += ["   " + x for x in wrap("→ " + self.ms_answer_text(i), self.width - 3)]
            out += self.with_extra(block)
            if os.environ.get("FAKE_TUI_REVIEW_TIGHT"):
                out += ["   ❯ 1. Submit answers", "     2. Cancel"]
            else:
                out += ["", "❯ 1. Submit answers", "  2. Cancel"]
            return out + ([self.rule(w)] if os.environ.get("FAKE_TUI_RULE_LABEL") else [])
        q = self.qs[self.cur]
        ql = q["question"].split("\n")
        out += (["│ " + ln for ln in ql] if len(ql) > 1 else ql) + [""]
        n = len(q["options"])
        mark = lambda k: "❯" if self.focus == k else " "  # noqa: E731
        for k, o in enumerate(q["options"], 1):
            if q["multiSelect"]:
                lab = wrap(o["label"], w - 9)
                box = "✔" if k in self.order[self.cur] else " "
                out += ["%s %d. [%s] %s" % (mark(k), k, box, lab[0])] + ["         " + x for x in lab[1:]]
                out += ["         " + x for x in wrap(o["description"], w - 9)]
            else:
                out += ["%s %d. %s" % (mark(k), k, o["label"]), "     " + o["description"]]
        if q["multiSelect"]:
            t = self.typed[self.cur]
            out += ["%s %d. [%s] %s" % (mark(n + 1), n + 1, "✔" if t else " ", t or "Type something"),
                    "%s    %s" % (mark(n + 2), "Submit" if self.cur == len(self.qs) - 1 else "Next")]
        else:
            out += ["%s %d. Type something." % (mark(n + 1), n + 1)]
        out += ["─" * w, "  %d. Chat about this" % (n + 2), ""]
        nav = "↑/↓ to navigate" if len(self.qs) == 1 else "Tab/Arrow keys to navigate"
        return out + ["Enter to select · %s · Esc to cancel" % nav] + ([self.rule(w)] if os.environ.get("FAKE_TUI_RULE_LABEL") else [])

    def ms_next(self):
        if self.cur + 1 < len(self.qs):
            self.cur += 1
            self.focus = 1
        else:
            self.state = "ms-review"
            if self.mischief:   # someone in the pane moved question 1's boxes to question 2
                self.order[1], self.order[0] = self.order[0], []

    def ms_key(self, k):
        if self.state == "ms-review":
            if k == "1":
                self.state = "ms-submitted"
                with open(os.path.join(self.dir, "answers"), "w", encoding="utf-8") as fh:
                    json.dump([self.ms_answer_text(i) for i in range(len(self.qs))], fh, ensure_ascii=False)
            elif k == "Left":
                self.state, self.cur, self.focus = "ms", len(self.qs) - 1, 1
            return
        if self.state != "ms":
            return
        q = self.qs[self.cur]
        n = len(q["options"])
        if q["multiSelect"] and self.focus == n + 1 and len(k) == 1:
            self.typed[self.cur] += k   # typed into "Type something" (its box gets checked)
            return
        if k in ("Right", "Tab"):
            self.ms_next()
        elif k == "Left":
            if self.cur > 0:
                self.cur, self.focus = self.cur - 1, 1
        elif k == "Down":
            self.focus = min(self.focus + 1, n + 2)
        elif k == "Up":
            self.focus = max(self.focus - 1, 1)
        elif not q["multiSelect"]:
            if k.isdigit() and 1 <= int(k) <= n:
                self.single[self.cur] = q["options"][int(k) - 1]["label"]
                self.ms_next()
        elif k == "Enter":
            if self.focus == n + 2:
                self.ms_next()
            elif 1 <= self.focus <= n:
                self.ms_toggle(self.focus)
        elif k.isdigit() and 1 <= int(k) <= n and k != self.stuck:
            self.ms_toggle(int(k))

    def ms_toggle(self, k):
        o = self.order[self.cur]
        if k in o:
            o.remove(k)
        else:
            o.append(k)

    def draw(self):
        if self.state in ("ms", "ms-review", "ms-submitted"):
            sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(self.ms_lines()))
            sys.stdout.flush()
            return
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
        if st in ("ms", "ms-review", "ms-submitted"):
            return self.ms_key(k)
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


ARROWS = {"\x1b[A": "Up", "\x1b[B": "Down", "\x1b[C": "Right", "\x1b[D": "Left",
          "\x1bOA": "Up", "\x1bOB": "Down", "\x1bOC": "Right", "\x1bOD": "Left"}


def main(argv):
    d, state = argv[0], argv[1]
    with open(os.path.join(d, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    t = Tui(d, state)
    if state.startswith("form"):
        t.form_init()
    if state.startswith("msel-"):
        t.ms_init()
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
                arrow = ARROWS.get(text[i:i + 3]) if t.state.startswith("ms") else None
                if arrow:
                    t.key(arrow)
                    i += 3
                    continue
                if ch == "\t" and t.state.startswith("ms"):
                    t.key("Tab")
                elif ch == "\x1b":
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
