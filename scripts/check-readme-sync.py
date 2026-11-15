#!/usr/bin/env python3
"""Check that a translated README keeps the structure of README.md.

Usage: scripts/check-readme-sync.py [SOURCE [TRANSLATION ...]]
       (default: README.md README.zh-TW.md)

Heading text is translated, so the check compares structure, not words:
  1. the number of `##` headings,
  2. the order of `##` and `###` headings (the level sequence),
  3. inline code in each heading (`tmp-local/` must stay `tmp-local/`).
It also checks every file for `**` that CommonMark would print literally:
a closing `**` after punctuation and directly before a letter (common in
CJK text, e.g. `**注意。**接著`), or an opening `**` after a letter and
directly before punctuation. Move the punctuation outside: `**注意**。接著`.

Headings and `**` inside fenced code blocks and code spans are ignored.
Exit 0 = OK, 1 = mismatch (details on stderr), 2 = usage or I/O error.
"""
import re
import sys
import unicodedata

HEADING = re.compile(r"^ {0,3}(#{2,3})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
CODE = re.compile(r"`([^`]+)`")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
BLOCK_START = re.compile(r"^ {0,3}([-*+] |\d+[.)] |\||<|#)")


def prose_lines(path):
    """Yield (lineno, line) for lines outside fenced code blocks."""
    fence = None
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if fence is None:
                m = FENCE_OPEN.match(line)
                if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                    fence = m.group(1)
                    continue
                yield lineno, line
            else:
                m = FENCE_CLOSE.match(line)
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                    fence = None


def headings(path):
    out = []
    for lineno, line in prose_lines(path):
        h = HEADING.match(line)
        if h:
            text = h.group(2) or ""
            out.append((len(h.group(1)), CODE.findall(text), lineno, text))
    return out


def _punct(c):
    return c.isascii() and not c.isalnum() and not c.isspace() or unicodedata.category(c)[0] in "PS"


def _space(c):
    return c == "" or c.isspace()


def bold_problems(path):
    """`**` runs CommonMark can't use as the delimiter they are meant to be."""
    problems = []
    opened = False
    for lineno, line in prose_lines(path):
        if not line.strip() or BLOCK_START.match(line):
            opened = False  # bold never spans paragraphs, list items or table rows
        if not line.strip():
            continue
        text = CODE_SPAN.sub(lambda m: m.group(1) + "x" * len(m.group(2)) + m.group(1), line)
        text = text.replace("\\*", "xx")
        for m in re.finditer(r"(?<!\*)\*\*(?!\*)", text):
            before = text[m.start() - 1] if m.start() else ""
            after = text[m.end()] if m.end() < len(text) else ""
            if not opened:
                ok = not _space(after) and (not _punct(after) or _space(before) or _punct(before))
                kind = "opening"
            else:
                ok = not _space(before) and (not _punct(before) or _space(after) or _punct(after))
                kind = "closing"
            if not ok:
                problems.append("%s:%d: %s ** renders literally: ...%s..."
                                % (path, lineno, kind, line[max(0, m.start() - 12):m.end() + 6]))
            opened = not opened
    return problems


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
    out_of_sync = False
    bad_bold = False
    try:
        for dst in rest:
            errors = compare(src, dst)
            if errors:
                out_of_sync = True
                sys.stderr.write("readme-sync: %s is out of sync with %s\n" % (dst, src))
                for e in errors:
                    sys.stderr.write("  - %s\n" % e)
            else:
                print("readme-sync: %s matches %s (%d headings)" % (dst, src, len(headings(src))))
        for path in files:
            for p in bold_problems(path):
                bad_bold = True
                sys.stderr.write("readme-sync: %s\n" % p)
    except OSError as exc:
        sys.stderr.write("readme-sync: %s\n" % exc)
        return 2
    if out_of_sync:
        sys.stderr.write("Update the translation in the same PR. If it has to lag, still mirror the "
                         "heading (English text is fine) and say so in the PR (see CONTRIBUTING.md).\n")
    if bad_bold:
        sys.stderr.write("Move punctuation outside the bold: `**注意。**接著` -> `**注意**。接著`.\n")
    if out_of_sync or bad_bold:
        return 1
    print("readme-sync: no literal ** in %s" % ", ".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
