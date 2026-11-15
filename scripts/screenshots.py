#!/usr/bin/env python3
"""scripts/screenshots.py — render the README / Pages screenshots of the board from its demo data.

  python3 scripts/screenshots.py [--out DIR] [--only NAME[,NAME...]] [--list]

Opens the static demo page (scripts/build-site.py: artifact/board.html with the demo adapter
forced on and the same small reset board-serve adds, here with the web fonts) in a headless
Chromium (python playwright) and writes WebP images (converted in the browser, nothing to install) to
docs/assets/screenshots/ by default, each under MAX_BYTES. Only demo data is shown: generic
session codes, example.com, no host names, emails or paths from this machine.

Stable output: the page clock is frozen at DEMO_TIME (UTC, en-US), so relative times and dates
are the same on every run. Chromium's rasterization still varies now and then: a few pixels,
scattered over the page. An existing image is kept when the new render matches it within
SAME_TOLERANCE (git stays clean). A visible change, even one digit, is clustered: it fails the
per-tile cap and is written. Google Fonts load when the network allows; offline, the fallback
fonts are used (the images then differ slightly).

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed.
"""
import argparse
import base64
import json
import datetime as dt
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
OUT = os.path.join(REPO, "docs", "assets", "screenshots")
DEMO_TIME = dt.datetime(2026, 1, 15, 10, 30, tzinfo=dt.timezone.utc)
MAX_BYTES = 200 * 1024
# When is a new render "the same" as the image on disk? Measured on this page: rasterization noise
# moves at most ~16 pixels by more than `big` within any 16x16 tile at 2x (3 at 1x), scattered
# over the page; a one-digit edit moves 19+ (1x) or 49+ (2x) pixels in one tile. So: every tile
# under its cap, and only a sliver of the page different at all.
SAME_TOLERANCE = {
    "big": 24,             # a channel difference above this is a "big" pixel
    "tile": 16,            # tile size in image pixels
    "tile_cap": 10,        # big pixels allowed per tile at 1x ...
    "tile_cap_per_dpr": 20,  # ... plus this much per extra device pixel ratio (2x: 30)
    "small": 16,           # a channel difference above this counts as "different"
    "big_share": 0.0002,   # share of the image that may be big pixels
    "small_share": 0.001,  # share of the image that may differ at all
}
DESKTOP = {"width": 1280, "height": 860}
PHONE = {"width": 390, "height": 844}

# name: (alt text, viewport, color scheme, device scale factor)
SHOTS = {
    "overview-light": ("The board in light mode: decisions waiting, usage meters, owner-away banner", DESKTOP, "light", 1),
    "overview-dark": ("The board in dark mode", DESKTOP, "dark", 1),
    "session-card": ("Session cards by state, one expanded with its title, pending decisions, running tool and tmux target", DESKTOP, "light", 1),
    "decisions": ("Decision cards: a permission prompt with Allow / Deny and a question with its options", DESKTOP, "light", 1),
    "notifications": ("The Notifications dialog: mutes, triage floor and presence", {"width": 1280, "height": 1100}, "light", 1),
    "resources": ("The Resources section: memory, swap, disks and per-session processes", DESKTOP, "light", 1),
    "usage-popover": ("The usage popover: both windows with reset times, the threshold reached and the account", DESKTOP, "light", 1),
    "profile-menu": ("The profile menu: identity, roles, theme and language switchers, API keys", DESKTOP, "light", 1),
    "phone-dark": ("The board on a phone, dark mode", PHONE, "dark", 2),
    "pwa-standalone": ("The board as an iPhone home-screen app, clear of the status bar", {"width": 393, "height": 852}, "light", 2),
    "overview-zh": ("The board in Traditional Chinese (繁體中文)", DESKTOP, "light", 1),
}
LANG = {"overview-zh": "zh-TW"}  # shots in another language; English otherwise

WEBP_JS = """async ([b64, q]) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  c.getContext('2d').drawImage(img, 0, 0);
  const blob = await new Promise((ok) => c.toBlob(ok, 'image/webp', q));
  if (!blob || blob.type !== 'image/webp') return null;
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = ''; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(s);
}"""


SAME_JS = """async ([oldB64, newB64, t, cap]) => {
  const load = async (src) => { const i = new Image(); i.src = src; await i.decode(); return i; };
  const [a, b] = await Promise.all([load('data:image/webp;base64,' + oldB64), load('data:image/webp;base64,' + newB64)]);
  if (a.naturalWidth !== b.naturalWidth || a.naturalHeight !== b.naturalHeight) return false;
  const px = (img) => { const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight;
    const g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, c.width, c.height).data; };
  const x = px(a), y = px(b), w = a.naturalWidth, tw = Math.ceil(w / t.tile);
  const tiles = new Int32Array(tw * Math.ceil(a.naturalHeight / t.tile));
  let off = 0, big = 0;
  for (let i = 0, p = 0; i < x.length; i += 4, p++) {
    const d = Math.max(Math.abs(x[i] - y[i]), Math.abs(x[i + 1] - y[i + 1]), Math.abs(x[i + 2] - y[i + 2]));
    if (d > t.small) off++;
    if (d > t.big) {
      big++;
      const k = Math.floor((p % w) / t.tile) + tw * Math.floor(Math.floor(p / w) / t.tile);
      if (++tiles[k] > cap) return false;  // clustered: a visible change
    }
  }
  const n = x.length / 4;
  return big <= t.big_share * n && off <= t.small_share * n;
}"""


