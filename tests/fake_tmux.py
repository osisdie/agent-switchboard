#!/usr/bin/env python3
"""tests/fake_tmux.py — a scripted stand-in for tmux (Remote Control tests). Never talks to a
real tmux server.

FAKE_TMUX_CFG names a JSON file:
  {"session": "CP",                       display-message answer (the pane's tmux session)
   "pane_pid": 123,                       ... and its #{pane_pid} (default 1)
   "screens": ["...", "..."],             capture-pane shows screens[k], k = send-keys calls so
                                          far (the last screen repeats)
   "on_send": {"2": {"path": P, "set": {...}}},
                                          after send-keys call N, merge "set" into JSON file P
                                          (e.g. the registry gains bridgeSessionId)
   "on_capture": {"1": {"path": P, "set": {...}}},
                                          the same, at capture-pane call N (from 1)
   "seq": {"2": ["...", "..."]},          after send-keys call N, one screen per capture-pane
                                          call, in order (the last one repeats)
   "fail_send": 2}                        send-keys call N fails (exit 1)
Every call is appended to FAKE_TMUX_LOG, one line of arguments joined by "|".
"""
import json
import os
import sys


def bump(state_path, key):
    try:
        with open(state_path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st[key] = int(st.get(key, 0)) + 1
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    return st


def mutate(spec):
    if not spec:
        return
    with open(spec["path"], encoding="utf-8") as fh:
        d = json.load(fh)
    d.update(spec["set"])
    with open(spec["path"], "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def main(argv):
    log = os.environ.get("FAKE_TMUX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("|".join(argv) + "\n")
    cfg_path = os.environ.get("FAKE_TMUX_CFG")
    cfg = {}
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    state = (cfg_path or "/nonexistent") + ".state"
    cmd = argv[0] if argv else ""
    if cmd == "display-message":
        if any("#{pane_pid}" in a for a in argv):
            print("%s\t%s" % (cfg.get("session", "CP"), cfg.get("pane_pid", 1)))
        else:
            print(cfg.get("session", "CP"))
        return 0
    if cmd == "list-panes":
        return 0
    if cmd == "capture-pane":
        if not cfg_path:
            return 1
        st = bump(state, "captures")
        mutate((cfg.get("on_capture") or {}).get(str(st["captures"])))
        screens = cfg.get("screens") or [""]
        k = int(st.get("sends", 0))
        seq = (cfg.get("seq") or {}).get(str(k))
        if seq:
            n = int(st.get("since_send", 0))
            st["since_send"] = n + 1
            with open(state, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            sys.stdout.write(seq[min(n, len(seq) - 1)] + "\n")
            return 0
        sys.stdout.write(screens[min(k, len(screens) - 1)] + "\n")
        return 0
    if cmd == "send-keys":
        if not cfg_path:
            return 1
        st = bump(state, "sends")
        st["since_send"] = 0
        with open(state, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        if cfg.get("fail_send") == st["sends"]:
            return 1
        mutate((cfg.get("on_send") or {}).get(str(st["sends"])))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
