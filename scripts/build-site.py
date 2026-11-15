#!/usr/bin/env python3
"""scripts/build-site.py — build the static GitHub Pages site (stdlib only).

  python3 scripts/build-site.py OUTDIR

  OUTDIR/index.html          a short landing page: what it is, the screenshots, links
  OUTDIR/demo.html           artifact/board.html with the demo adapter forced on
  OUTDIR/assets/screenshots  docs/assets/screenshots/*.webp (scripts/screenshots.py makes them)
  OUTDIR/icon.svg, favicon.png, apple-touch-icon.png, .nojekyll

The demo never reaches a server: its board-mode meta tag is "demo" (the page boots the demo
adapter before anything else, so no fetch to /api and no claude.use), and its CSP has
connect-src 'none'. It also loads no third-party fonts (system fallbacks), so the page makes
no request beyond its own origin.
"""
import html
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
SHOTS_DIR = os.path.join(REPO, "docs", "assets", "screenshots")
REPO_URL = "https://github.com/osisdie/agent-switchboard"

# The same small reset board-serve and the Artifact viewer put around the page.
RESET_CSS = (":root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
             "padding-bottom:env(safe-area-inset-bottom,0px)}"
             "body{margin:0;font:14px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}"
             "img{max-width:100%}[hidden]{display:none!important}")
FONT_LINES = re.compile(r'^<link rel="(?:preconnect|stylesheet)" href="https://fonts\.(?:googleapis|gstatic)\.com[^\n]*\n', re.M)


