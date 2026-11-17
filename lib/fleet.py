#!/usr/bin/env python3
"""Shared helpers for agent-switchboard: config resolution and fleet.yaml access.

CLI (used by the bash scripts):
  fleet.py config-dir              print the resolved config directory
  fleet.py get KEY [DEFAULT]       print a dotted key from fleet.yaml (JSON for non-scalars)
  fleet.py sessions                print fleet sessions as JSON lines
  fleet.py board-link [ID]         print the board link for a decision/message/consensus id (no
                                   ID: the board root); prints nothing when no board URL is set
  fleet.py board-url-check         print the board URL, or why it is rejected (exit 1)
  fleet.py askers-check            for `board doctor`: askers missing from an explicit
                                   serve.tunnel.allowed_emails (one per line; exit 1 when any)
  fleet.py service-tokens-check    for `board doctor`: "unbound <cn>" / "nobody <cn>" per
                                   serve.tunnel.api_service_tokens entry not bound to a person
                                   (exit 1 when any)
"""
import json
import os
import re
import sys
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

DEFAULTS = {
    "collector": True,
    "poll_interval": 3,
    "decision_timeout_hours": 24,
    "remote_url_prefix": "https://claude.ai/code/",
    "resume_fallback_seconds": 15,
    "approvers": [],
    "release_managers": [],
    "askers": [],
    "owners": [],
    "serve": {
        "enabled": False,
        "port": 39410,
    },
    "press": {
        "prompt_regex": r"Do you want to (proceed|make this edit|create|overwrite|allow)",
        "lines": 30,
        "allow_keys": ["1"],
        "deny_keys": ["Escape"],
        # AskUserQuestion picker footer ("Enter to select · ↑/↓ to navigate · Esc to cancel").
        "question_regex": r"Enter to select.*navigate",
        # The picker row that takes free text ("4. Type something.").
        "free_text_label": r"Type something",
        # A form of several questions: the tab bar above it ("←  ☐ Color  ☐ Size  ✔ Submit  →"), the
        # review screen after the last question, and its submit row ("1. Submit answers").
        "tabbar_regex": r"^[[:space:]]*←.*(☐|☒).*Submit.*→",
        "review_regex": r"Review your answers",
        "focus_regex": r"(❯|>|›)",
        "submit_label": r"Submit answers",
        # The TUI scrolled up with a prompt open: one of its last 2 non-blank lines is, as a whole line,
        # "4 new messages (ctrl+End) ↓". board press then sends scroll_key (scroll to the bottom; never
        # submits in Claude Code's TUI) once and looks again.
        "scrolled_regex": r"^[[:space:]]*[0-9]+ new messages? .*ctrl\+End.*$",
        "scroll_key": "C-End",
        # The pane's #{pane_current_command} must match this (the TUI) before any key: claude,
        # claude.exe, node, or a version number (the native installer's binary is named after its
        # version, X.Y.Z). Empty: not checked.
        "tui_command_regex": r"^(claude(\.exe)?|node|[0-9]+(\.[0-9]+){2})$",
        # A shortened question taller than the pane: how many lines of scrollback above the screen
        # board press may search for its start (0 = never: the start must be on screen).
        "scrollback_lines": 200,
        # A line that starts another turn (Claude's "●"/"⏺" bullet, the "❯" prompt): none may sit
        # between a question's start found in the scrollback and its picker. A focused picker row
        # ("❯ 2. Blue": focus_regex, then a number) is not one.
        "turn_regex": r"^[[:space:]]*(●|⏺|❯)",
    },
    # Session actions (lib/actions.py): fixed slash commands typed into an idle session's empty
    # prompt box, and what the pane must show before and after (Claude Code's own screens;
    # adjust when they change).
    "session_actions": {
        "auto_apply": True,
        "lines": 30,
        "prompt_regex": r"^\u276f[ \u00a0]*$",
        "rule_regex": r"^\u2500{8,}",
        "busy_regex": r"esc to interrupt",
        # The footer of Claude's dialogs and pickers ("Enter to select · Esc to continue",
        # "Enter to confirm · Esc to cancel").
        "dialog_regex": r"Enter to (select|confirm)",
        # A vim-mode prompt (Enter does not submit there) or another mode line: refused.
        "mode_regex": r"-- (NORMAL|VISUAL|VISUAL LINE) --",
        "retry_minutes": 10,
        "max_tries": 3,
        "queue_hours": 12,
        "rc_confirm_seconds": 10,
        # Matched against the focused row of the dialog only (the text after the focus mark).
        "rc_disconnect_label": r"^\s*\u276f?\s*Disconnect this session\s*$",
        # Rows of the consent and Disconnect dialogs: each a whole row of its own.
        "rc_consent_regex": r"^\s*Never mind\b",
        "rc_consent_title_regex": r"^\s*Enable Remote Control\b",
        "rc_qr_regex": r"^\s*Show QR code\b",
        "rc_continue_regex": r"^\s*Continue\s*$",
        "rc_unavailable_regex": r"Unknown command: /remote-control",
        "compact_running_regex": r"Compacting conversation",
        "compact_done_regex": r"\u23bf\s+Compacted",
        "compact_empty_regex": r"Not enough messages to compact",
        "compact_start_seconds": 15,
        "compact_seconds": 600,
        # Idle again with no "Compacting" line and no "Compacted" line for this long: interrupted.
        "compact_idle_seconds": 5,
        # How long a CLI call waits for another action on the same session before it only stores
        # the request ("will apply when free").
        "lock_wait_seconds": 20,
    },
    # Resume after a usage limit (lib/resume.py, `board resume`): what Claude Code's limit screens
    # look like (2.1.x; adjust when they change) and the prompt typed into an idle limited session.
    "limit_resume": {
        "prompt": "Quota is available again. Continue where you left off.",
        "lines": 40,
        # The looping collector reads the running sessions' panes this often (0: never), for the
        # board's "usage limit" chip and Resume button.
        "scan_seconds": 30,
        "confirm_seconds": 10,
        # Two Escapes at least this far apart: a quick double Escape opens Claude's rewind menu.
        "esc_gap_seconds": 1.5,
        "max_steps": 4,
        # The usage-limit options dialog: its title, its rows, and the footer. It counts only with
        # a limit notice in it or right above it and at least two of its rows, one of them
        # limit-specific (dialog_row_regex; a bare "Stop" alone is any picker's). It is closed with
        # Escape (its own cancel = "Stop"), never by a row.
        "dialog_title_regex": r"What do you want to do\?",
        "dialog_row_regex": r"^(\d+\.\s*)?(Stop and wait for limit to reset|Wait here, then continue automatically\b.*"
                            r"|Don.t continue automatically|Upgrade your plan|Switch to (usage|extra)\b.*"
                            r"|Add funds to continue\b.*|Ask your admin for more usage)\s*$",
        "dialog_stop_regex": r"^(\d+\.\s*)?Stop\s*$",
        "dialog_footer_regex": r"Esc to cancel",
        # Claude's automatic continue, armed: a line below the input box.
        "wait_regex": r"continuing (automatically|shortly)\b.*esc to cancel",
        # The limit has reset and the box holds Claude's prefilled text: a line below the box.
        "ready_regex": r"press enter to continue",
        "prefill": "continue",
        # Claude's own limit notice, matched at the START of a "⎿" line's text, or of the last line
        # before the input box when it is not part of an assistant "●" paragraph (case-insensitive).
        # Assistant prose that mentions limits never counts.
        "limit_regex": r"(claude (ai )?)?usage limit (reached|has reset|reset)|you.ve hit your (usage |session |weekly )?limit"
                       r"|(5-hour|weekly|session|opus|sonnet) limit reached"
                       r"|automatic continue (was )?(cancelled|turned off|stopped|did not run)",
    },
    # Remote Control at start (bin/fleet-up): unset_do_not_track starts claude with
    # `env -u DO_NOT_TRACK` (Claude Code ignores Remote Control while DO_NOT_TRACK is set).
    "rc": {
        "unset_do_not_track": False,
    },
}


