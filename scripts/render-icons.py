#!/usr/bin/env python3
"""scripts/render-icons.py — render the board's PNG icons from the SVG sources with a headless
Chromium (python playwright). Run it after changing an icon SVG; commit the PNGs.

  python3 scripts/render-icons.py

  artifact/icon.svg                  -> artifact/favicon.png (32, transparent corners)
  artifact/icons/icon-maskable.svg   -> artifact/apple-touch-icon.png (180),
                                        artifact/icons/icon-192.png, icon-512.png
                                        (full bleed, opaque: iOS ignores transparency, and the
                                        mark stays inside the maskable safe zone, a centred
                                        circle of 40% radius)

PLAYWRIGHT_CHROMIUM_EXECUTABLE points at a specific Chromium / headless shell when the one
playwright expects is not installed. The board's data: URI favicon in artifact/board.html is
artifact/icon.svg, URL-encoded; tests/test_serve.sh checks that they match.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ART = os.path.join(REPO, "artifact")
JOBS = (("icon.svg", "favicon.png", 32, True),
        ("icons/icon-maskable.svg", "apple-touch-icon.png", 180, False),
        ("icons/icon-maskable.svg", "icons/icon-192.png", 192, False),
        ("icons/icon-maskable.svg", "icons/icon-512.png", 512, False))


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("render-icons: python playwright is not installed")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None)
        for src, out, size, transparent in JOBS:
            with open(os.path.join(ART, src), encoding="utf-8") as fh:
                svg = fh.read()
            page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
            page.set_content("<style>html,body{margin:0;background:transparent}svg{display:block;width:%dpx;height:%dpx}</style>%s"
                             % (size, size, svg))
            page.screenshot(path=os.path.join(ART, out), omit_background=transparent)
            page.close()
            print("%-28s %4d px  %6d bytes" % (out, size, os.path.getsize(os.path.join(ART, out))))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
