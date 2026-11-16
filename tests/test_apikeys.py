#!/usr/bin/env python3
"""tests/test_apikeys.py — API keys (lib/apikeys.py, `board apikey`): shown once, only a peppered
hash stored (0600), expiry and revoke, rotate with a grace window, request -> approve, who may do
what (owner / approver / anyone else), audit rows, the gitleaks rule and the redaction of keys.
Every key here is made at run time in a temp home; no key literal is in this file.

  python3 tests/test_apikeys.py [-v]
  APIKEYS_REQUIRE_GITLEAKS=1 makes a missing gitleaks binary a failure instead of a skip (CI).
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-apikeys-test.")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.environ.update({"BOARD_HOME": os.path.join(TMP, "home"), "BOARD_CONFIG_DIR": CFG})
os.environ.pop("BOARD_NOW_MS", None)
OWNER, APPROVER, RM, VIEWER, STRANGER = ("owner@example.com", "approver@example.com", "rm@example.com",
                                         "viewer@example.com", "stranger@example.com")
FLEET = ("owners: [Owner@Example.com]\napprovers: [%s]\nrelease_managers: [%s]\n"
         "serve:\n  tunnel:\n    allowed_emails: [%s]\n" % (APPROVER, RM, VIEWER))
with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
    fh.write(FLEET)

import apikeys  # noqa: E402
import guard  # noqa: E402

BOARD = os.path.join(REPO, "bin", "board")
NOW = 1_800_000_000_000
DAY = 86400 * 1000


def home():
    return os.environ["BOARD_HOME"]


def board(*args, now=NOW, check=None):
    env = dict(os.environ, BOARD_NOW_MS=str(now))
    r = subprocess.run([BOARD, "apikey"] + list(args), capture_output=True, text=True, env=env)
    if check is not None:
        assert r.returncode == check, "board apikey %s: rc %d (want %d)\n%s%s" % (
            " ".join(args), r.returncode, check, r.stdout, r.stderr)
    return r


def create(owner=VIEWER, by=OWNER, name="ci reader", now=NOW, *extra):
    r = board("create", "--name", name, "--owner", owner, "--by", by, "--json", *extra, now=now, check=0)
    d = json.loads(r.stdout)
    return d["id"], d["key"]


def events():
    try:
        with open(os.path.join(home(), "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def tree_bytes():
    out = b""
    for root, _, files in os.walk(home()):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                out += fh.read()
    return out


def record(kid):
    with open(os.path.join(home(), "apikeys", kid + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(home(), ignore_errors=True)
        os.makedirs(home(), mode=0o700)


class ShownOnce(Base):
    def test_key_on_stdout_once_and_nowhere_else(self):
        r = board("create", "--name", "phone", "--owner", VIEWER, "--by", OWNER, check=0)
        key = r.stdout.strip()
        self.assertRegex(key, r"^asb_live_[0-9a-z]{12}_[A-Za-z0-9_-]{43}$")
        self.assertTrue(apikeys.KEY_RE.fullmatch(key))
        self.assertIn("never shown again", r.stderr)
        self.assertNotIn(key, r.stderr)
        secret = key.rsplit("_", 1)[1]
        stored = tree_bytes()
        self.assertNotIn(secret.encode(), stored)          # not in any file: records, pepper, events
        kid = key.split("_")[2]
        for args in (("ls", "--by", OWNER, "--json"), ("ls", "--by", VIEWER), ("show", kid, "--by", VIEWER, "--json"),
                     ("show", kid, "--by", OWNER)):
            out = board(*args, check=0)
            self.assertNotIn(secret, out.stdout + out.stderr, args)
            self.assertNotIn("hash", json.loads(out.stdout)[0] if args[-1] == "--json" and args[0] == "ls"
                             else (json.loads(out.stdout) if args[-1] == "--json" else {}))

    def test_two_keys_differ(self):
        a, b = create()[1], create()[1]
        self.assertNotEqual(a, b)
        self.assertNotEqual(a.split("_")[2], b.split("_")[2])


class HashOnly(Base):
    def test_only_a_peppered_hash_at_0600(self):
        kid, key = create()
        rec = record(kid)
        self.assertNotIn("key", rec)
        self.assertNotIn("secret", rec)
        pep_path = os.path.join(home(), "secrets", "apikey.pepper")
        with open(pep_path, "rb") as fh:
            pep = fh.read()
        self.assertEqual(len(pep), 32)
        self.assertEqual(rec["hash"], hmac.new(pep, key.encode(), hashlib.sha256).hexdigest())
        self.assertNotEqual(rec["hash"], hashlib.sha256(key.encode()).hexdigest())   # peppered, not a bare hash
        for p, mode in ((os.path.join(home(), "apikeys", kid + ".json"), 0o600), (pep_path, 0o600),
                        (os.path.join(home(), "apikeys"), 0o700), (os.path.dirname(pep_path), 0o700)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), mode, p)
        self.assertEqual([n for n in os.listdir(os.path.join(home(), "apikeys")) if ".tmp." in n], [])

    def test_stolen_store_without_pepper_is_useless(self):
        kid, key = create()
        self.assertEqual(apikeys.verify(key, now=NOW)[1], None)
        os.unlink(os.path.join(home(), "secrets", "apikey.pepper"))
        self.assertEqual(apikeys.verify(key, now=NOW), (None, "unknown"))

    def test_open_pepper_refused(self):
        kid, key = create()
        os.chmod(os.path.join(home(), "secrets", "apikey.pepper"), 0o644)
        self.assertEqual(apikeys.verify(key, now=NOW), (None, "unknown"))
        r = board("create", "--name", "x", "--owner", VIEWER, "--by", OWNER)
        self.assertEqual(r.returncode, 1)
        self.assertIn("chmod 600", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_verify_rejects(self):
        kid, key = create()
        self.assertEqual(apikeys.verify(key, now=NOW)[0]["id"], kid)
        tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
        self.assertEqual(apikeys.verify(tampered, now=NOW), (None, "mismatch"))
        self.assertEqual(apikeys.verify("asb_test_" + key[len("asb_live_"):], now=NOW), (None, "wrong_env"))
        self.assertEqual(apikeys.verify(key, kind="test", now=NOW), (None, "wrong_env"))
        self.assertEqual(apikeys.verify(key[:-2], now=NOW), (None, "malformed"))
        self.assertEqual(apikeys.verify(None, now=NOW), (None, "malformed"))
        other = "asb_live_" + "zzzzzzzzzzzz" + key[len("asb_live_") + 12:]
        self.assertEqual(apikeys.verify(other, now=NOW), (None, "unknown"))

    def test_test_kind(self):
        r = board("create", "--name", "demo", "--owner", VIEWER, "--by", OWNER, "--test", check=0)
        key = r.stdout.strip()
        self.assertTrue(key.startswith("asb_test_"))
        self.assertIsNone(apikeys.verify(key, kind="test", now=NOW)[1])
        self.assertEqual(apikeys.verify(key, now=NOW), (None, "wrong_env"))

    def test_last_used_flushed_at_most_once_a_minute(self):
        kid, _ = create()
        self.assertTrue(apikeys.mark_used(kid, now=NOW + 1000))
        self.assertFalse(apikeys.mark_used(kid, now=NOW + 30000))
        self.assertEqual(record(kid)["last_used_at"], NOW + 1000)
        self.assertTrue(apikeys.mark_used(kid, now=NOW + 62000))
        self.assertEqual(record(kid)["last_used_at"], NOW + 62000)


class ExpiryAndRevoke(Base):
    def test_default_expiry_and_limits(self):
        kid, key = create()
        self.assertEqual(record(kid)["expires_at"], NOW + 90 * DAY)
        for bad in ("never", "0d", "366d", "2020-01-01", "2026-02-30", "soon"):
            r = board("create", "--name", "x", "--owner", VIEWER, "--by", OWNER, "--expires", bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertEqual(r.stdout, "", bad)
        kid2, _ = create(OWNER, OWNER, "dated", NOW, "--expires", "2027-01-15")
        self.assertEqual(apikeys.fmt_time(record(kid2)["expires_at"]), "2027-01-16 00:00Z")

    def test_expired_key_stops_working(self):
        kid, key = create(VIEWER, OWNER, "short", NOW, "--expires", "1d")
        self.assertIsNone(apikeys.verify(key, now=NOW + DAY - 1)[1])
        self.assertEqual(apikeys.verify(key, now=NOW + DAY), (None, "expired"))
        self.assertEqual(json.loads(board("ls", "--by", VIEWER, "--json", now=NOW + DAY).stdout), [])
        rows = json.loads(board("ls", "--by", VIEWER, "--json", "--all", now=NOW + DAY).stdout)
        self.assertEqual([(r["id"], r["status"]) for r in rows], [(kid, "expired")])
        # an owner extends it (set or extend expiry)
        board("expire", kid, "--expires", "30d", "--by", OWNER, now=NOW + DAY, check=0)
        self.assertIsNone(apikeys.verify(key, now=NOW + 2 * DAY)[1])
        self.assertEqual(board("expire", kid, "--expires", "400d", "--by", OWNER).returncode, 2)

    def test_revoke_is_immediate(self):
        kid, key = create()
        board("revoke", kid, "--by", VIEWER, check=0)
        self.assertEqual(apikeys.verify(key, now=NOW), (None, "revoked"))
        rec = record(kid)
        self.assertEqual((rec["state"], rec["revoked_by"], rec["revoked_at"]), ("revoked", VIEWER, NOW))
        self.assertEqual(board("revoke", kid, "--by", VIEWER).returncode, 1)
        self.assertEqual(board("expire", kid, "--expires", "5d", "--by", OWNER).returncode, 1)
        self.assertEqual(board("rotate", kid, "--by", OWNER).returncode, 1)
        ev = [e for e in events() if e["action"] == "revoke"]
        self.assertEqual(ev[-1]["key_id"], kid)
        self.assertEqual(ev[-1]["result"], "ok")


class Rotate(Base):
    def test_rotate_with_grace(self):
        kid, old = create(VIEWER, OWNER, "rot", NOW, "--scopes", "read:state,read:tasks")
        r = board("rotate", kid, "--by", OWNER, "--grace", "24h", "--json", check=0)
        new = json.loads(r.stdout)
        self.assertIn("keeps working until", r.stderr)
        self.assertEqual(new["scopes"], ["read:state", "read:tasks"])
        self.assertEqual(new["owner"], VIEWER)
        self.assertIsNone(apikeys.verify(new["key"], now=NOW)[1])
        self.assertIsNone(apikeys.verify(old, now=NOW + DAY - 1)[1])                 # still in grace
        self.assertEqual(apikeys.verify(old, now=NOW + DAY), (None, "revoked"))     # grace over
        self.assertEqual((record(kid)["successor"], record(new["id"])["predecessor"]), (new["id"], kid))
        rows = {r["id"]: r for r in json.loads(board("ls", "--by", VIEWER, "--all", "--json", now=NOW + DAY).stdout)}
        self.assertEqual((rows[kid]["status"], rows[kid]["revoked_at"]), ("revoked", NOW + DAY))
        self.assertEqual(board("rotate", kid, "--by", OWNER).returncode, 1)          # already replaced

    def test_rotate_without_grace_revokes_now(self):
        kid, old = create()
        new = json.loads(board("rotate", kid, "--by", OWNER, "--json", check=0).stdout)
        self.assertEqual(apikeys.verify(old, now=NOW), (None, "revoked"))
        self.assertIsNone(apikeys.verify(new["key"], now=NOW)[1])
        self.assertEqual(board("rotate", new["id"], "--by", OWNER, "--grace", "8d").returncode, 2)

    def test_change_scopes_revokes_and_reissues(self):
        kid, old = create()
        r = board("scopes", kid, "--scopes", "read:state,tickets:read", "--by", OWNER, "--json", check=0)
        new = json.loads(r.stdout)
        self.assertNotEqual(new["id"], kid)
        self.assertEqual(new["scopes"], ["read:state", "tickets:read"])
        self.assertEqual(apikeys.verify(old, now=NOW), (None, "revoked"))
        self.assertEqual(apikeys.verify(new["key"], now=NOW)[0]["scopes"], ["read:state", "tickets:read"])
        self.assertEqual(record(new["id"])["expires_at"], record(kid)["expires_at"])

    def test_no_authority_scopes(self):
        for s in ("decisions:answer", "apikeys:admin", "away", "press", "consensus", "production", "admin"):
            self.assertNotIn(s, apikeys.SCOPES)
            r = board("create", "--name", "x", "--owner", VIEWER, "--by", OWNER, "--scopes", s)
            self.assertEqual(r.returncode, 2, s)
            self.assertIn("unknown scope", r.stderr)
        self.assertFalse([s for s in apikeys.SCOPES if re.search(r"answer|admin|key|press|away|gate", s)])


class RequestApprove(Base):
    def test_request_then_approve(self):
        r = board("request", "--name", "my script", "--by", VIEWER, "--expires", "10d", "--json", check=0)
        req = json.loads(r.stdout)
        self.assertIn("pending", r.stderr)
        self.assertEqual(req["owner"], VIEWER)
        self.assertEqual(apikeys.verify(req["key"], now=NOW), (None, "pending"))
        # an approver who is not an owner cannot approve; nothing changes
        before = record(req["id"])
        self.assertEqual(board("approve", req["id"], "--by", APPROVER).returncode, 4)
        self.assertEqual(record(req["id"]), before)
        later = NOW + 3 * DAY
        board("approve", req["id"], "--by", "board:" + OWNER, now=later, check=0)
        rec = record(req["id"])
        self.assertEqual((rec["state"], rec["approved_by"], rec["expires_at"]), ("active", OWNER, later + 10 * DAY))
        self.assertIsNone(apikeys.verify(req["key"], now=later)[1])
        self.assertEqual(board("approve", req["id"], "--by", OWNER).returncode, 1)
        ev = [e for e in events() if e["action"] == "approve" and e["result"] == "ok"][-1]
        self.assertEqual((ev["by"], ev["via"], ev["owner"]), (OWNER, "board", VIEWER))

    def test_deny_a_request_by_revoking_it(self):
        req = json.loads(board("request", "--name", "nope", "--by", STRANGER, "--json", check=0).stdout)
        board("revoke", req["id"], "--by", OWNER, check=0)
        self.assertEqual(record(req["id"])["revoke_reason"], "denied")
        self.assertEqual(board("approve", req["id"], "--by", OWNER).returncode, 1)
        self.assertEqual(apikeys.verify(req["key"], now=NOW), (None, "revoked"))

    def test_pending_requests_are_capped(self):
        for i in range(apikeys.MAX_PENDING_PER_OWNER):
            board("request", "--name", "r%d" % i, "--by", VIEWER, check=0)
        r = board("request", "--name", "one more", "--by", VIEWER)
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_request_is_only_for_yourself(self):
        r = board("request", "--name", "x", "--by", VIEWER, "--owner", OWNER)
        self.assertEqual(r.returncode, 2)


class Authorization(Base):
    def test_owner_issues_to_anyone_case_insensitively(self):
        kid, _ = create(STRANGER, "OWNER@example.COM")
        self.assertEqual(record(kid)["owner"], STRANGER)
        self.assertEqual(record(kid)["created_by"], OWNER)

    def test_approver_issues_to_listed_people_or_self(self):
        for who in (APPROVER, RM, VIEWER, OWNER):
            create(who, APPROVER)
        r = board("create", "--name", "x", "--owner", STRANGER, "--by", APPROVER)
        self.assertEqual((r.returncode, r.stdout), (4, ""))
        self.assertIn("not a listed person", r.stderr)

    def test_others_can_only_request(self):
        for by in (VIEWER, RM, STRANGER):
            r = board("create", "--name", "x", "--owner", by, "--by", by)
            self.assertEqual((r.returncode, r.stdout), (4, ""), by)
            self.assertIn("request", r.stderr)
        self.assertFalse(os.path.isdir(os.path.join(home(), "apikeys")) and os.listdir(os.path.join(home(), "apikeys")))
        refused = [e for e in events() if e["result"] == "refused"]
        self.assertEqual(len(refused), 3)
        self.assertTrue(all(e["reason"] == "not_authorized" and e["type"] == "apikey" for e in refused))

    def test_visibility(self):
        mine, _ = create(VIEWER)
        theirs, _ = create(RM)
        apprs, _ = create(APPROVER)
        ids = lambda by: sorted(r["id"] for r in json.loads(board("ls", "--by", by, "--json", check=0).stdout))
        self.assertEqual(ids(VIEWER), [mine])
        self.assertEqual(ids(APPROVER), [apprs])                     # an approver sees only their own keys
        self.assertEqual(ids(OWNER), sorted([mine, theirs, apprs]))
        self.assertEqual(ids("board:" + OWNER), sorted([mine, theirs, apprs]))
        self.assertEqual(sorted(r["id"] for r in json.loads(
            board("ls", "--by", OWNER, "--owner", RM, "--json", check=0).stdout)), [theirs])
        r = board("show", theirs, "--by", VIEWER)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("no such key", r.stderr)
        board("show", theirs, "--by", RM, check=0)
        self.assertIn("ls: --by", board("ls").stderr)

    def test_revoke_rules(self):
        theirs, _ = create(RM)
        unknown = board("revoke", "zzzzzzzzzzzz", "--by", VIEWER)
        for by in (VIEWER, APPROVER):                # someone else's key: exactly like an unknown id
            r = board("revoke", theirs, "--by", by)
            self.assertEqual((r.returncode, r.stdout), (unknown.returncode, unknown.stdout))
            self.assertEqual(r.stderr.replace(theirs, "zzzzzzzzzzzz"), unknown.stderr)
        self.assertEqual(apikeys.status(record(theirs), NOW), "active")
        self.assertEqual([e["reason"] for e in events() if e["result"] == "refused"], ["not_authorized"] * 2)
        issued, _ = create(RM, APPROVER)
        board("revoke", issued, "--by", APPROVER, check=0)            # an approver revokes a key they issued
        board("revoke", theirs, "--by", OWNER, check=0)
        own, _ = create(VIEWER)
        board("revoke", own, "--by", VIEWER, check=0)

    def test_admin_actions_are_owner_only(self):
        kid, _ = create(APPROVER, APPROVER)
        for args in (("rotate", kid), ("expire", kid, "--expires", "5d"), ("scopes", kid, "--scopes", "read:tasks")):
            for by in (APPROVER, VIEWER, "local"):
                before = record(kid)
                r = board(*(args + ("--by", by)))
                self.assertEqual((r.returncode, r.stdout), (4, ""), (args, by))
                self.assertEqual(record(kid), before)
        self.assertEqual(len(os.listdir(os.path.join(home(), "apikeys"))), 1)

    def test_no_owners_fails_closed(self):
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write("approvers: [%s]\n" % APPROVER)
        try:
            req = json.loads(board("request", "--name", "x", "--by", VIEWER, "--json", check=0).stdout)
            self.assertEqual(board("approve", req["id"], "--by", APPROVER).returncode, 4)
            self.assertEqual(board("approve", req["id"], "--by", OWNER).returncode, 4)
            create(APPROVER, APPROVER)                                  # approvers still issue to themselves
        finally:
            with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
                fh.write(FLEET)

    def test_bad_identities(self):
        for by in ("", "not-an-email", "a@b", "x@example.com\nmore"):
            r = board("create", "--name", "x", "--owner", VIEWER, "--by", by)
            self.assertEqual(r.returncode, 2, repr(by))
        self.assertEqual(board("revoke", "../../etc", "--by", OWNER).returncode, 1)
        self.assertEqual(board("create", "--name", "x\x01", "--owner", VIEWER, "--by", OWNER).returncode, 2)
        self.assertEqual(board("create", "--name", "x" * 61, "--owner", VIEWER, "--by", OWNER).returncode, 2)

    def test_every_change_audited_without_secrets(self):
        kid, key = create()
        board("rotate", kid, "--by", OWNER, "--grace", "1h", check=0)
        succ = record(kid)["successor"]
        board("expire", succ, "--expires", "20d", "--by", OWNER, check=0)
        req = json.loads(board("request", "--name", "r", "--by", VIEWER, "--json", check=0).stdout)
        board("approve", req["id"], "--by", OWNER, check=0)
        board("revoke", succ, "--by", OWNER, check=0)
        acts = [e["action"] for e in events() if e["result"] == "ok"]
        self.assertEqual(acts, ["create", "rotate", "expire", "request", "approve", "revoke"])
        with open(os.path.join(home(), "events.jsonl"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(key.rsplit("_", 1)[1], raw)
        self.assertNotIn(req["key"].rsplit("_", 1)[1], raw)
        self.assertNotIn(record(kid)["hash"], raw)


class Review(Base):
    """api-review of A1: lapsing requests, approve output, pepper safety, invisible characters."""

    def request(self, by=VIEWER, now=NOW, *extra):
        r = board("request", "--name", "req", "--by", by, "--json", *extra, now=now, check=0)
        return json.loads(r.stdout)

    def test_request_lapses(self):
        req = self.request()
        for later in (NOW + 14 * DAY, NOW + 3 * 365 * DAY):
            before = record(req["id"])
            r = board("approve", req["id"], "--by", OWNER, now=later)
            self.assertEqual(r.returncode, 1)
            self.assertIn("request expired; ask again", r.stderr)
            self.assertEqual(record(req["id"]), before)
            self.assertEqual(apikeys.verify(req["key"], now=later), (None, "expired"))
            self.assertEqual(board("expire", req["id"], "--expires", "30d", "--by", OWNER, now=later).returncode, 1)
        rows = json.loads(board("ls", "--by", VIEWER, "--all", "--json", now=NOW + 14 * DAY).stdout)
        self.assertEqual([(x["status"], x["reason"]) for x in rows], [("expired", "lapsed")])
        self.assertEqual(json.loads(board("ls", "--by", VIEWER, "--json", now=NOW + 14 * DAY).stdout), [])
        ev = [e for e in events() if e["action"] == "approve"]
        self.assertEqual({(e["result"], e["reason"]) for e in ev}, {("refused", "lapsed")})
        fresh = self.request(VIEWER, NOW + DAY)
        board("approve", fresh["id"], "--by", OWNER, now=NOW + 14 * DAY, check=0)    # 13 days old: fine

    def test_request_ttl_from_config(self):
        with open(os.path.join(CFG, "fleet.yaml"), "a") as fh:
            fh.write("apikeys:\n  request_ttl_days: 2\n")
        try:
            req = self.request()
            self.assertEqual(board("approve", req["id"], "--by", OWNER, now=NOW + 2 * DAY).returncode, 1)
        finally:
            with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
                fh.write(FLEET)

    def test_approve_shows_scopes_and_age_and_may_narrow(self):
        req = self.request(VIEWER, NOW, "--scopes", "read:state,read:tasks,tickets:read")
        r = board("approve", req["id"], "--by", OWNER, "--scopes", "read:state,read:decisions", now=NOW + DAY)
        self.assertEqual(r.returncode, 2)
        self.assertIn("only narrow", r.stderr)
        r = board("approve", req["id"], "--by", OWNER, "--scopes", "read:tasks", now=NOW + DAY + 3600000, check=0)
        self.assertIn("scopes read:tasks (requested read:state,read:tasks,tickets:read)", r.stdout)
        self.assertIn("requested 1d 1h ago", r.stdout)
        self.assertEqual(record(req["id"])["scopes"], ["read:tasks"])
        ev = [e for e in events() if e["action"] == "approve"][-1]
        self.assertEqual((ev["scopes"], ev["requested_scopes"], ev["request_age_ms"]),
                         (["read:tasks"], ["read:state", "read:tasks", "tickets:read"], DAY + 3600000))
        other = self.request(RM)
        r = board("approve", other["id"], "--by", OWNER, check=0)
        self.assertIn("scopes read:state, requested 0h 0m ago", r.stdout)

    def test_withdraw_vs_deny(self):
        mine, theirs = self.request(VIEWER), self.request(RM)
        self.assertIn("withdrawn", board("revoke", mine["id"], "--by", VIEWER, check=0).stdout)
        self.assertIn("denied", board("revoke", theirs["id"], "--by", OWNER, check=0).stdout)
        self.assertEqual((record(mine["id"])["revoke_reason"], record(theirs["id"])["revoke_reason"]),
                         ("withdrawn", "denied"))

    def test_no_new_pepper_over_existing_keys(self):
        kid, key = create()
        pep = os.path.join(home(), "secrets", "apikey.pepper")
        os.unlink(pep)
        for args in (("create", "--name", "x", "--owner", VIEWER, "--by", OWNER), ("request", "--name", "x", "--by", RM),
                     ("rotate", kid, "--by", OWNER)):
            r = board(*args)
            self.assertEqual((r.returncode, r.stdout), (1, ""), args)
            self.assertIn("apikey.pepper is missing but 1 key(s) exist", r.stderr)
        self.assertFalse(os.path.lexists(pep))
        self.assertEqual(len(os.listdir(os.path.join(home(), "apikeys"))), 1)

    def test_pepper_written_whole_and_never_through_a_symlink(self):
        create()
        d = os.path.join(home(), "secrets")
        self.assertEqual(os.listdir(d), ["apikey.pepper"])                 # no temp file left
        self.assertEqual(os.path.getsize(os.path.join(d, "apikey.pepper")), 32)
        kid, key = create()
        real = os.path.join(d, "apikey.pepper")
        os.rename(real, os.path.join(d, "elsewhere"))
        os.symlink(os.path.join(d, "elsewhere"), real)
        self.assertEqual(apikeys.verify(key, now=NOW), (None, "unknown"))
        r = board("create", "--name", "x", "--owner", VIEWER, "--by", OWNER)
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)

    def test_invisible_characters_refused(self):
        for name in ("a\u202eb", "zero\u200bwidth", "line\u2028sep", "x\ufeff"):
            r = board("create", "--name", name, "--owner", VIEWER, "--by", OWNER)
            self.assertEqual(r.returncode, 2, repr(name))
        for who in ("vie\u200bwer@example.com", "viewer@exam\u202eple.com"):
            r = board("create", "--name", "x", "--owner", who, "--by", OWNER)
            self.assertEqual(r.returncode, 2, repr(who))
            self.assertEqual(board("request", "--name", "x", "--by", who).returncode, 2)

    def test_refused_show_is_audited(self):
        theirs, _ = create(RM)
        self.assertEqual(board("show", theirs, "--by", VIEWER).returncode, 1)
        ev = [e for e in events() if e["action"] == "show"]
        self.assertEqual([(e["result"], e["reason"], e["by"]) for e in ev], [("refused", "not_authorized", VIEWER)])


class Gitleaks(unittest.TestCase):
    def _key(self):
        return apikeys.new_key(tempfile.mkdtemp(dir=TMP), "live")[1]

    def test_rule_matches_the_key_shape(self):
        with open(os.path.join(REPO, ".gitleaks.toml"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r'id = "agent-switchboard-api-key"[\s\S]*?regex = \'\'\'(.+?)\'\'\'', src)
        self.assertTrue(m, "the gitleaks rule is missing")
        self.assertEqual(m.group(1), apikeys.KEY_RE.pattern.replace("([", "[").replace("})", "}"))
        self.assertTrue(re.fullmatch(m.group(1), self._key()))

    def test_gitleaks_catches_a_key_outside_fixtures(self):
        exe = shutil.which("gitleaks")
        if not exe:
            if os.environ.get("APIKEYS_REQUIRE_GITLEAKS") == "1":
                self.fail("gitleaks is not installed")
            self.skipTest("gitleaks missing")
        repo = tempfile.mkdtemp(dir=TMP)
        git = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
                                         "-c", "commit.gpgsign=false"] + list(a), check=True, capture_output=True)
        git("init", "-q")
        shutil.copy(os.path.join(REPO, ".gitleaks.toml"), os.path.join(repo, ".gitleaks.toml"))
        os.makedirs(os.path.join(repo, "tests", "fixtures"))
        with open(os.path.join(repo, "tests", "fixtures", "mock-key.json"), "w") as fh:
            json.dump({"demo_key": self._key()}, fh)
        git("add", "-A")
        git("commit", "-qm", "fixture")

        def scan():
            rep = os.path.join(repo, ".report.json")
            r = subprocess.run([exe, "git", "--no-banner", "-c", ".gitleaks.toml", "--report-format", "json",
                                "--report-path", rep, "."], cwd=repo, capture_output=True, text=True)
            with open(rep, encoding="utf-8") as fh:
                found = json.load(fh) or []
            os.unlink(rep)
            return r.returncode, found
        self.assertEqual(scan(), (0, []))                               # allowlisted fixture
        with open(os.path.join(repo, "client.py"), "w") as fh:
            fh.write("# a pasted key\nBOARD = %r\n" % self._key())
        git("add", "client.py")
        git("commit", "-qm", "oops")
        rc, found = scan()
        self.assertEqual(rc, 1)
        self.assertIn(("agent-switchboard-api-key", "client.py"), {(f["RuleID"], f["File"]) for f in found})
        self.assertFalse([f for f in found if f["File"].startswith("tests/fixtures/")])


class Redaction(unittest.TestCase):
    def test_guard_redacts_keys(self):
        home_ = tempfile.mkdtemp(dir=TMP)
        for kind in ("live", "test"):
            key = apikeys.new_key(home_, kind)[1]
            for text in (key, "use %s please" % key, "Authorization: Bearer " + key, '{"k": "%s"}' % key,
                         "curl -H 'X-Key: %s' http://127.0.0.1/api/v1/whoami" % key):
                out = guard.scrub(text)
                self.assertNotIn(key.rsplit("_", 1)[1], out, text)
                self.assertIn("[REDACTED]", out)
            r = subprocess.run([sys.executable, os.path.join(REPO, "lib", "guard.py"), "scrub-out"],
                               input=json.dumps({"note": "key " + key}), capture_output=True, text=True)
            self.assertNotIn(key.rsplit("_", 1)[1], r.stdout)

    def test_hook_redacts_keys(self):
        if not shutil.which("jq"):
            self.skipTest("jq missing")
        with open(os.path.join(REPO, "hooks", "board-hook.sh"), encoding="utf-8") as fh:
            src = fh.read()
        jq_def = src.split("IFS= read -r -d '' REDACT <<'JQ' || true\n", 1)[1].split("\nJQ\n", 1)[0]
        key = apikeys.new_key(tempfile.mkdtemp(dir=TMP), "live")[1]
        r = subprocess.run(["jq", "-r", jq_def + " .[] | redact"], input=json.dumps(["export K=x; echo " + key]),
                           capture_output=True, text=True, check=True)
        self.assertNotIn(key.rsplit("_", 1)[1], r.stdout)
        self.assertIn("[REDACTED]", r.stdout)


class Cli(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([BOARD, "apikey", "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("rotate ID", r.stdout)
        self.assertIn("read:state", r.stdout)
        self.assertEqual(subprocess.run([BOARD, "apikey", "nope"], capture_output=True).returncode, 2)
        self.assertIn("board apikey", subprocess.run([BOARD, "--help"], capture_output=True, text=True).stdout)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
