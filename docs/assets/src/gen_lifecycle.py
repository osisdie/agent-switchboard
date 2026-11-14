"""Generate docs/assets/lifecycle-gates-{light,dark}.svg. Usage: python3 docs/assets/src/gen_lifecycle.py"""
import sys
import gen_common as C
from gen_common import text, extruded, chip, gate_pill, lock, HUES, tw

W, H = 1600, 970
CX0, CW, GAP, CY, CH, D = 64, 271, 34, 184, 214, 10

STAGES = [
    ("plan", "1 · Plan", "questions first", "intent.md", "owner approves"),
    ("design", "2 · Design", "optional: only if needed", "spec.md", "owner signs off"),
    ("build", "3 · Build", "plan, then code", "plan.md + code", "engineer / tech lead"),
    ("test", "4 · Test", "tests + evals", "evals/", "code owner reviews"),
    ("deploy", "5 · Deploy", "merge · staging · smoke", "review-findings.md", "code owner"),
]


def cx(i):
    return CX0 + i * (CW + GAP)


def header(x, y, w, hue, label, tag=None):
    top, bot, _ = HUES[hue]
    gid = f"h-{hue}"
    out = [f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
           f'<stop offset="1" stop-color="{bot}"/></linearGradient>']
    out.append(f'<path d="M{x} {y+52} V{y+14} a14 14 0 0 1 14 -14 H{x+w-14} a14 14 0 0 1 14 14 V{y+52} Z" fill="url(#{gid})"/>')
    out.append(f'<path d="M{x} {y+52} V{y+14} a14 14 0 0 1 14 -14 H{x+w-14} a14 14 0 0 1 14 14 V{y+52} Z" fill="url(#gloss)"/>')
    out.append(text(x + 16, y + 35, label, 25, "#ffffff", weight="800"))
    if tag:
        tw_ = tw(tag, 18) + 20
        out.append(f'<rect x="{x+w-tw_-12}" y="{y+12}" width="{tw_}" height="28" rx="14" fill="#ffffff" fill-opacity="0.22" stroke="#ffffff" stroke-opacity="0.7" stroke-dasharray="4 3"/>')
        out.append(text(x + w - 12 - tw_ / 2, y + 32, tag, 18, "#ffffff", weight="700", anchor="middle"))
    return "\n".join(out)


def stage_card(i, T):
    hue, label, sub, art, gate = STAGES[i]
    x, y = cx(i), CY
    side = HUES[hue][2]
    optional = hue == "design"
    out = [extruded(x, y, CW, CH, D, "url(#front)", side, HUES[hue][0], dash="9 6" if optional else None, sw=2.5)]
    out.append(header(x, y, CW, hue, label))
    out.append(text(x + 16, y + 82, sub, 18, T["muted"], weight="500"))
    c, _ = chip(x + 14, y + 98, art, T)
    out.append(c)
    out.append(gate_pill(x + 12, y + 156, gate, T, CW - 24))
    return "\n".join(out)


