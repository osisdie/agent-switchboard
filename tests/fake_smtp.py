#!/usr/bin/env python3
"""Tiny fake SMTP server for tests/test_notify.sh (no TLS, accepts any AUTH).

Usage: fake_smtp.py PORTFILE OUTDIR
Binds 127.0.0.1 on a free port, writes the port to PORTFILE, and saves every message as
OUTDIR/<n>.eml with an "X-Fake-Rcpt:" and "X-Fake-Auth:" line on top. Runs until killed.
"""
import os
import socket
import sys
import threading


def handle(conn, outdir, counter):
    f = conn.makefile("rb")

    def say(line):
        conn.sendall((line + "\r\n").encode())

    say("220 fake ESMTP")
    rcpts, auth, sender = [], "none", ""
    while True:
        raw = f.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        cmd = line.split(" ", 1)[0].upper()
        if cmd in ("EHLO", "HELO"):
            conn.sendall(b"250-fake\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
        elif cmd == "AUTH":
            parts = line.split()
            if len(parts) >= 3 or (len(parts) == 2 and parts[1].upper() == "PLAIN"):
                if len(parts) == 2:
                    say("334 ")
                    f.readline()
                auth = parts[1].upper()
                say("235 ok")
            else:  # LOGIN: user then password
                say("334 VXNlcm5hbWU6")
                f.readline()
                say("334 UGFzc3dvcmQ6")
                f.readline()
                auth = "LOGIN"
                say("235 ok")
        elif cmd == "MAIL":
            sender = line[10:]
            say("250 ok")
        elif cmd == "RCPT":
            rcpts.append(line[8:])
            say("250 ok")
        elif cmd == "DATA":
            say("354 go")
            data = []
            while True:
                d = f.readline()
                if not d or d in (b".\r\n", b".\n"):
                    break
                data.append(d.decode("utf-8", "replace"))
            with counter["lock"]:
                counter["n"] += 1
                n = counter["n"]
            with open(os.path.join(outdir, "%d.eml" % n), "w", encoding="utf-8") as fh:
                fh.write("X-Fake-From: %s\nX-Fake-Rcpt: %s\nX-Fake-Auth: %s\n" % (sender, ",".join(rcpts), auth))
                fh.write("".join(data))
            say("250 queued")
        elif cmd == "QUIT":
            say("221 bye")
            break
        else:
            say("250 ok")
    conn.close()


def main():
    portfile, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    with open(portfile + ".tmp", "w") as fh:
        fh.write(str(s.getsockname()[1]))
    os.replace(portfile + ".tmp", portfile)
    counter = {"n": 0, "lock": threading.Lock()}
    while True:
        conn, _ = s.accept()
        threading.Thread(target=handle, args=(conn, outdir, counter), daemon=True).start()


if __name__ == "__main__":
    main()
