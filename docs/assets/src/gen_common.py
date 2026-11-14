"""Shared helpers for hand-built README SVG diagrams (light + dark)."""
from xml.sax.saxutils import escape

SANS = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

THEMES = {
    "light": dict(
        bg1="#f8fafc", bg2="#e9eef5", frame="#d5dde8", dot="#cfd8e3",
        text="#0f172a", muted="#475569", front1="#ffffff", front2="#f1f5f9",
        chip_bg="#eef2f7", chip_stroke="#b8c4d4", chip_text="#0f172a", chip_side="#c3cddb",
        gate_bg="#fef3c7", gate_stroke="#d97706", gate_text="#78350f", lock="#b45309", gate_side="#e9b949",
        arrow="#334155", dashed="#64748b", shadow="#0f172a", shadow_op="0.20",
        slab1="#1f2937", slab2="#0b1220", slab_side="#020617", slab_text="#f8fafc",
        slab_muted="#cbd5e1", slab_accent="#fca5a5", stop="#dc2626",
        ribA_bg="#e0e7ff", ribA_stroke="#818cf8", ribA_text="#1e1b4b", ribA_side="#a5b4fc",
        ribB_bg="#fff7ed", ribB_stroke="#fb923c", ribB_text="#7c2d12", ribB_side="#fdba74",
        plane_local1="#eef3f9", plane_local2="#e2e9f2", plane_local_side="#c6d2e1", plane_local_stroke="#9fb0c6",
        plane_cloud1="#eef2ff", plane_cloud2="#e0e7ff", plane_cloud_side="#c3cbf5", plane_cloud_stroke="#8b9cf0",
        plane_ext1="#f5f3ff", plane_ext2="#ede9fe", plane_ext_side="#d6cdf7", plane_ext_stroke="#a78bfa",
        boundary="#dc2626", primary="#ea580c", primary_halo="#ffedd5", secondary="#64748b",
        badge_text="#ffffff",
    ),
    "dark": dict(
        bg1="#121a27", bg2="#0b111b", frame="#283447", dot="#1e293b",
        text="#e6edf3", muted="#a3b1c4", front1="#1e2738", front2="#161e2c",
        chip_bg="#0b1220", chip_stroke="#3e4e68", chip_text="#e2e8f0", chip_side="#05080f",
        gate_bg="#3b2a08", gate_stroke="#f59e0b", gate_text="#fcd34d", lock="#fbbf24", gate_side="#1f1604",
        arrow="#b6c2d3", dashed="#8d9bb0", shadow="#000000", shadow_op="0.60",
        slab1="#0a0d14", slab2="#020305", slab_side="#3b0d12", slab_text="#f8fafc",
        slab_muted="#cbd5e1", slab_accent="#fca5a5", stop="#ef4444",
        ribA_bg="#1e1b4b", ribA_stroke="#6366f1", ribA_text="#c7d2fe", ribA_side="#0f0d2e",
        ribB_bg="#2b1a07", ribB_stroke="#f59e0b", ribB_text="#fed7aa", ribB_side="#140b02",
        plane_local1="#162031", plane_local2="#111a28", plane_local_side="#070b12", plane_local_stroke="#33435c",
        plane_cloud1="#1a1d3d", plane_cloud2="#141733", plane_cloud_side="#0a0b1f", plane_cloud_stroke="#4f5bd5",
        plane_ext1="#1f1838", plane_ext2="#18122d", plane_ext_side="#0c0818", plane_ext_stroke="#7c5ce0",
        boundary="#f87171", primary="#fb923c", primary_halo="#3a1d08", secondary="#8d9bb0",
        badge_text="#1a0d00",
    ),
}

# stage hue: (gradient top, gradient bottom, extrusion side)
HUES = {
    "plan": ("#2563eb", "#1e40af", "#172f7a"),
    "design": ("#7c3aed", "#5b21b6", "#3f1782"),
    "build": ("#0d9488", "#115e59", "#0a3d3a"),
    "test": ("#16a34a", "#166534", "#0f4223"),
    "deploy": ("#e11d48", "#9f1239", "#6b0c27"),
    "maintain": ("#0284c7", "#075985", "#063d5c"),
    "slate": ("#475569", "#334155", "#1e293b"),
    "indigo": ("#4f46e5", "#3730a3", "#272274"),
    "violet": ("#8b5cf6", "#6d28d9", "#4c1d95"),
    "amber": ("#d97706", "#b45309", "#7c3a06"),
}


def esc(s):
    return escape(s, {'"': "&quot;"})


def tw(s, size, mono=False, bold=False):
    """Rough text width estimate."""
    k = 0.62 if mono else (0.64 if bold else 0.58)
    return len(s) * size * k


