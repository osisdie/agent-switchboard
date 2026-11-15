#!/usr/bin/env python3
"""Check that a translated README keeps the structure of README.md.

Usage: scripts/check-readme-sync.py [SOURCE [TRANSLATION ...]]
       (default: README.md README.zh-TW.md)

Heading text is translated, so the check compares structure, not words:
  1. the number of `##` headings,
  2. the order of `##` and `###` headings (the level sequence),
  3. inline code in each heading (`tmp-local/` must stay `tmp-local/`).
Headings inside fenced code blocks are ignored. Exit 0 = in sync,
1 = mismatch (details on stderr), 2 = usage or I/O error.
"""
import re
import sys

HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
CODE = re.compile(r"`([^`]+)`")


def headings(path):
    out = []
    fence = None
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            m = FENCE.match(line)
            if m:
                if fence is None:
                    fence = m.group(1)
                elif m.group(1) == fence:
                    fence = None
                continue
            if fence is not None:
                continue
            h = HEADING.match(line)
            if h:
                out.append((len(h.group(1)), CODE.findall(h.group(2)), lineno, h.group(2)))
    return out


def compare(src, dst):
    a, b = headings(src), headings(dst)
    errors = []
    n2a = sum(1 for h in a if h[0] == 2)
    n2b = sum(1 for h in b if h[0] == 2)
    if n2a != n2b:
        errors.append("## heading count: %s has %d, %s has %d" % (src, n2a, dst, n2b))
    for i in range(max(len(a), len(b))):
        ha = a[i] if i < len(a) else None
        hb = b[i] if i < len(b) else None
        if ha and hb and ha[0] == hb[0] and ha[1] == hb[1]:
            continue
        left = "%s:%d %s %s" % (src, ha[2], "#" * ha[0], ha[3]) if ha else "%s: (none)" % src
        right = "%s:%d %s %s" % (dst, hb[2], "#" * hb[0], hb[3]) if hb else "%s: (none)" % dst
        errors.append("heading #%d differs (level or inline code):\n    %s\n    %s" % (i + 1, left, right))
        break  # later headings are shifted too; the first difference is enough
    return errors


def main(argv):
    if any(x in ("-h", "--help") for x in argv):
        print(__doc__.strip())
        return 0
    files = argv or ["README.md", "README.zh-TW.md"]
    if len(files) < 2:
        sys.stderr.write("need a source and at least one translation\n")
        return 2
    src, rest = files[0], files[1:]
    failed = False
    for dst in rest:
        try:
            errors = compare(src, dst)
        except OSError as exc:
            sys.stderr.write("readme-sync: %s\n" % exc)
            return 2
        if errors:
            failed = True
            sys.stderr.write("readme-sync: %s is out of sync with %s\n" % (dst, src))
            for e in errors:
                sys.stderr.write("  - %s\n" % e)
        else:
            print("readme-sync: %s matches %s (%d headings)" % (dst, src, len(headings(src))))
    if failed:
        sys.stderr.write("Update the translation in the same PR. If it has to lag, still mirror the "
                         "heading (English text is fine) and say so in the PR (see CONTRIBUTING.md).\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
