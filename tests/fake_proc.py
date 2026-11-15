#!/usr/bin/env python3
"""tests/fake_proc.py — a minimal fixture /proc tree for lib/resources.py (BOARD_PROC_ROOT).

  python3 tests/fake_proc.py ROOT     write a calm tree (16 GiB, half available, no swap use,
                                      load 1.0 on 4 CPUs, only pid 1) and exit

The shell suites point BOARD_PROC_ROOT at a calm tree so the live machine's numbers never
raise res:* alerts in them. tests/test_resources.py builds richer trees with FakeProc.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import resources as R  # noqa: E402


class FakeProc:
    """A minimal /proc tree: system files plus <pid>/{stat,cmdline}."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.procs = {}
        self.mem(total_mb=16384, avail_mb=8192)
        self.write("stat", "cpu  1000 0 1000 8000 0 0 0 0 0 0\ncpu0 1 1 1 1\ncpu1 1 1 1 1\ncpu2 1 1 1 1\ncpu3 1 1 1 1\nbtime 1\n")
        self.write("loadavg", "1.00 0.80 0.50 2/300 4242\n")
        self.write("vmstat", "nr_free_pages 1\noom_kill 2\n")
        self.write("pressure/memory", "some avg10=0.00 avg60=1.50 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.write("uptime", "100000.00 1.00\n")
        self.write("version", "Linux version 6.0.0-generic (builder@example) #1 SMP\n")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def mem(self, total_mb, avail_mb, swap_total_mb=4096, swap_free_mb=4096):
        self.write("meminfo", "MemTotal: %d kB\nMemFree: 1 kB\nMemAvailable: %d kB\nSwapTotal: %d kB\nSwapFree: %d kB\n"
                   % (total_mb * 1024, avail_mb * 1024, swap_total_mb * 1024, swap_free_mb * 1024))

    def cpu(self, busy, idle):
        self.write("stat", "cpu  %d 0 0 %d 0 0 0 0 0 0\ncpu0 1 1 1 1\ncpu1 1 1 1 1\ncpu2 1 1 1 1\ncpu3 1 1 1 1\n" % (busy, idle))

    def proc(self, pid, ppid, comm, argv, rss_mb=10, ticks=0, start_s=1000):
        self.procs[pid] = dict(ppid=ppid, comm=comm, argv=argv, rss_mb=rss_mb, ticks=ticks, start_s=start_s)
        self._flush(pid)

    def tick(self, pid, add):
        self.procs[pid]["ticks"] += add
        self._flush(pid)

    def kill(self, pid):
        self.procs.pop(pid)
        shutil.rmtree(os.path.join(self.root, str(pid)), ignore_errors=True)

    def _flush(self, pid):
        p = self.procs[pid]
        pages = p["rss_mb"] * 1024 * 1024 // R.PAGE
        fields = ["S", str(p["ppid"])] + ["0"] * 9 + [str(p["ticks"]), "0"] + ["0"] * 6 + \
                 [str(int(p["start_s"] * R.HZ)), "0", str(pages)] + ["0"] * 5
        self.write("%d/stat" % pid, "%d (%s) %s\n" % (pid, p["comm"], " ".join(fields)))
        with open(os.path.join(self.root, str(pid), "cmdline"), "wb") as fh:
            fh.write(b"\0".join(a.encode() for a in p["argv"]) + b"\0")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: fake_proc.py ROOT\n")
        sys.exit(2)
    FakeProc(sys.argv[1]).proc(1, 0, "init", ["/sbin/init"])
