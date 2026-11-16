#!/usr/bin/env python3
"""What a board viewer who is not an approver or release manager may see of ticket text
(owner decision Q3). Used by board-serve (/api/state, SSE) and by `board task ls --json --viewer`
(the operator's source for the Artifact mirror).

  tasks_for(tasks, reader)          a task taken from a ticket shows the key as its title, and its
                                    `ticket` keeps only provider and key
  ticket_text_for(state, reader)    the board state: tasks as above, each session's `task` title,
                                    and a kind "ticket" message's `ticket` ({provider, key})

CLI: viewer.py tasks   JSON task list on stdin -> the viewer's list on stdout
"""
import json
import sys


def tasks_for(tasks, reader):
    if reader or not isinstance(tasks, list):
        return tasks
    out = []
    for t in tasks:
        tk = t.get("ticket") if isinstance(t, dict) else None
        if isinstance(tk, dict):
            t = dict(t, title=str(tk.get("key") or ""), ticket={"provider": tk.get("provider"), "key": tk.get("key")})
        out.append(t)
    return out


def ticket_text_for(state, reader):
    if reader:
        return state
    tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
    hidden = {str(t.get("title")): str(t["ticket"].get("key") or "") for t in tasks
              if isinstance(t, dict) and isinstance(t.get("ticket"), dict) and t.get("title")}
    state = dict(state, tasks=tasks_for(tasks, False))
    if isinstance(state.get("sessions"), list):
        state["sessions"] = [dict(s, task=hidden[s["task"]]) if isinstance(s, dict) and s.get("task") in hidden else s
                             for s in state["sessions"]]
    if isinstance(state.get("messages"), list):
        state["messages"] = [dict(m, ticket={k: m["ticket"].get(k) for k in ("provider", "key")})
                             if isinstance(m, dict) and m.get("kind") == "ticket" and isinstance(m.get("ticket"), dict) else m
                             for m in state["messages"]]
    return state


def main(argv):
    if argv[1:] != ["tasks"]:
        sys.stderr.write("usage: viewer.py tasks < tasks.json\n")
        return 2
    try:
        tasks = json.load(sys.stdin)
    except ValueError:
        sys.stderr.write("viewer.py: stdin is not JSON\n")
        return 1
    print(json.dumps(tasks_for(tasks, False), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
