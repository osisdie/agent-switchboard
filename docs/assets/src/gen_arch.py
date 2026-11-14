"""Generate docs/assets/architecture-{light,dark}.svg. Usage: python3 docs/assets/src/gen_arch.py"""
import sys
import gen_common as C
from gen_common import text, extruded, chip, HUES, tw

W, H = 1600, 1000
D = 10


def plane(x, y, w, h, T, key, label, label_right=False, label_w=None):
    out = [extruded(x, y, w, h, 16, f"url(#pl-{key})", T[f"plane_{key}_side"], T[f"plane_{key}_stroke"], rx=22, sw=2)]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="url(#dots)" opacity="0.8"/>')
    lw = label_w or tw(label, 18, bold=True) + 0.6 * len(label) + 30
    lx = x + w - lw - 18 if label_right else x + 18
    out.append(f'<rect x="{lx}" y="{y-17}" width="{lw}" height="34" rx="17" fill="{T[f"plane_{key}_stroke"]}"/>')
    out.append(text(lx + lw / 2, y + 6, label, 18, "#ffffff", weight="800", anchor="middle", extra='letter-spacing="0.6"'))
    return "\n".join(out)


def node(x, y, w, h, T, hue, title, lines=(), mono_title=False, tsize=21, stack=0):
    top, bot, side = HUES[hue]
    out = []
    for k in range(stack, 0, -1):
        out.append(extruded(x + 14 * k, y - 12 * k, w, h, D, "url(#front)", side, top, sw=2, shadow=(k == stack)))
    out.append(extruded(x, y, w, h, D, "url(#front)", side, top, sw=2.5, shadow=(stack == 0)))
    # accent strip on the left
    out.append(f'<path d="M{x} {y+14} a14 14 0 0 1 14 -14 H{x+w-14} a14 14 0 0 1 14 14 V{y+10} H{x} Z" fill="{top}"/>')
    out.append(text(x + 16, y + 44, title, tsize, T["text"], weight="800", mono=mono_title))
    yy = y + 76
    for ln in lines:
        if isinstance(ln, tuple) and ln[0] == "chip":
            c, _ = chip(x + 14, yy - 26, ln[1], T, size=18, h=36)
            out.append(c)
            yy += 44
        elif isinstance(ln, tuple) and ln[0] == "mono":
            out.append(text(x + 16, yy, ln[1], 18, T["muted"], mono=True, weight="600"))
            yy += 28
        else:
            out.append(text(x + 16, yy, ln, 18, T["muted"]))
            yy += 28
    return "\n".join(out)


def cylinder(x, y, w, h, T, hue, title, lines):
    top, bot, side = HUES[hue]
    ry = 18
    out = [f'<ellipse cx="{x+w/2+8}" cy="{y+h+12}" rx="{w/2+6}" ry="{ry+6}" fill="{T["shadow"]}" fill-opacity="{T["shadow_op"]}" filter="url(#soft)"/>']
    out.append(f'<path d="M{x} {y+ry} V{y+h} A{w/2} {ry} 0 0 0 {x+w} {y+h} V{y+ry}" fill="url(#cyl-{hue})" stroke="{top}" stroke-width="2.5"/>')
    out.append(f'<ellipse cx="{x+w/2}" cy="{y+ry}" rx="{w/2}" ry="{ry}" fill="{T["front1"]}" stroke="{top}" stroke-width="2.5"/>')
    out.append(text(x + w / 2, y + 74, title, 21, T["text"], weight="800", anchor="middle", mono=True))
    yy = y + 106
    for ln in lines:
        out.append(text(x + w / 2, yy, ln, 18, T["muted"], anchor="middle", mono=True, weight="600"))
        yy += 28
    return "\n".join(out)


def badge(x, y, n, T):
    return (f'<circle cx="{x}" cy="{y}" r="16" fill="{T["primary"]}" stroke="{T["bg1"]}" stroke-width="3"/>'
            + text(x, y + 6.5, str(n), 18, T["badge_text"], weight="900", anchor="middle"))