def shots():
    """[(name, alt)] in gallery order, from scripts/screenshots.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("screenshots", os.path.join(REPO, "scripts", "screenshots.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [(name, v[0]) for name, v in mod.SHOTS.items()]


def demo_html(keep_fonts=False):
    """board.html wrapped as a stand-alone page in demo mode."""
    with open(os.path.join(REPO, "artifact", "board.html"), encoding="utf-8") as fh:
        # board.html's own charset line (for a page opened from disk): the head below declares it.
        body = re.sub(r"\A<meta charset=\"utf-8\">[^\n]*\n", "", fh.read(), count=1)
    fonts = ""
    if keep_fonts:
        fonts = " https://fonts.googleapis.com"
    else:
        body = FONT_LINES.sub("", body)
    csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'%s; img-src 'self' data:; "
           "font-src%s; connect-src 'none'; base-uri 'none'; form-action 'none'"
           % (fonts, " https://fonts.gstatic.com" if keep_fonts else " 'none'"))
    head = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta http-equiv="Content-Security-Policy" content="%s">'
            '<meta name="board-mode" content="demo"><meta name="robots" content="noindex">'
            '<style>%s</style>\n' % (html.escape(csp), RESET_CSS))
    return head + body + "\n</html>\n"


INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard: every Claude Code session on one panel</title>
<meta name="description" content="A self-hosted board for resident Claude Code sessions: answer decisions and permission prompts, see who is working, send notes. Static demo with sample data.">
<link rel="icon" href="icon.svg" type="image/svg+xml"><link rel="icon" href="favicon.png" sizes="32x32" type="image/png">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'">
<style>
:root { color-scheme: light; --paper: #edf0f4; --panel: #fbfcfd; --ink: #16202c; --muted: #586477; --rule: #d3d9e2; --cord: #1d5bb8; --cord-ink: #fff; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --paper: #0f141a; --panel: #161d25; --ink: #e5eaf1; --muted: #98a4b5; --rule: #2a3440; --cord: #7aa9ff; --cord-ink: #0b1220; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 0 max(16px, env(safe-area-inset-right, 0px)) 48px max(16px, env(safe-area-inset-left, 0px)); }
main { max-width: 1120px; margin-inline: auto; }
header { display: flex; flex-wrap: wrap; gap: 16px 24px; align-items: center; padding: 40px 0 8px; }
header img { width: 64px; height: 64px; }
h1 { margin: 0; font-size: clamp(32px, 6vw, 48px); line-height: 1.05; letter-spacing: -.01em; }
.lede { margin: 12px 0 0; max-width: 68ch; color: var(--muted); font-size: 18px; }
.cta { display: flex; flex-wrap: wrap; gap: 10px; margin: 24px 0 8px; }
.cta a { display: inline-flex; align-items: center; min-height: 44px; padding: 8px 18px; border-radius: 8px; border: 1px solid var(--rule); background: var(--panel); color: var(--ink); font-weight: 600; text-decoration: none; }
.cta a.primary { background: var(--cord); border-color: var(--cord); color: var(--cord-ink); }
.cta a:hover { border-color: var(--cord); }
.points { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%%, 240px), 1fr)); gap: 12px; margin: 28px 0; padding: 0; list-style: none; }
.points li { padding: 14px 16px; border: 1px solid var(--rule); border-radius: 8px; background: var(--panel); }
.points b { display: block; margin-bottom: 2px; }
h2 { font-size: 24px; margin: 36px 0 12px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%%, 340px), 1fr)); gap: 16px; }
figure { margin: 0; border: 1px solid var(--rule); border-radius: 8px; background: var(--panel); overflow: hidden; }
figure a { display: block; aspect-ratio: 16 / 10; background: var(--paper); }
figure img { display: block; width: 100%%; height: 100%%; object-fit: cover; object-position: top; }
figcaption { padding: 10px 12px; font-size: 14px; color: var(--muted); }
footer { margin-top: 40px; font-size: 14px; color: var(--muted); }
a { color: var(--cord); }
:focus-visible { outline: 2px solid var(--cord); outline-offset: 2px; }
</style></head>
<body><main>
<header><img src="icon.svg" alt="" width="64" height="64"><div>
<h1>Switchboard</h1>
<p class="lede">Every resident Claude Code session on one panel. Answer decisions and permission prompts from your phone, see who is working and on what, and send a note to any session. Self-hosted, or as a claude.ai Artifact.</p>
</div></header>
<div class="cta">
<a class="primary" href="demo.html">Open the live demo</a>
<a href="%(repo)s">Source on GitHub</a>
<a href="%(repo)s/blob/main/README.md">README</a>
<a href="%(repo)s/blob/main/docs/contract.md">Contract</a>
</div>
<ul class="points">
<li><b>Decisions</b>Questions, permission prompts and lifecycle gates, answered on the board and relayed to the session.</li>
<li><b>Sessions</b>One card per session: state, task title, repo and branch, what it is running now.</li>
<li><b>Alerts</b>Slack, desktop, email and LINE with escalation, mutes and quiet hours.</li>
<li><b>Owner away</b>Sessions settle cross-session questions by explicit consensus; red lines wait for you.</li>
</ul>
<h2>Screenshots</h2>
<div class="gallery">
%(figures)s
</div>
<footer>The demo runs entirely in your browser with sample data: nothing is sent anywhere. <a href="%(repo)s">%(repo_short)s</a></footer>
</main></body></html>
"""


def index_html():
    figs = []
    for name, alt in shots():
        src = "assets/screenshots/%s.webp" % name
        figs.append('<figure><a href="%s"><img src="%s" alt="%s" loading="lazy"></a><figcaption>%s</figcaption></figure>'
                    % (src, src, html.escape(alt), html.escape(alt)))
    return INDEX % {"repo": REPO_URL, "repo_short": REPO_URL.replace("https://", ""), "figures": "\n".join(figs)}


def build(out):
    os.makedirs(os.path.join(out, "assets", "screenshots"), exist_ok=True)
    with open(os.path.join(out, "demo.html"), "w", encoding="utf-8") as fh:
        fh.write(demo_html())
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(index_html())
    missing = []
    for name, _alt in shots():
        src = os.path.join(SHOTS_DIR, name + ".webp")
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out, "assets", "screenshots", name + ".webp"))
        else:
            missing.append(name)
    for f in ("icon.svg", "favicon.png", "apple-touch-icon.png"):
        shutil.copyfile(os.path.join(REPO, "artifact", f), os.path.join(out, f))
    open(os.path.join(out, ".nojekyll"), "w").close()
    if missing:
        sys.exit("build-site: missing screenshots (run scripts/screenshots.py): %s" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    sys.exit(build(sys.argv[1]))
