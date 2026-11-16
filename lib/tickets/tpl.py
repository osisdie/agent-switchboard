"""Templates and a tiny JSON path language for tracker configs (no eval, no string-built JSON).

Templates: "{name}" placeholders, name = [a-z_][a-z0-9_]*, filled from a context dict.
  fill_path   each value becomes ONE percent-encoded path segment ("/", "?", "#", "%", ":" and "@"
              encoded); "", "." and ".." are refused, so a value can never change the host or
              climb the path
  fill_text   plain substitution, for query values (urlencoded by the caller) and link templates
  fill_body   a parsed JSON template: a string that is exactly "{name}" takes the value as is
              (typed); other strings get plain substitution; keys are never templated

Paths: "$", then any of ".name", "[n]", "[?field=={var}]" (the first element of a list whose
field equals the context value), e.g. "$.data[?displayId=={key}].id". A missing step gives None.
"""
import re
import urllib.parse

PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
PATH_STEP_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[([0-9]{1,6})\]|\[\?([A-Za-z_][A-Za-z0-9_-]*)==\{([a-z_][a-z0-9_]*)\}\]")


class TemplateError(ValueError):
    pass


def placeholders(tpl):
    return set(PLACEHOLDER_RE.findall(tpl if isinstance(tpl, str) else ""))


def _scalar(ctx, name):
    if name not in ctx:
        raise TemplateError("no value for {%s}" % name)
    v = ctx[name]
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise TemplateError("{%s} is not a string or number" % name)
    return str(v)


def fill_path(tpl, ctx):
    def seg(m):
        v = _scalar(ctx, m.group(1))
        if v in ("", ".", ".."):
            raise TemplateError("{%s} is not a usable path segment" % m.group(1))
        return urllib.parse.quote(v, safe="")
    return PLACEHOLDER_RE.sub(seg, tpl)


def fill_link(tpl, ctx):
    """A link for people (get.link): each value percent-encoded as one segment; None when a value
    is missing (the ticket then has no url)."""
    try:
        return PLACEHOLDER_RE.sub(lambda m: urllib.parse.quote(_scalar(ctx, m.group(1)), safe=""), str(tpl))
    except TemplateError:
        return None


def fill_text(tpl, ctx):
    return PLACEHOLDER_RE.sub(lambda m: _scalar(ctx, m.group(1)), str(tpl))


def fill_body(tpl, ctx):
    if isinstance(tpl, dict):
        return {k: fill_body(v, ctx) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [fill_body(v, ctx) for v in tpl]
    if isinstance(tpl, str):
        m = PLACEHOLDER_RE.fullmatch(tpl)
        if m:
            if m.group(1) not in ctx:
                raise TemplateError("no value for {%s}" % m.group(1))
            return ctx[m.group(1)]
        return fill_text(tpl, ctx)
    return tpl


def check_path_expr(expr):
    """None when expr is a valid path, else the reason."""
    if not isinstance(expr, str) or not expr.startswith("$"):
        return "a path starts with $"
    pos = 1
    while pos < len(expr):
        m = PATH_STEP_RE.match(expr, pos)
        if not m:
            return "unexpected text at %d" % pos
        pos = m.end()
    return None


def lookup(doc, expr, ctx=None):
    err = check_path_expr(expr)
    if err:
        raise TemplateError(err)
    cur, pos = doc, 1
    while pos < len(expr):
        m = PATH_STEP_RE.match(expr, pos)
        pos = m.end()
        name, idx, field, var = m.groups()
        if name is not None:
            cur = cur.get(name) if isinstance(cur, dict) else None
        elif idx is not None:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        else:
            want = _scalar(ctx or {}, var)
            cur = next((x for x in cur if isinstance(x, dict) and str(x.get(field)) == want), None) \
                if isinstance(cur, list) else None
        if cur is None:
            return None
    return cur