def arrow(d, T, primary=True, width=None):
    col = T["primary"] if primary else T["secondary"]
    m = "prim" if primary else "sec"
    w = width or (5 if primary else 3.5)
    halo = (f'<path d="{d}" fill="none" stroke="{T["bg1"]}" stroke-width="{w+6}" stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>'
            if primary else "")
    return halo + f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{w}" stroke-linejoin="round" marker-end="url(#m-{m})"/>'


def person(x, y, col):
    return (f'<circle cx="{x}" cy="{y-9}" r="10" fill="{col}"/>'
            f'<path d="M{x-17} {y+19} a17 15 0 0 1 34 0 Z" fill="{col}"/>')


def build(theme):
    T = C.THEMES[theme]
    C.SHADOW[:] = [T["shadow"], T["shadow_op"]]
    b = []
    b.append(text(48, 54, "agent-switchboard: architecture", 32, T["text"], weight="800"))
    b.append(text(48, 84, "Zero-token plumbing on your machine; one LLM operator talks to the cloud board.", 19, T["muted"]))
    # legend
    lx = 960
    b.append(f'<path d="M{lx} 60 H{lx+46}" stroke="{T["primary"]}" stroke-width="5" marker-end="url(#m-prim)"/>')
    b.append(text(lx + 56, 66, "decision path", 18, T["text"]))
    lx2 = lx + 56 + tw("decision path", 18) + 24
    b.append(f'<path d="M{lx2} 60 H{lx2+40}" stroke="{T["secondary"]}" stroke-width="3.5" marker-end="url(#m-sec)"/>')
    b.append(text(lx2 + 50, 66, "other flow", 18, T["text"]))
    lx3 = lx2 + 50 + tw("other flow", 18) + 24
    b.append(f'<path d="M{lx3} 60 H{lx3+40}" stroke="{T["boundary"]}" stroke-width="3" stroke-dasharray="8 6"/>')
    b.append(text(lx3 + 50, 66, "trust boundary", 18, T["text"]))

    # ---- step list
    sx, sy, sw, sh = 48, 112, 340, 312
    b.append(extruded(sx, sy, sw, sh, D, "url(#front)", T["primary_halo"], T["primary"], sw=2.5))
    b.append(text(sx + 20, sy + 40, "Decision path", 22, T["text"], weight="800"))
    b.append(text(sx + 20, sy + 66, "from blocked worker to answer", 18, T["muted"]))
    steps = ["hook writes needs-you", "collector → snapshot.json", "operator write_db → board",
             "you answer on the board", "operator read_db (~3 min)", "relay: SendMessage / press"]
    for i, s in enumerate(steps):
        yy = sy + 108 + i * 37
        b.append(badge(sx + 34, yy - 6, i + 1, T))
        b.append(text(sx + 60, yy, s, 18, T["text"], weight="500"))

    # ---- planes
    cpx, cpy, cpw, cph = 420, 214, 700, 210
    b.append(plane(cpx, cpy, cpw, cph, T, "cloud", "CLOUD · claude.ai"))
    epx, epw = 1160, 392
    b.append(plane(epx, cpy, epw, cph, T, "ext", "EXTERNAL · alert channels"))
    lpx, lpy, lpw, lph = 36, 486, 1520, 474
    b.append(plane(lpx, lpy, lpw, lph, T, "local", "LOCAL MACHINE", label_right=True))

    # trust boundary
    by = 456
    b.append(f'<path d="M30 {by} H1570" stroke="{T["boundary"]}" stroke-width="3" stroke-dasharray="12 8"/>')
    tl = "TRUST BOUNDARY"
    tlw = tw(tl, 18, bold=True) + len(tl) + 36
    b.append(f'<rect x="{48}" y="{by-17}" width="{tlw}" height="34" rx="17" fill="{T["bg1"]}" stroke="{T["boundary"]}" stroke-width="2.5"/>')
    b.append(text(48 + tlw / 2, by + 6, tl, 18, T["boundary"], weight="900", anchor="middle", extra='letter-spacing="1"'))

    # ---- cloud: board
    bx, byy, bw, bh = 452, 246, 650, 150
    b.append(node(bx, byy, bw, bh, T, "indigo", "Artifact board", ["db · user · room · phone-friendly"]))
    b.append(text(bx + bw - 16, byy + 44, "artifact/board.html", 18, T["muted"], mono=True, weight="600", anchor="end"))
    cxp = bx + 16
    for lab in ("decisions/", "tasks/", "approvals/", "messages/"):
        c, w = chip(cxp, byy + 96, lab, T, size=18, h=36)
        b.append(c)
        cxp += w + 12

    # ---- external channels
    ex, ey = epx + 26, cpy + 40
    rows = [["Slack", "email", "LINE"], ["SNS", "webhook"]]
    for r, row in enumerate(rows):
        xx = ex
        for lab in row:
            w = tw(lab, 19, bold=True) + 30
            b.append(extruded(xx, ey + r * 70, w, 46, 6, "url(#front)", HUES["violet"][2], HUES["violet"][0], rx=10, sw=2, shadow=False))
            b.append(text(xx + w / 2, ey + r * 70 + 30, lab, 19, T["text"], weight="700", anchor="middle"))
            xx += w + 14
    b.append(text(epx + 26, cpy + 192, "on when its env vars are set", 18, T["muted"]))

    # ---- human
    hx, hy, hw, hh = 1200, 104, 340, 72
    b.append(extruded(hx, hy, hw, hh, 8, "url(#front)", HUES["amber"][2], HUES["amber"][0], rx=36, sw=2.5))
    b.append(person(hx + 42, hy + 36, HUES["amber"][0]))
    b.append(text(hx + 74, hy + 32, "Human approver", 21, T["text"], weight="800"))
    b.append(text(hx + 74, hy + 58, "on the board or a phone", 18, T["muted"]))

    # ---- local row 1
    r1, nh = 548, 150
    b.append(node(60, r1, 230, nh, T, "slate", "Worker sessions", ["Claude Code in tmux", ("chip", "API · WEB · …")], stack=2))
    b.append(node(430, r1, 260, nh, T, "slate", "bin/board", ["ask · answer · press", "keys only if guards pass"], mono_title=True))
    b.append(node(800, r1, 350, nh, T, "amber", "Operator session BOARD", ["LLM session · Remote Control", ("chip", "prompts/board-operator.md")]))
    # ---- local row 2
    r2 = 784
    b.append(node(60, r2, 260, nh, T, "build", "hooks/board-hook.sh", ["10 lifecycle events", "exit 0 · never blocks"], mono_title=True, tsize=19))
    b.append(cylinder(420, r2 - 8, 240, nh + 4, T, "build", "$BOARD_HOME", ["state/ decisions/", "events.jsonl"]))
    b.append(node(760, r2, 270, nh, T, "build", "bin/board-collect", ["bash loop · 0 tokens", ("chip", "snapshot.json")], mono_title=True, tsize=19))
    b.append(node(1120, r2, 270, nh, T, "violet", "lib/notify.py", ["via bin/board-notify", "cooldown · escalation"], mono_title=True, tsize=19))
    b.append(text(1420, r2 + 60, "0 tokens:", 18, T["text"], weight="800"))
    b.append(text(1420, r2 + 88, "keeps alerting", 18, T["muted"]))
    b.append(text(1420, r2 + 114, "while the", 18, T["muted"]))
    b.append(text(1420, r2 + 140, "operator is", 18, T["muted"]))
    b.append(text(1420, r2 + 166, "rate-limited", 18, T["muted"]))

    # ---- edges: secondary first, primary on top
    # workers -> bin/board (ask)
    b.append(arrow(f"M{330+2} {r1+110} H{424}", T, primary=False))
    b.append(text(380, r1 + 100, "ask", 18, T["muted"], anchor="middle", weight="600"))
    # bin/board -> $BOARD_HOME (decisions/)
    b.append(arrow(f"M550 {r1+nh+D+2} V{r2-14}", T, primary=False))
    b.append(text(562, r1 + nh + 52, "decisions/", 18, T["muted"], mono=True, weight="600"))
    # collector -> notify
    b.append(arrow(f"M{1030+D+2} {r2+80} H{1114}", T, primary=False))
    # notify -> channels (cross boundary)
    b.append(arrow(f"M1200 {r2-2} V{cpy+cph+22}", T, primary=False))
    b.append(text(1214, 720, "needs-you", 18, T["muted"], weight="600"))
    b.append(text(1214, 744, "usage limit", 18, T["muted"], weight="600"))
    # channels -> human
    b.append(arrow(f"M1525 {cpy-2} V{hy+hh+D+6}", T, primary=False))
    b.append(text(1537, 204, "push", 18, T["muted"], weight="600"))

    # primary: workers -> hook
    b.append(arrow(f"M180 {r1+nh+D+2} V{r2-10}", T))
    b.append(badge(180, r1 + nh + 46, 1, T))
    b.append(text(205, r1 + nh + 52, "events", 18, T["text"], weight="600"))
    # hook -> $BOARD_HOME
    b.append(arrow(f"M{320+D+2} {r2+75} H{414}", T))
    # $BOARD_HOME -> collector
    b.append(arrow(f"M{660+6} {r2+75} H{754}", T))
    b.append(badge(708, r2 + 75, 2, T))
    # collector -> operator (snapshot, Monitor)
    b.append(arrow(f"M895 {r2-2} V{r1+nh+D+8}", T))
    b.append(text(910, r1 + nh + 52, "Monitor", 18, T["text"], weight="600"))
    # operator <-> board
    b.append(arrow(f"M960 {r1-4} V{byy+bh+D+8}", T))
    b.append(badge(960, 486, 3, T))
    b.append(text(944, 524, "write_db", 18, T["text"], weight="700", anchor="end", mono=True))
    b.append(arrow(f"M1076 {byy+bh+D+4} V{r1-10}", T))
    b.append(badge(1076, 486, 5, T))
    b.append(text(1062, 524, "read_db", 18, T["text"], weight="700", anchor="end", mono=True))
    # human -> board (answers)
    b.append(arrow(f"M{hx-4} {hy+36} H1060 Q1040 {hy+36} 1040 {hy+56} V{byy-8}", T))
    b.append(badge(1100, hy + 36, 4, T))
    b.append(text(1024, hy + 76, "answers,", 18, T["text"], weight="600", anchor="end"))
    b.append(text(1024, hy + 100, "@mentions", 18, T["text"], weight="600", anchor="end"))
    # operator -> workers (SendMessage) routed above row 1
    b.append(arrow(f"M840 {r1-4} V522 Q840 508 826 508 H194 Q180 508 180 522 V{r1-30}", T))
    b.append(badge(740, 508, 6, T))
    b.append(text(220, 500, "SendMessage [BOARD decision …]", 18, T["text"], weight="600"))
    # operator -> bin/board (press)
    b.append(arrow(f"M798 {r1+56} H{690+D+6}", T))
    b.append(text(748, r1 + 44, "press", 18, T["text"], weight="600", anchor="middle"))
    # bin/board -> workers (send-keys)
    b.append(arrow(f"M428 {r1+56} H{330+4}", T))
    b.append(text(380, r1 + 44, "keys", 18, T["text"], weight="600", anchor="middle"))

    extra = []
    for key in ("local", "cloud", "ext"):
        extra.append(f'<linearGradient id="pl-{key}" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" stop-color="{T[f"plane_{key}1"]}"/>'
                     f'<stop offset="1" stop-color="{T[f"plane_{key}2"]}"/></linearGradient>')
    top, bot, side = HUES["build"]
    extra.append(f'<linearGradient id="cyl-build" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{T["front2"]}"/>'
                 f'<stop offset="0.45" stop-color="{T["front1"]}"/><stop offset="1" stop-color="{side}" stop-opacity="0.55"/></linearGradient>')
    desc = ("Architecture. Your machine: worker sessions (Claude Code in tmux) fire lifecycle events into "
            "hooks/board-hook.sh, which writes state files under $BOARD_HOME; bin/board-collect (bash, zero tokens) "
            "builds snapshot.json and calls lib/notify.py for Slack, email, LINE, SNS or webhook alerts. "
            "The operator session BOARD (an LLM) watches the snapshot and writes it to the Artifact board on claude.ai "
            "(write_db) across the trust boundary. A human answers on the board; the operator reads the answer (read_db) "
            "and relays it with SendMessage or a guarded bin/board press that sends keys to the worker's tmux pane.")
    return C.doc(W, H, T, "agent-switchboard architecture", desc, "\n".join(b), "\n".join(extra))


if __name__ == "__main__":
    import os
    # default: docs/assets/ (the parent of this src/ directory); optional first arg overrides
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    for th in ("light", "dark"):
        open(f"{out}/architecture-{th}.svg", "w").write(build(th))