def same_image(conv, old, new, scale=1):
    """Do two WebP images show the same thing (within SAME_TOLERANCE, at this device scale)?"""
    t = SAME_TOLERANCE
    cap = t["tile_cap"] + t["tile_cap_per_dpr"] * max(0, scale - 1)
    return bool(conv.evaluate(SAME_JS, [base64.b64encode(old).decode(), base64.b64encode(new).decode(), t, cap]))


def to_webp(conv, png):
    """PNG bytes -> WebP bytes under MAX_BYTES (quality steps down as needed)."""
    b64 = base64.b64encode(png).decode()
    for q in (0.9, 0.82, 0.74, 0.66, 0.58, 0.5):
        out = conv.evaluate(WEBP_JS, [b64, q])
        if out is None:
            raise SystemExit("screenshots: this Chromium cannot encode WebP")
        data = base64.b64decode(out)
        if len(data) <= MAX_BYTES:
            return data
    raise SystemExit("screenshots: an image stays over %d bytes" % MAX_BYTES)


def union(page, sels, pad=12):
    """The box around the elements, in page coordinates (bounding_box() is viewport-relative)."""
    sx, sy = page.evaluate("() => [scrollX, scrollY]")
    boxes = [page.locator(s).bounding_box() for s in sels]
    boxes = [dict(b, x=b["x"] + sx, y=b["y"] + sy) for b in boxes if b]
    x0, y0 = min(b["x"] for b in boxes) - pad, min(b["y"] for b in boxes) - pad
    x1, y1 = max(b["x"] + b["width"] for b in boxes) + pad, max(b["y"] + b["height"] for b in boxes) + pad
    return {"x": max(0, x0), "y": max(0, y0), "width": x1 - max(0, x0), "height": y1 - max(0, y0)}


def shoot(page, name):
    """Set the page up for one shot and return the PNG bytes."""
    if name in ("overview-light", "overview-dark", "phone-dark", "pwa-standalone", "overview-zh"):
        return page.screenshot()
    if name == "session-card":
        page.click('article[aria-label="Session API"] .scard-toggle')
        page.wait_for_timeout(150)
        return page.screenshot(full_page=True, clip=union(page, ["#board .sec-head", "#board-cols"]))
    if name == "decisions":
        return page.screenshot(full_page=True, clip=union(page, ["#decisions .sec-head", "#dec-d-1-demo", "#dec-d-2-demo"]))
    if name == "notifications":
        page.click("#notif-btn")
        page.wait_for_timeout(250)
        return page.locator("#notif").screenshot()
    if name == "resources":
        return page.locator("#resources").screenshot()
    if name == "usage-popover":
        page.hover("#usage")
        page.wait_for_timeout(200)
        return page.screenshot(full_page=True, clip=union(page, ["#usage", "#usage-pop"], pad=16))
    if name == "profile-menu":
        page.click("#acct-btn")
        page.wait_for_timeout(200)
        return page.screenshot(full_page=True, clip=union(page, ["#acct-btn", "#acct-panel"], pad=16))
    raise KeyError(name)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true", help="print name<TAB>alt text and exit")
    ap.add_argument("--eval", default="", help="JavaScript run in the page before each shot (tests: a one-digit edit)")
    args = ap.parse_args(argv[1:])
    if args.list:
        for name, (alt, *_rest) in SHOTS.items():
            print("%s\t%s" % (name, alt))
        return 0
    names = [n for n in args.only.split(",") if n] or list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        sys.exit("screenshots: unknown shot(s): %s" % ", ".join(unknown))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("screenshots: python playwright is not installed")
    os.makedirs(args.out, exist_ok=True)
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_site", os.path.join(REPO, "scripts", "build-site.py"))
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    tmp = tempfile.mkdtemp(prefix="switchboard-shots.")
    with open(os.path.join(tmp, "demo.html"), "w", encoding="utf-8") as fh:
        fh.write(site.demo_html(keep_fonts=True))
    page_url = "file://" + os.path.join(tmp, "demo.html")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        conv = browser.new_page()
        for name in names:
            _alt, viewport, scheme, scale = SHOTS[name]
            ctx = browser.new_context(viewport=viewport, device_scale_factor=scale, color_scheme=scheme,
                                      timezone_id="UTC", locale="en-US", reduced_motion="reduce")
            if name == "pwa-standalone":
                ctx.add_init_script("Object.defineProperty(navigator, 'standalone', { get: () => true });")
            # The language is chosen before the page's pre-paint script reads it.
            ctx.add_init_script("try { localStorage.setItem('switchboard.lang', %s); } catch (e) {}" % json.dumps(LANG.get(name, "en")))
            page = ctx.new_page()
            page.clock.set_fixed_time(DEMO_TIME)
            if name == "pwa-standalone":
                cdp = ctx.new_cdp_session(page)
                cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 59, "bottom": 34, "left": 0, "right": 0}})
            page.goto(page_url)
            page.wait_for_load_state("networkidle")
            page.evaluate("() => document.fonts.ready")
            if args.eval:
                page.evaluate(args.eval)
            page.wait_for_timeout(300)
            data = to_webp(conv, shoot(page, name))
            path = os.path.join(args.out, name + ".webp")
            old = open(path, "rb").read() if os.path.isfile(path) else None
            if old and (old == data or same_image(conv, old, data, scale)):
                print("%-16s %4dx%-4d @%dx  %6d bytes  (unchanged)" % (name, viewport["width"], viewport["height"], scale, len(old)))
            else:
                with open(path, "wb") as fh:
                    fh.write(data)
                print("%-16s %4dx%-4d @%dx  %6d bytes" % (name, viewport["width"], viewport["height"], scale, len(data)))
            ctx.close()
        browser.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