def build(theme):
    T = C.THEMES[theme]
    C.SHADOW[:] = [T["shadow"], T["shadow_op"]]
    b = []
    # ---- title + legend
    b.append(text(80, 54, "Agent-ready SDLC: lifecycle and gates", 32, T["text"], weight="800"))
    b.append(text(80, 84, "Every stage commits an artifact and stops at a named human gate.", 19, T["muted"]))
    lx = 900
    c, w = chip(lx, 42, "file.md", T, size=18, h=34)
    b.append(c)
    b.append(text(lx + w + 12, 66, "committed artifact", 18, T["text"]))
    lx2 = lx + w + 12 + tw("committed artifact", 18) + 26
    b.append(extruded(lx2, 42, 36, 34, 3, T["gate_bg"], T["gate_side"], T["gate_stroke"], rx=17, shadow=False))
    b.append(lock(lx2 + 8.5, 47, T["lock"]))
    b.append(text(lx2 + 46, 66, "human gate", 18, T["text"]))
    lx3 = lx2 + 46 + tw("human gate", 18) + 26
    b.append(f'<path d="M{lx3} 59 H{lx3+44}" stroke="{T["dashed"]}" stroke-width="3" stroke-dasharray="7 5" marker-end="url(#m-dash)"/>')
    b.append(text(lx3 + 54, 66, "alt. path", 18, T["text"]))

    # ---- ribbon A: commit chain
    rx0, rx1 = CX0, cx(4) + CW
    b.append(extruded(rx0, 102, rx1 - rx0, 44, 5, T["ribA_bg"], T["ribA_side"], T["ribA_stroke"], rx=22, shadow=False))
    gx, gy = rx0 + 26, 124
    b.append(f'<path d="M{gx-8} {gy} H{gx+28}" stroke="{T["ribA_text"]}" stroke-width="3"/>'
             f'<circle cx="{gx+10}" cy="{gy}" r="7" fill="{T["ribA_bg"]}" stroke="{T["ribA_text"]}" stroke-width="3"/>')
    b.append(f'<text x="{gx+44}" y="131" font-size="20" fill="{T["ribA_text"]}"><tspan font-weight="800">'
             f'Each stage reads the previous stage&apos;s last commit</tspan><tspan font-weight="500">'
             f' · nothing runs from memory or from an uncommitted draft</tspan></text>')
    # commit chain beads on the card tops
    xs = [cx(i) + CW / 2 for i in range(5)]
    for a, z in zip(xs, xs[1:]):
        b.append(f'<path d="M{a+12} 163 H{z-14}" stroke="{T["ribA_stroke"]}" stroke-width="3" marker-end="url(#m-arrow)" opacity="0.9"/>')
    for i, x in enumerate(xs):
        b.append(f'<circle cx="{x}" cy="163" r="8" fill="{T["bg1"]}" stroke="{HUES[STAGES[i][0]][0]}" stroke-width="4"/>')

    # ---- stage cards + forward arrows
    for i in range(5):
        b.append(stage_card(i, T))
    ay = CY + 110
    for i in range(4):
        x0, x1 = cx(i) + CW + D + 3, cx(i + 1) - 4
        b.append(f'<path d="M{x0} {ay} H{x1}" stroke="{T["arrow"]}" stroke-width="4" marker-end="url(#m-arrow)"/>')

    # ---- bypass: Plan -> Build skipping Design
    by = CY + CH + D + 4
    p0, p1 = xs[0], xs[2]
    b.append(f'<path d="M{p0} {by} C{p0} {by+120} {p1} {by+120} {p1} {by+6}" fill="none" stroke="{T["dashed"]}" '
             f'stroke-width="3.5" stroke-dasharray="10 7" marker-end="url(#m-dash)"/>')
    mx = (p0 + p1) / 2
    b.append(text(mx, by + 52, "simple fix skips design", 19, T["text"], weight="700", anchor="middle"))
    b.append(text(mx, by + 75, "(owner confirms)", 18, T["muted"], anchor="middle"))

    # ---- request changes loop under Test
    t = cx(3)
    l0, l1 = t + CW - 50, t + 50
    b.append(f'<path d="M{l0} {by} C{l0+30} {by+95} {l1-30} {by+95} {l1} {by+6}" fill="none" stroke="{T["gate_stroke"]}" '
             f'stroke-width="3.5" stroke-dasharray="10 7" marker-end="url(#m-amber)"/>')
    b.append(text(t + CW / 2, by + 104, "request changes", 19, T["text"], weight="700", anchor="middle"))
    b.append(text(t + CW / 2, by + 128, "→ back to the same stage (any gate)", 18, T["muted"], anchor="middle"))

    # ---- production slab
    sx, sy, sw, sh = CX0, 580, cx(4) + CW - CX0, 98
    dep = xs[4]
    b.append(f'<path d="M{dep} {by} V{sy-6}" stroke="{T["arrow"]}" stroke-width="5" marker-end="url(#m-arrow)"/>')
    b.append(extruded(sx, sy, sw, sh, 14, "url(#slab)", T["slab_side"], T["stop"], rx=16, sw=3))
    b.append(f'<rect x="{sx+18}" y="{sy+8}" width="{sw-36}" height="9" rx="4.5" fill="url(#haz)" opacity="0.95"/>')
    # stop octagon
    ox, oy, r = sx + 58, sy + 56, 30
    import math
    pts = " ".join(f"{ox + r*math.cos(math.radians(22.5+45*k)):.1f},{oy + r*math.sin(math.radians(22.5+45*k)):.1f}" for k in range(8))
    b.append(f'<polygon points="{pts}" fill="{T["stop"]}" stroke="#ffffff" stroke-width="3"/>'
             f'<rect x="{ox-15}" y="{oy-4.5}" width="30" height="9" rx="2" fill="#ffffff"/>')
    b.append(f'<text x="{sx+106}" y="{sy+56}" fill="{T["slab_text"]}"><tspan font-size="30" font-weight="900" letter-spacing="1">'
             f'PRODUCTION GATE</tspan><tspan font-size="26" font-weight="700" fill="{T["slab_accent"]}"> · the agent stops here</tspan></text>')
    b.append(text(sx + 106, sy + 84, "peer sessions never count · no release manager configured → refused (fail closed)",
                  19, T["slab_muted"]))
    pw = tw("release manager authorizes", 19, bold=True) + 56
    b.append(gate_pill(sx + sw - pw - 28, sy + 30, "release manager authorizes", T, pw, h=44, size=19))

    # ---- row 3: approvals ribbon + Maintain
    ry = 736
    mw = 540
    mx0 = cx(4) + CW - mw
    rbw = mx0 - 50 - CX0
    b.append(extruded(CX0, ry, rbw, 140, 10, T["ribB_bg"], T["ribB_side"], T["ribB_stroke"], rx=18, sw=2))
    b.append(text(CX0 + 28, ry + 42, "Every human approval is recorded → approvals log", 23, T["ribB_text"], weight="800"))
    b.append(text(CX0 + 28, ry + 76, "## Approvals section · commit trailer · board approvals/ (append-only)", 18, T["ribB_text"]))
    b.append(text(CX0 + 28, ry + 108, 'events.jsonl ← {"type": "approval", "result": "approved|rejected"}', 18, T["ribB_text"], mono=True))
    # stacked log sheets
    kx, ky = CX0 + rbw - 120, ry + 26
    for k in range(3, -1, -1):
        b.append(f'<rect x="{kx + k*7}" y="{ky + k*7}" width="70" height="84" rx="6" fill="{T["front1"]}" stroke="{T["ribB_stroke"]}" stroke-width="2"/>')
    for k in range(4):
        b.append(f'<rect x="{kx+12}" y="{ky+16+k*14}" width="{46 - (k%2)*14}" height="5" rx="2.5" fill="{T["ribB_stroke"]}" opacity="0.7"/>')
    b.append(lock(kx + 44, ky + 62, T["lock"], 1.05))

    b.append(f'<path d="M{xs[4]} {sy+sh+16} V{ry-8}" stroke="{T["arrow"]}" stroke-width="5" marker-end="url(#m-arrow)"/>')
    side = HUES["maintain"][2]
    b.append(extruded(mx0, ry, mw, 132, D, "url(#front)", side, HUES["maintain"][0], sw=2.5))
    b.append(header(mx0, ry, mw, "maintain", "6 · Maintain"))
    b.append(text(mx0 + mw - 16, ry + 34, "operate · triage", 18, "#ffffff", weight="600", anchor="end"))
    c, cw_ = chip(mx0 + 14, ry + 70, "new intent.md", T)
    b.append(c)
    b.append(gate_pill(mx0 + 14 + cw_ + 14, ry + 69, "service owner triages", T, mw - cw_ - 42))

    # ---- loop back to Plan
    lc = HUES["maintain"][0]
    lx_ = mx0 + 120
    yb, xl, yp = ry + 132 + D + 30, 42, CY + 110
    b.append(f'<path d="M{lx_} {ry+132+D+4} V{yb-18} Q{lx_} {yb} {lx_-18} {yb} H{xl+18} Q{xl} {yb} {xl} {yb-18} '
             f'V{yp+18} Q{xl} {yp} {xl+18} {yp} H{CX0-6}" fill="none" stroke="{lc}" stroke-width="5" marker-end="url(#m-loop)"/>')
    b.append(text(CX0 + 10, yb + 32, "loop back to Plan: the new intent.md starts the next cycle", 19, T["text"], weight="700"))

    extra = (f'<linearGradient id="slab" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{T["slab1"]}"/>'
             f'<stop offset="1" stop-color="{T["slab2"]}"/></linearGradient>'
             f'<pattern id="haz" width="22" height="22" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
             f'<rect width="11" height="22" fill="#f59e0b"/><rect x="11" width="11" height="22" fill="#111827"/></pattern>')
    desc = ("Lifecycle: Plan (intent.md, owner approves) → Design (optional, spec.md, owner signs off; a simple fix skips "
            "design) → Build (plan.md + code, engineer / tech lead) → Test (evals/, code owner reviews) → Deploy "
            "(review-findings.md, code owner) → Production gate: the agent stops here, release manager authorizes → "
            "Maintain (new intent.md, service owner triages) → loop back to Plan. Request changes keeps the task in the "
            "same stage. Each stage reads the previous stage's last commit; every approval is recorded in the approvals log.")
    return C.doc(W, H, T, "Agent-ready SDLC: lifecycle and gates", desc, "\n".join(b), extra)


if __name__ == "__main__":
    import os
    # default: docs/assets/ (the parent of this src/ directory); optional first arg overrides
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    for th in ("light", "dark"):
        open(f"{out}/lifecycle-gates-{th}.svg", "w").write(build(th))