def config_dir():
    env = os.environ.get("BOARD_CONFIG_DIR")
    if env:
        return env
    local = os.path.join(REPO, "tmp-local")
    if os.path.isdir(local):
        return local
    return os.path.join(os.path.expanduser("~"), ".config", "agent-switchboard")


def account_enabled(cfg):
    """May the board show the account the sessions run on? Off with BOARD_SHOW_ACCOUNT=0 or
    usage.show_account: false in fleet.yaml (the collector and board-serve both ask)."""
    if os.environ.get("BOARD_SHOW_ACCOUNT", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    usage = cfg.get("usage") if isinstance(cfg.get("usage"), dict) else {}
    return usage.get("show_account") is not False


def id_list(cfg, key):
    val = cfg.get(key) or []
    if not isinstance(val, list):
        val = [val]
    return [str(v) for v in val if v not in (None, "")]


def owner_ids(cfg):
    """The board's owners: `owners`, else `approvers` (a single-user board keeps working). The one
    rule for the board's owner-only surfaces (board-serve, reminders); API key administration never
    falls back (lib/apikeys.py)."""
    return id_list(cfg, "owners") or id_list(cfg, "approvers")


def service_tokens(cfg):
    """serve.tunnel.api_service_tokens -> {common_name: owners}, names and owners lowercased.

    owners is a frozenset of the people (emails, or "local") the token is bound to, or None for an
    unbound token. Forms: a mapping {cn: owner | [owners]} (bound), a list or a single string of
    common names (unbound, the old form), or a list mixing names and one-entry mappings. A mapped
    token with no owners is bound to nobody: every call through it is refused (fail closed). A
    name both bound and unbound counts as bound."""
    serve = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    tun = serve.get("tunnel") if isinstance(serve.get("tunnel"), dict) else {}
    raw = tun.get("api_service_tokens")
    items = [raw] if isinstance(raw, (str, dict)) else raw if isinstance(raw, list) else []
    out = {}

    def norm(v):
        return str(v or "").strip().lower()
    for it in items:
        if isinstance(it, dict):
            for cn, who in it.items():
                if not norm(cn):
                    continue
                who = [who] if not isinstance(who, list) else who
                prev = out.get(norm(cn)) or frozenset()
                out[norm(cn)] = prev | frozenset(norm(w) for w in who if norm(w))
        elif norm(it) and norm(it) not in out:
            out[norm(it)] = None
    return out


def board_home():
    return os.environ.get("BOARD_HOME") or os.path.join(os.path.expanduser("~"), ".agent-switchboard")


def ensure_home():
    """Private state: umask 077 for every file written from here on, BOARD_HOME mode 0700."""
    os.umask(0o077)
    h = board_home()
    if not os.path.isdir(h):
        os.makedirs(h, mode=0o700, exist_ok=True)
    return h


CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")

# Ids that can be linked to on the board (and the only ones board.html will look up). Always
# use .fullmatch(): in Python `$` also matches before a trailing newline ("d-1-ab\n").
DEC_ID_RE = re.compile(r"d-[0-9]+-[a-z0-9]+")
MSG_ID_RE = re.compile(r"m-[0-9]+-[a-z0-9]+")
CONS_ID_RE = re.compile(r"c-[0-9]+-[a-z0-9]+")
LINK_PARAMS = (("d", DEC_ID_RE), ("m", MSG_ID_RE), ("c", CONS_ID_RE))
LOCAL_HOSTS = ("127.0.0.1", "localhost")
BOARD_PATH_RE = re.compile(r"[A-Za-z0-9._~/%-]*")
HOST_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


def env_has(pid, name):
    """Does process PID have NAME set (non-empty) in its environment? Reads only the names in
    /proc/<pid>/environ of this user's own processes; never returns a value."""
    try:
        with open("/proc/%d/environ" % int(pid), "rb") as fh:
            env = fh.read()
    except (OSError, ValueError, TypeError):
        return False
    key = name.encode() + b"="
    return any(item.startswith(key) and len(item) > len(key) for item in env.split(b"\0"))


def safe_text(s):
    """Text for a terminal: control characters (escapes, CR, ...) become '?'."""
    return CONTROL_RE.sub("?", "" if s is None else str(s))


def is_production(d):
    """A decision is a production decision when its gate or stage, trimmed and lowercased, is
    "production" (the same rule as `board answer`)."""
    return any(str(d.get(k) or "").strip().lower() == "production" for k in ("gate", "stage"))


def read_dotenv(path=None):
    """KEY=VALUE pairs of <config dir>/.env (comments, inline ` # comments` after an unquoted value,
    `export ` and quotes handled)."""
    env = {}
    path = path or os.path.join(config_dir(), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if v[:1] in ("\"", "'") and v.find(v[0], 1) > 0:
                    v = v[1:v.find(v[0], 1)]  # quoted: up to the closing quote (a comment may follow)
                else:
                    v = re.split(r"\s+#", v, maxsplit=1)[0]  # unquoted: an inline " # comment" is not the value
                env[k] = v
    except OSError:
        pass
    return env


def check_board_url(raw):
    """(normalized base URL, None) or (None, reason). https only (http only for 127.0.0.1 and
    localhost), no userinfo, no query or fragment; a trailing slash is dropped."""
    u = str(raw or "").strip()
    if not u:
        return None, "empty"
    if CONTROL_RE.search(u) or any(c in u for c in " <>|\"'`\\^{}"):
        return None, "contains spaces, control or quote characters"
    if "?" in u or "#" in u:
        return None, "must not have a query or fragment"
    try:
        p = urllib.parse.urlsplit(u)
        port = p.port
    except ValueError:
        return None, "not a valid URL"
    host = (p.hostname or "").lower()
    if "@" in p.netloc:
        return None, "must not contain a user name or password"
    if not host:
        return None, "needs a host name"
    if not all(HOST_LABEL_RE.fullmatch(label) for label in host.split(".")):
        return None, "host name is not valid (each label: letters, digits, inner hyphens)"
    if port == 0:
        return None, "port 0 is not valid"
    scheme = p.scheme.lower()
    if scheme == "http" and host not in LOCAL_HOSTS:
        return None, "must use https (http only for 127.0.0.1 or localhost)"
    if scheme not in ("https", "http"):
        return None, "must use https"
    if not BOARD_PATH_RE.fullmatch(p.path):
        return None, "path has unexpected characters"
    netloc = host + (":%d" % port if port else "")
    return "%s://%s%s" % (scheme, netloc, p.path.rstrip("/")), None


def board_url_check(cfg=None):
    """(base, error, source) for the public board URL: env BOARD_PUBLIC_URL, then BOARD_PUBLIC_URL
    in <config dir>/.env, then `board_url` in fleet.yaml. Unset: (None, None, None)."""
    for source, raw in (("env BOARD_PUBLIC_URL", os.environ.get("BOARD_PUBLIC_URL")),
                        (".env BOARD_PUBLIC_URL", read_dotenv().get("BOARD_PUBLIC_URL"))):
        if raw:
            break
    else:
        cfg = cfg if cfg is not None else load_fleet()
        source, raw = "fleet.yaml board_url", cfg.get("board_url")
    if raw in (None, ""):
        return None, None, None
    base, err = check_board_url(raw)
    return base, err, source


def board_url(cfg=None):
    """The validated public board URL, or None (unset or invalid: then alerts carry no link)."""
    return board_url_check(cfg)[0]


def board_link(base, item_id=None):
    """Link that opens the board on one card: <base>/?d=<decision id> (m= message, c= consensus).
    An id that does not match its strict pattern links to the board root instead. A query, not a
    #fragment: the Cloudflare Access login redirect and some mail clients drop fragments."""
    if not base:
        return None
    i = str(item_id or "")
    for param, rx in LINK_PARAMS:
        if rx.fullmatch(i):
            return "%s/?%s=%s" % (base, param, i)
    return base + "/"


def sessions_dir():
    return os.environ.get("CLAUDE_SESSIONS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_fleet():
    path = os.path.join(config_dir(), "fleet.yaml")
    data = {}
    if os.path.isfile(path):
        try:
            import yaml  # noqa: WPS433
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:  # pragma: no cover - surfaced by `board doctor`
            sys.stderr.write("fleet.yaml: %s\n" % exc)
            data = {}
    if not isinstance(data, dict):
        data = {}
    cfg = _merge(DEFAULTS, data)
    sessions = []
    for s in data.get("sessions") or []:
        if not isinstance(s, dict) or not s.get("code"):
            continue
        if str(s["code"]) == "all":  # reserved: `board rc on|off all` means every session
            sys.stderr.write("fleet.yaml: session code \"all\" is reserved; entry skipped\n")
            continue
        sessions.append({
            "code": str(s["code"]),
            "cwd": os.path.expanduser(str(s.get("cwd") or "~")),
            "autostart": bool(s.get("autostart", False)),
            "resume_name": s.get("resume_name") or None,
            "extra_args": str(s.get("extra_args") or ""),
            "resume": s.get("resume", True) is not False,
            "initial_prompt": str(s.get("initial_prompt") or ""),
            "aliases": [str(a) for a in (s.get("aliases") or [])],
            # true/false: start it with or without --remote-control; None: the fleet default.
            "remote_control": s["remote_control"] if isinstance(s.get("remote_control"), bool) else None,
        })
    # Top-level `aliases: {CODE: [alias, ...]}` is also accepted.
    top = data.get("aliases") or {}
    if isinstance(top, dict):
        for code, al in top.items():
            if str(code) == "all":
                continue
            match = [s for s in sessions if s["code"] == str(code)]
            if match:
                match[0]["aliases"] = sorted(set(match[0]["aliases"]) | {str(a) for a in al or []})
            else:
                sessions.append({"code": str(code), "cwd": None, "autostart": False, "resume_name": None,
                                 "extra_args": "", "resume": True, "initial_prompt": "", "aliases": [str(a) for a in al or []],
                                 "remote_control": None, "listed": False})
    cfg["sessions"] = sessions
    return cfg


def alias_map(cfg):
    """alias -> code (codes map to themselves)."""
    out = {}
    for s in cfg["sessions"]:
        out[s["code"]] = s["code"]
        for a in s["aliases"]:
            out[a] = s["code"]
    return out


def get_key(cfg, key):
    cur = cfg
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd = argv[1]
    if cmd == "config-dir":
        print(config_dir())
    elif cmd == "get":
        val = get_key(load_fleet(), argv[2])
        if val is None:
            val = argv[3] if len(argv) > 3 else ""
        if isinstance(val, bool):
            print("true" if val else "false")
        elif isinstance(val, (dict, list)):
            print(json.dumps(val))
        else:
            print(val)
    elif cmd == "board-link":
        link = board_link(board_url(), argv[2] if len(argv) > 2 else None)
        if link:
            print(link)
    elif cmd == "board-url-check":
        base, err, source = board_url_check()
        if err:
            print("%s rejected: %s" % (source, err))
            return 1
        print(base or "")
    elif cmd == "askers-check":
        cfg = load_fleet()
        serve = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
        tun = serve.get("tunnel") if isinstance(serve.get("tunnel"), dict) else {}
        explicit = tun.get("allowed_emails")
        explicit = [explicit] if isinstance(explicit, str) else explicit
        if not isinstance(explicit, list):
            return 0                       # unset: askers are in the tunnel's fallback set
        allowed = {str(x).strip().lower() for x in explicit}
        missing = [a for a in id_list(cfg, "askers") if "@" in a and a.strip().lower() not in allowed]
        for a in missing:
            print(a)
        return 1 if missing else 0
    elif cmd == "service-tokens-check":
        bad = 0
        for cn, owners in sorted(service_tokens(load_fleet()).items()):
            if owners is None:
                print("unbound %s" % cn)
                bad = 1
            elif not owners:
                print("nobody %s" % cn)
                bad = 1
        return bad
    elif cmd == "sessions":
        for s in load_fleet()["sessions"]:
            if s.get("listed", True):
                print(json.dumps(s))
    else:
        sys.stderr.write("unknown command: %s\n" % cmd)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