def text(x, y, s, size=18, fill="#000", weight=None, anchor=None, mono=False, extra=""):
    a = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'font-size="{size}"', f'fill="{fill}"']
    if mono:
        a.append('class="m"')
    if weight:
        a.append(f'font-weight="{weight}"')
    if anchor:
        a.append(f'text-anchor="{anchor}"')
    if extra:
        a.append(extra)
    return f'<text {" ".join(a)}>{esc(s)}</text>'


SHADOW = ["#000", "0.3"]


def extruded(x, y, w, h, depth, front, side, stroke=None, rx=14, sw=2, shadow=True, dash=None, front_extra=""):
    """A card with a solid extruded body (down-right) and a soft shadow."""
    out = []
    if shadow:
        out.append(f'<rect x="{x+depth+4}" y="{y+depth+10}" width="{w}" height="{h}" rx="{rx}" '
                   f'fill="{SHADOW[0]}" fill-opacity="{SHADOW[1]}" filter="url(#soft)"/>')
    for i in range(depth, 0, -1):
        out.append(f'<rect x="{x+i}" y="{y+i}" width="{w}" height="{h}" rx="{rx}" fill="{side}"/>')
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    if dash:
        st += f' stroke-dasharray="{dash}"'
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{front}"{st}{front_extra}/>')
    return "\n".join(out)


def lock(x, y, color, s=1.0):
    return (f'<g transform="translate({x:.1f},{y:.1f}) scale({s})">'
            f'<path d="M4.5 10 V7 a5 5 0 0 1 10 0 V10" fill="none" stroke="{color}" stroke-width="2.6" stroke-linecap="round"/>'
            f'<rect x="1" y="9.5" width="17" height="12.5" rx="3" fill="{color}"/>'
            f'<circle cx="9.5" cy="15.2" r="1.9" fill="#fff" opacity="0.85"/></g>')


def chip(x, y, label, T, size=19, h=40, w=None):
    w = w or tw(label, size, mono=True) + 26
    out = [extruded(x, y, w, h, 4, T["chip_bg"], T["chip_side"], T["chip_stroke"], rx=8, sw=1.5, shadow=False),
           text(x + 13, y + h / 2 + size * 0.35, label, size, T["chip_text"], weight="600", mono=True)]
    return "\n".join(out), w


def gate_pill(x, y, label, T, w, h=42, size=18):
    out = [extruded(x, y, w, h, 4, T["gate_bg"], T["gate_side"], T["gate_stroke"], rx=h / 2, sw=2, shadow=False),
           lock(x + 12, y + h / 2 - 11, T["lock"], 0.92),
           text(x + 38, y + h / 2 + size * 0.35, label, size, T["gate_text"], weight="600", extra='letter-spacing="-0.3"')]
    return "\n".join(out)


def defs(T, extra=""):
    SHADOW[0], SHADOW[1] = T["shadow"], T["shadow_op"]
    markers = []
    for name, col in (("arrow", T["arrow"]), ("dash", T["dashed"]), ("prim", T["primary"]),
                      ("sec", T["secondary"]), ("loop", HUES["maintain"][0]), ("amber", T["gate_stroke"]),
                      ("bound", T["boundary"])):
        markers.append(
            f'<marker id="m-{name}" viewBox="0 0 12 12" refX="9" refY="6" markerWidth="5" markerHeight="5" '
            f'orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M0,0 L12,6 L0,12 L3,6 z" fill="{col}"/></marker>')
    return f"""<defs>
<linearGradient id="bg" x1="0" y1="0" x2="0.3" y2="1"><stop offset="0" stop-color="{T['bg1']}"/><stop offset="1" stop-color="{T['bg2']}"/></linearGradient>
<linearGradient id="front" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{T['front1']}"/><stop offset="1" stop-color="{T['front2']}"/></linearGradient>
<linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.28"/><stop offset="0.55" stop-color="#ffffff" stop-opacity="0"/></linearGradient>
<pattern id="dots" width="28" height="28" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.3" fill="{T['dot']}"/></pattern>
<filter id="soft" x="-15%" y="-15%" width="130%" height="140%"><feGaussianBlur stdDeviation="9"/></filter>
{''.join(markers)}
{extra}
</defs>"""


def doc(W, H, T, title, desc, body, extra_defs=""):
    style = f"<style>text{{font-family:{SANS};}} text.m{{font-family:{MONO};}}</style>"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">
<title id="t">{esc(title)}</title>
<desc id="d">{esc(desc)}</desc>
{style}
{defs(T, extra_defs)}
<rect x="0" y="0" width="{W}" height="{H}" rx="24" fill="url(#bg)"/>
<rect x="0" y="0" width="{W}" height="{H}" rx="24" fill="url(#dots)"/>
<rect x="1" y="1" width="{W-2}" height="{H-2}" rx="23" fill="none" stroke="{T['frame']}" stroke-width="2"/>
{body}
</svg>
"""
