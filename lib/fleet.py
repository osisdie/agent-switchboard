#!/usr/bin/env python3
"""Shared helpers for agent-switchboard: config resolution and fleet.yaml access.

CLI (used by the bash scripts):
  fleet.py config-dir              print the resolved config directory
  fleet.py get KEY [DEFAULT]       print a dotted key from fleet.yaml (JSON for non-scalars)
  fleet.py sessions                print fleet sessions as JSON lines
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

DEFAULTS = {
    "collector": True,
    "poll_interval": 3,
    "decision_timeout_hours": 24,
    "remote_url_prefix": "https://claude.ai/code/",
    "resume_fallback_seconds": 15,
    "approvers": [],
    "release_managers": [],
    "press": {
        "prompt_regex": r"Do you want to (proceed|make this edit|create|overwrite|allow)",
        "lines": 30,
        "allow_keys": ["1"],
        "deny_keys": ["Escape"],
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


def id_list(cfg, key):
    val = cfg.get(key) or []
    if not isinstance(val, list):
        val = [val]
    return [str(v) for v in val if v not in (None, "")]


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


def safe_text(s):
    """Text for a terminal: control characters (escapes, CR, ...) become '?'."""
    return CONTROL_RE.sub("?", "" if s is None else str(s))


def is_production(d):
    """A decision is a production decision when its gate or stage, trimmed and lowercased, is
    "production" (the same rule as `board answer`)."""
    return any(str(d.get(k) or "").strip().lower() == "production" for k in ("gate", "stage"))


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
        sessions.append({
            "code": str(s["code"]),
            "cwd": os.path.expanduser(str(s.get("cwd") or "~")),
            "autostart": bool(s.get("autostart", False)),
            "resume_name": s.get("resume_name") or None,
            "extra_args": str(s.get("extra_args") or ""),
            "resume": s.get("resume", True) is not False,
            "initial_prompt": str(s.get("initial_prompt") or ""),
            "aliases": [str(a) for a in (s.get("aliases") or [])],
        })
    # Top-level `aliases: {CODE: [alias, ...]}` is also accepted.
    top = data.get("aliases") or {}
    if isinstance(top, dict):
        for code, al in top.items():
            match = [s for s in sessions if s["code"] == str(code)]
            if match:
                match[0]["aliases"] = sorted(set(match[0]["aliases"]) | {str(a) for a in al or []})
            else:
                sessions.append({"code": str(code), "cwd": None, "autostart": False, "resume_name": None,
                                 "extra_args": "", "resume": True, "initial_prompt": "", "aliases": [str(a) for a in al or []], "listed": False})
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
