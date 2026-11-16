#!/usr/bin/env python3
"""PagerDuty-style alerting for agent-switchboard (see docs/contract.md, "Alerting & escalation"
and "Usage meter"). Python stdlib only (+ pyyaml through lib/fleet.py). Costs zero tokens.

Usage:
  notify.py send --level info|warn|high|urgent --key KEY --title T [--body B] [--url U]
                 [--board ID] [--kind KIND] [--escalate|--no-escalate] [--json]
  notify.py ack KEY [KEY...]        stop escalation (KEY may be a glob, e.g. 'session:API:*')
  notify.py tick [--json]           escalation pass + snapshot discovery + usage thresholds
  notify.py test --channel slack|desktop|email|line|sns|webhook [--level L]
  notify.py status [--json]         alerts.json, which channels are enabled (never values) and
                                    the notification controls (mutes, min levels, presence)
  notify.py gate --channel C --level L [--key K]
                                    exit 0 when C may deliver L now, 10 when the notification
                                    controls hold it (logged); used by `board notify`

State:  $BOARD_HOME/alerts.json   {key: {level, first_at, last_at, acked_at, sent:{level: ms}, ...}}
Log:    $BOARD_HOME/events.jsonl  {ts, type:"alert", key, channel, level, ok, error}
Config: $BOARD_CONFIG_DIR/.env (channel credentials; the process environment wins) and the
        `alerting:` block of fleet.yaml, on top of an optional alerting.yaml in the same dir.
Test hooks: BOARD_NOW_MS (fake clock, epoch ms).

Board link: when a public board URL is set (BOARD_PUBLIC_URL, or `board_url` in fleet.yaml; see
lib/fleet.py), every delivery carries <board>/?d=<id> (decision), ?c= (consensus), ?m= (message)
or the board root as its `url`, worked out at delivery time from the alert's `board` id or its key.
A caller's own --url (an artifact or session link) is then sent as `ref`, and both lines are
labelled ("Decide on the board: …", "Open the session: …"). Unset: no link, no labels, and the
message is byte for byte what it was before.

Notification controls (lib/prefs.py; docs/contract.md, "Notification controls"): at delivery
time, after dedupe and quiet hours, every routed channel is checked against the mutes, the
per-channel and global min levels and the presence policy. A held channel is logged as
{type: "alert-suppressed", key, channel, level, reason} with reason muted:<channel|all>,
below_min_level or owner_present. When every routed channel is held, the level counts as sent
(status "suppressed"): nothing is replayed later, and an alert that stays open goes out at its next
escalation step or repeat. Urgent breaks through mutes unless a mute says include_urgent.

A send never raises: every adapter failure is caught, logged and reported in the result.
"""
import argparse
import base64
import datetime as dt
import fcntl
import fnmatch
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import guard  # noqa: E402
import prefs  # noqa: E402

LEVELS = ["info", "warn", "high", "urgent"]
CHANNELS = ["slack", "desktop", "email", "line", "sns", "webhook"]
ADAPTER_TIMEOUT = 5          # seconds, per adapter
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_MAX = 5000
KEEP_MS = 7 * 24 * 3600 * 1000  # resolved/acked entries are pruned after a week

DEFAULTS = {
    "routes": {
        "info": ["slack"],
        "warn": ["slack", "desktop"],
        "high": ["slack", "desktop", "email"],
        "urgent": ["slack", "desktop", "email", "line", "sns", "webhook"],
    },
    "quiet_hours": None,          # e.g. "23:00-08:00" (local time); only urgent passes
    "repeat_minutes": 60,         # re-alert at the same level after this long (0 = never)
    "retry_minutes": 5,           # retry a delivery whose channels all failed
    "escalate_minutes": {"high": 10, "urgent": 30, "permission_urgent": 10},
    "thresholds": [80, 90, 95],   # usage %: lowest -> warn, middle -> high, highest -> urgent
    "mention_from": "warn",       # SLACK_MENTION is prepended from this level up
    "subject_prefix": "[agent-switchboard]",
    "min_level": {},              # floors {all|channel: level}; `board notify-level` overrides them
    "presence": {"reduce": True, "min_level": "high", "window_seconds": 120, "idle_seconds": 900},
}

ENV_PREFIXES = ("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")
SECRET_HINTS = ("TOKEN", "PASSWORD", "SECRET", "WEBHOOK", "URL", "KEY")
WINDOWS = [("five_hour", "5h", "5h", 5 * 3600 * 1000), ("seven_day", "7d", "weekly", 7 * 24 * 3600 * 1000)]


# ------------------------------------------------------------------ basics

def now_ms():
    fake = os.environ.get("BOARD_NOW_MS")
    if fake:
        try:
            return int(fake)
        except ValueError:
            pass
    return int(time.time() * 1000)


def rank(level):
    return LEVELS.index(level) if level in LEVELS else 0


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def home():
    return fleet.ensure_home()


def log_event(ev):
    try:
        with open(os.path.join(home(), "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def to_ms(v):
    """Epoch seconds or ms (number or numeric string), or an ISO-8601 string -> epoch ms."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v * 1000) if v < 1e12 else int(v)
    try:
        return to_ms(float(v))
    except (TypeError, ValueError):
        pass
    try:
        return int(dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def fmt_time(ms, weekday=False):
    if not ms:
        return "?"
    t = dt.datetime.fromtimestamp(ms / 1000)
    return t.strftime("%a %H:%M" if weekday else "%H:%M")


# ------------------------------------------------------------------ config

def load_env():
    """Channel settings: <config dir>/.env, overridden by the process environment."""
    env = fleet.read_dotenv()
    for k, v in os.environ.items():
        if k.startswith(ENV_PREFIXES) and v:
            env[k] = v
    return env


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(fleet.config_dir(), "alerting.yaml")
    if os.path.isfile(path):
        try:
            import yaml  # noqa: WPS433
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if isinstance(data, dict) and isinstance(data.get("alerting"), dict):
                data = data["alerting"]
            if isinstance(data, dict):
                cfg = fleet._merge(cfg, data)  # noqa: SLF001
        except Exception as exc:  # pragma: no cover
            sys.stderr.write("alerting.yaml: %s\n" % exc)
    fl = fleet.load_fleet()
    block = fl.get("alerting")
    if isinstance(block, dict):
        cfg = fleet._merge(cfg, block)  # noqa: SLF001
    cfg["board_url"] = fleet.board_url(fl)  # validated; None = alerts carry no board link
    cfg["approvers"] = fleet.id_list(fl, "approvers")  # whose presence on the board counts
    return cfg


def scrub(text, env):
    """An adapter error, before it is logged: channel credentials (process env included) become
    ***, then the board's secret guard (lib/guard.py) runs over what is left."""
    text = str(text or "")
    for k, v in env.items():
        if v and len(v) >= 6 and any(h in k for h in SECRET_HINTS):
            text = text.replace(v, "***")
    return guard.scrub(text)[:300]


def in_quiet_hours(cfg, now):
    spec = cfg.get("quiet_hours")
    if not spec or "-" not in str(spec):
        return False
    try:
        a, b = [x.strip() for x in str(spec).split("-", 1)]
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
    except ValueError:
        return False
    t = dt.datetime.fromtimestamp(now / 1000)
    cur, start, end = t.hour * 60 + t.minute, ah * 60 + am, bh * 60 + bm
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def threshold_levels(cfg):
    """[(pct, level)] ascending. Accepts a list [80, 90, 95] or a dict {warn: 80, ...}."""
    th = cfg.get("thresholds") or []
    if isinstance(th, dict):
        out = [(float(v), k) for k, v in th.items() if k in LEVELS and v is not None]
        return sorted(out)
    vals = sorted(float(x) for x in th)
    names = ["warn", "high", "urgent"]
    out = []
    for i, v in enumerate(vals):
        j = i - (len(vals) - len(names))
        out.append((v, names[j] if j >= 0 else "info"))
    return out


# ------------------------------------------------------------------ transport

def _cfg_quote(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def http_post(url, payload, headers=None):
    """POST JSON. Returns (ok, error). Secrets (URL, headers) never go on argv: curl reads them
    from a config on stdin. Falls back to urllib when curl is missing."""
    body = json.dumps(payload, ensure_ascii=True)
    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    curl = shutil.which("curl")
    if curl:
        conf = ["url = %s" % _cfg_quote(url)]
        conf += ["header = %s" % _cfg_quote("%s: %s" % (k, v)) for k, v in headers.items()]
        conf.append("data-binary = %s" % _cfg_quote(body))
        try:
            r = subprocess.run([curl, "-sS", "-o", os.devnull, "-w", "%{http_code}", "-m", str(ADAPTER_TIMEOUT),
                                "--connect-timeout", "3", "-K", "-"], input="\n".join(conf) + "\n",
                               capture_output=True, text=True, timeout=ADAPTER_TIMEOUT + 2)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        code = (r.stdout or "").strip()[-3:]
        if code.startswith("2"):
            return True, None
        if r.returncode:
            return False, "curl exit %d" % r.returncode
        return False, "http %s" % (code or "000")
    import urllib.error  # noqa: WPS433
    import urllib.request  # noqa: WPS433
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=ADAPTER_TIMEOUT) as resp:  # noqa: S310
            return (200 <= resp.status < 300), (None if resp.status < 300 else "http %d" % resp.status)
    except urllib.error.HTTPError as exc:
        return False, "http %d" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def spawn_detached(argv, cwd=None):
    """Start argv fully detached (re-parented to init, no zombie) and return at once."""
    try:
        p = subprocess.Popen(["/bin/sh", "-c", '"$@" </dev/null >/dev/null 2>&1 &', "sh"] + argv,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=cwd, start_new_session=True)
        p.wait(timeout=3)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


# ------------------------------------------------------------------ adapters

def labelled(label, text):
    """'Label: text' when the line has a label (only with a board link), else the text as is."""
    return "%s: %s" % (label, text) if label else text


def link_lines(msg, render=str):
    """The url line and the ref line, each labelled when a board link is in play."""
    out = []
    if msg.get("url"):
        out.append(labelled(msg.get("url_label"), render(msg["url"])))
    if msg.get("ref"):
        out.append(labelled(msg.get("ref_label") or "Link", render(msg["ref"])))
    return out


def text_of(msg, with_level=True):
    head = ("[%s] " % msg["level"].upper() if with_level else "") + msg["title"]
    parts = [head]
    if msg.get("body"):
        parts.append(msg["body"])
    parts += link_lines(msg)
    return "\n".join(parts)


def subject_of(msg, cfg):
    lvl = msg["level"]
    tag = (lvl.upper() + ": ") if lvl in ("high", "urgent") else ""
    return ("%s %s%s" % (cfg.get("subject_prefix") or "", tag, msg["title"])).strip()


def slack_escape(s):
    """Slack mrkdwn control characters. Titles and bodies come from sessions and pages (lower
    trust), so they must not be able to inject <!channel>, <@user> or <url|fake text> links."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_link(url):
    """A link only for a plain http(s) URL; anything else is shown as escaped text."""
    u = str(url).strip()
    if re.fullmatch(r"https?://[^\s<>|]+", u):
        return "<%s>" % u.replace("&", "&amp;")  # Slack wants & escaped inside links too
    return slack_escape(u)


def a_slack(msg, env, cfg):
    mention = env.get("SLACK_MENTION", "") if rank(msg["level"]) >= rank(cfg.get("mention_from") or "warn") else ""
    icon = {"urgent": ":rotating_light:", "high": ":warning:", "warn": ":bell:"}.get(msg["level"], "")
    parts = [("[%s] " % msg["level"].upper()) + slack_escape(msg["title"])]
    if msg.get("body"):
        parts.append(slack_escape(msg["body"]))
    # Labels are our own constants; links (board and ref alike) go through slack_link, so only a
    # plain http(s) URL becomes a link and anything else is escaped text.
    parts += link_lines(msg, slack_link)
    # The mention is added after escaping: it is the only markup that comes from our own config.
    text = " ".join(x for x in (mention, icon, "\n".join(parts)) if x)
    return http_post(env["SLACK_WEBHOOK_URL"], {"text": text})


TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
function D($s) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)) }
$t = D '%(title)s'; $b = D '%(body)s'
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $e = [System.Security.SecurityElement]
  $x = "<toast scenario='%(scenario)s'><visual><binding template='ToastGeneric'><text>$($e::Escape($t))</text><text>$($e::Escape($b))</text></binding></visual><audio src='%(sound)s'/></toast>"
  $d = New-Object Windows.Data.Xml.Dom.XmlDocument; $d.LoadXml($x)
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($d))
} catch {
  [System.Media.SystemSounds]::Exclamation.Play()
  try { & msg.exe * /TIME:60 ("$t`n$b") } catch { }
}
"""


def a_desktop(msg, env, cfg):
    title = "%s %s" % (cfg.get("subject_prefix") or "", msg["title"])
    body = "\n".join(x for x in [msg.get("body")] + link_lines(msg) if x)
    ps = shutil.which("powershell.exe")
    if ps:
        urgent = msg["level"] == "urgent"
        script = TOAST_PS % {
            "title": base64.b64encode(title.strip()[:200].encode()).decode(),
            "body": base64.b64encode((body or msg["level"])[:600].encode()).decode(),
            "scenario": "reminder" if urgent else "default",
            "sound": "ms-winsoundevent:Notification.Looping.Alarm" if urgent else "ms-winsoundevent:Notification.Default",
        }
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        return spawn_detached([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-WindowStyle", "Hidden", "-EncodedCommand", enc], cwd=os.path.dirname(ps))
    if shutil.which("notify-send"):
        urg = "critical" if msg["level"] == "urgent" else "normal"
        return spawn_detached(["notify-send", "-u", urg, "--", title.strip(), body])
    if shutil.which("osascript"):
        safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        return spawn_detached(["osascript", "-e", 'display notification "%s" with title "%s" sound name "Glass"'
                               % (safe(body), safe(title.strip()))])
    return False, "no desktop notifier"


def a_email(msg, env, cfg):
    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT") or 587)
    sec = (env.get("SMTP_SECURITY") or "").lower() or {465: "ssl", 587: "starttls"}.get(port, "auto")
    creds = bool(env.get("SMTP_USER") and env.get("SMTP_PASSWORD"))
    if sec not in ("ssl", "starttls", "auto", "none"):
        return False, "unknown SMTP_SECURITY %r (ssl, starttls, auto or none)" % sec
    if sec == "none" and creds:
        return False, "SMTP_SECURITY=none with credentials: refusing to send them in plaintext"
    sender = env.get("SMTP_FROM") or env.get("SMTP_USER")
    rcpts = [x.strip() for x in env["ALERT_EMAIL_TO"].split(",") if x.strip()]
    m = EmailMessage()
    m["Subject"] = subject_of(msg, cfg)
    m["From"] = formataddr((env.get("ALERT_EMAIL_FROM_NAME") or "agent-switchboard", sender))
    m["To"] = ", ".join(rcpts)
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="agent-switchboard.local")
    if msg["level"] == "urgent":
        m["X-Priority"] = "1"
    m.set_content(text_of(msg) + "\n\n-- \nkey: %s\nsent by agent-switchboard\n" % msg.get("key", ""))
    ctx = ssl.create_default_context()
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=ADAPTER_TIMEOUT, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=ADAPTER_TIMEOUT)
    try:
        s.ehlo()
        encrypted = sec == "ssl"
        if sec in ("starttls", "auto"):
            if not s.has_extn("starttls"):
                # Plaintext only with SMTP_SECURITY=none (and no credentials).
                return False, "server offers no STARTTLS; set SMTP_SECURITY=none (without credentials) to allow plaintext"
            s.starttls(context=ctx)
            s.ehlo()
            encrypted = True
        if creds:
            if not encrypted:
                return False, "refusing to log in over an unencrypted connection"
            try:
                s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            except smtplib.SMTPServerDisconnected:
                # Some servers drop the connection after a rejected AUTH instead of answering 535.
                raise smtplib.SMTPAuthenticationError(535, "login rejected (server closed the connection); "
                                                      "check SMTP_USER / SMTP_PASSWORD") from None
        s.send_message(m, from_addr=sender, to_addrs=rcpts)
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
    return True, None


def a_line(msg, env, cfg):
    text = text_of(msg)[:LINE_TEXT_MAX]
    errs = []
    for to in [x.strip() for x in env["LINE_TO"].split(",") if x.strip()]:
        ok, err = http_post(LINE_PUSH_URL, {"to": to, "messages": [{"type": "text", "text": text}]},
                            {"Authorization": "Bearer " + env["LINE_CHANNEL_ACCESS_TOKEN"],
                             "X-Line-Retry-Key": str(uuid.uuid4())})
        if not ok:
            errs.append(err)
    return (not errs), ("; ".join(errs) if errs else None)


def a_sns(msg, env, cfg):
    subj = "".join(c if 32 <= ord(c) < 127 else "?" for c in subject_of(msg, cfg))[:100]
    penv = dict(os.environ)
    penv.update({k: v for k, v in env.items() if k.startswith("AWS_")})
    try:
        r = subprocess.run([shutil.which("aws"), "sns", "publish", "--topic-arn", env["AWS_SNS_TOPIC_ARN"],
                            "--subject", subj, "--message", text_of(msg), "--cli-connect-timeout", "3",
                            "--cli-read-timeout", str(ADAPTER_TIMEOUT), "--output", "json"],
                           capture_output=True, text=True, timeout=ADAPTER_TIMEOUT + 3, env=penv)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return (r.returncode == 0), (None if r.returncode == 0 else "aws exit %d" % r.returncode)


def a_webhook(msg, env, cfg):
    payload = {"source": "agent-switchboard", "level": msg["level"], "key": msg.get("key"),
               "title": msg["title"], "body": msg.get("body") or "", "url": msg.get("url"), "ts": msg.get("ts")}
    if msg.get("ref"):
        payload["ref"] = msg["ref"]
    headers = {"Authorization": env["ALERT_WEBHOOK_AUTH"]} if env.get("ALERT_WEBHOOK_AUTH") else None
    return http_post(env["ALERT_WEBHOOK_URL"], payload, headers)


ADAPTERS = {"slack": a_slack, "desktop": a_desktop, "email": a_email, "line": a_line, "sns": a_sns,
            "webhook": a_webhook}


def enabled(channel, env):
    if channel == "slack":
        return bool(env.get("SLACK_WEBHOOK_URL"))
    if channel == "desktop":
        # The process's own ALERT_DESKTOP=0 always wins, even over an explicit env dict, so a
        # caller (tests, probes) that turned the desktop off can never pop a real toast.
        if os.environ.get("ALERT_DESKTOP") == "0":
            return False
        return env.get("ALERT_DESKTOP", "1") != "0" and any(
            shutil.which(x) for x in ("powershell.exe", "notify-send", "osascript"))
    if channel == "email":
        return bool(env.get("SMTP_HOST") and env.get("ALERT_EMAIL_TO") and (env.get("SMTP_FROM") or env.get("SMTP_USER")))
    if channel == "line":
        return bool(env.get("LINE_CHANNEL_ACCESS_TOKEN") and env.get("LINE_TO"))
    if channel == "sns":
        return bool(env.get("AWS_SNS_TOPIC_ARN") and shutil.which("aws"))
    if channel == "webhook":
        return bool(env.get("ALERT_WEBHOOK_URL"))
    return False


def run_channel(channel, msg, env, cfg):
    try:
        ok, err = ADAPTERS[channel](msg, env, cfg)
    except Exception as exc:  # noqa: BLE001 - an adapter never breaks a send
        ok, err = False, "%s: %s" % (type(exc).__name__, exc)
    return bool(ok), (scrub(err, env) if err else None)


def dispatch(msg, channels, env, cfg):
    """Run the adapters in parallel; returns {channel: {ok, error}} and logs one event each."""
    results = {}
    if not channels:
        return results
    pool = ThreadPoolExecutor(max_workers=len(channels))
    futs = {pool.submit(run_channel, c, msg, env, cfg): c for c in channels}
    done, _ = wait(futs, timeout=ADAPTER_TIMEOUT + 4)
    for f, c in futs.items():
        ok, err = f.result() if f in done else (False, "timeout")
        results[c] = {"ok": ok, "error": err}
        log_event({"ts": msg.get("ts") or now_ms(), "type": "alert", "key": msg.get("key"), "channel": c,
                   "level": msg["level"], "ok": ok, "error": err})
    pool.shutdown(wait=False)
    return results


# ------------------------------------------------------------------ state

class Store:
    """alerts.json under an exclusive flock (collector tick and CLI sends may race)."""

    def __enter__(self):
        h = home()
        self.path = os.path.join(h, "alerts.json")
        self.lock = open(os.path.join(h, "alerts.lock"), "w")  # noqa: SIM115
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        data = read_json(self.path)
        self.data = data if isinstance(data, dict) else {}
        self.orig = json.dumps(self.data, sort_keys=True)
        return self

    def __exit__(self, *exc):
        try:
            if json.dumps(self.data, sort_keys=True) != self.orig:
                write_json_atomic(self.path, self.data)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
        return False


def _open_episode(prev, alert, now):
    """A fresh entry for `alert`, keeping the previous per-level send times (dedupe)."""
    return {
        "level": alert["level"], "first_at": now, "last_at": now, "acked_at": None, "resolved": False,
        "sent": dict((prev or {}).get("sent") or {}),
        "title": guard.scrub(alert["title"]), "body": guard.scrub(alert.get("body") or ""), "url": alert.get("url"),
        "kind": alert.get("kind") or "generic", "escalate": bool(alert.get("escalate")),
        "repeat": alert.get("repeat", True), "resets_at": alert.get("resets_at"),
        "board": alert.get("board"),
    }


def board_id(key, e=None):
    """The board card an alert is about: the entry's explicit `board` id, else the id in a
    decision:/consensus:/message: key. None = the board root."""
    if e and e.get("board"):
        return e["board"]
    for prefix in ("decision:", "consensus:", "message:"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


BOARD_LABELS = {"d": "Decide on the board", "c": "View on the board", "m": "View on the board"}
REF_LABELS = (("session:", "Open the session"), ("decision:", "Open the artifact"))


def with_board_link(msg, cfg, item_id=None):
    """Point msg at the board when a board URL is configured: the board link becomes `url`, the
    original url (session or artifact link) becomes `ref`, and both get a label so they are easy
    to tell apart on a phone. Unset: msg is returned untouched."""
    link = fleet.board_link(cfg.get("board_url"), item_id)
    if link:
        ref = msg.get("url")
        msg["url"] = link
        param = link.rsplit("/?", 1)[1][:1] if "/?" in link else ""
        msg["url_label"] = BOARD_LABELS.get(param, "Open the board")
        if ref and ref != link:
            msg["ref"] = ref
            key = str(msg.get("key") or "")
            msg["ref_label"] = next((lbl for pre, lbl in REF_LABELS if key.startswith(pre)), "Link")
    return msg


def _deliver(key, e, now, env, cfg, force=False):
    """Deliver entry `e` at its current level, honouring dedupe, quiet hours and retry backoff.
    Returns a result dict; mutates e."""
    lvl = e["level"]
    res = {"key": key, "level": lvl, "status": None, "channels": {}}
    last = (e.get("sent") or {}).get(lvl)
    repeat_ms = float(cfg.get("repeat_minutes") or 0) * 60000
    if not force and last is not None and (repeat_ms <= 0 or now - last < repeat_ms):
        res["status"] = "deduped"
        return res
    if not force and lvl != "urgent" and in_quiet_hours(cfg, now):
        res["status"] = "quiet"
        if e.get("held") == lvl:
            return res
        e["held"] = lvl
        log_event({"ts": now, "type": "alert", "key": key, "channel": None, "level": lvl, "ok": False,
                   "error": "quiet hours"})
        return res
    routes = (cfg.get("routes") or {}).get(lvl) or []
    channels = [c for c in routes if c in ADAPTERS and enabled(c, env)]
    channels, held = prefs.filter_channels(channels, lvl, cfg, now, approvers=cfg.get("approvers"))
    for c, why in held.items():
        log_event({"ts": now, "type": "alert-suppressed", "key": key, "channel": c, "level": lvl, "reason": why})
    if held:
        res["suppressed"] = held
    if held and not channels:
        # Held by the owner's own controls: this level counts as delivered (no replay later); an
        # alert that stays open goes out at its next escalation step or repeat.
        e.setdefault("sent", {})[lvl] = now
        e.setdefault("suppressed", {})[lvl] = now
        e.pop("held", None)
        e.pop("retry_at", None)
        res["status"] = "suppressed"
        return res
    # The secret guard runs on every delivery too (alerts.json may predate it, or be edited).
    msg = with_board_link({"level": lvl, "key": key, "title": guard.scrub(e.get("title") or key),
                           "body": guard.scrub(e.get("body") or ""),
                           "url": guard.scrub(e.get("url"), structural=True), "ts": now}, cfg, board_id(key, e))
    res["channels"] = dispatch(msg, channels, env, cfg)
    if not channels or any(r["ok"] for r in res["channels"].values()):
        e.setdefault("sent", {})[lvl] = now
        e.pop("held", None)
        e.pop("retry_at", None)
        res["status"] = "sent" if channels else "no-channels"
    else:
        e["retry_at"] = now + int(float(cfg.get("retry_minutes") or 5) * 60000)
        res["status"] = "failed"
    return res


# ------------------------------------------------------------------ public API

def send(level, key, title, body="", url=None, kind=None, escalate=None, repeat=None, resets_at=None,
         board=None, now=None, env=None, cfg=None):
    """Raise (or re-raise) alert KEY at LEVEL. Never raises; returns a result dict."""
    try:
        if level not in LEVELS:
            raise ValueError("unknown level: %s" % level)
        now = now if now is not None else now_ms()
        env = env if env is not None else load_env()
        cfg = cfg if cfg is not None else load_config()
        if escalate is None:
            escalate = key.startswith(("decision:", "session:"))
        if repeat is None:
            repeat = bool(escalate)  # only alerts that can be acked/resolved re-alert
        alert = {"level": level, "title": title, "body": body, "url": url, "kind": kind, "escalate": escalate,
                 "repeat": repeat, "resets_at": resets_at, "board": board}
        with Store() as st:
            e = st.data.get(key)
            if e is None or e.get("acked_at") or e.get("resolved"):
                e = _open_episode(e, alert, now)
            else:
                if rank(level) > rank(e.get("level")):
                    e["level"] = level
                e.update({"title": guard.scrub(title), "body": guard.scrub(body) or e.get("body") or "",
                          "url": url or e.get("url"),
                          "last_at": now})
                if kind:
                    e["kind"] = kind
                if board:
                    e["board"] = board
            st.data[key] = e
            return _deliver(key, e, now, env, cfg)
    except Exception as exc:  # noqa: BLE001
        return {"key": key, "level": level, "status": "error", "error": "%s: %s" % (type(exc).__name__, exc),
                "channels": {}}


def ack(pattern, now=None, by="cli"):
    """Acknowledge every unacked key matching PATTERN (exact key or glob). Returns the keys."""
    now = now if now is not None else now_ms()
    out = []
    with Store() as st:
        for key, e in st.data.items():
            if (key == pattern or fnmatch.fnmatchcase(key, pattern)) and not e.get("acked_at"):
                e["acked_at"] = now
                e["acked_by"] = by
                out.append(key)
    for key in out:
        log_event({"ts": now, "type": "alert-ack", "key": key, "by": by})
    return out


def resolve(key, now=None):
    """The condition behind KEY is gone (decision left pending / session left needs-you)."""
    now = now if now is not None else now_ms()
    with Store() as st:
        e = st.data.get(key)
        if e and not e.get("resolved"):
            e["resolved"] = True
            e["acked_at"] = e.get("acked_at") or now
            e.setdefault("acked_by", "auto")
            return True
    return False


# Alert builders: one per collector event. Each returns the kwargs for send().

def permission_decision_id(session, decisions):
    """Id of the pending permission (or pane question) decision behind a session's prompt (same
    tool_use_id), if any."""
    tid = (session.get("pending") or {}).get("tool_use_id")
    if not tid:
        return None
    for d in decisions or []:
        if (isinstance(d, dict) and (d.get("kind") == "permission" or d.get("source") == "pane")
                and d.get("status") == "pending"
                and d.get("tool_use_id") == tid
                and (d.get("from") == session.get("code") or d.get("session_id") == session.get("session_id"))):
            return d.get("id")
    return None


def needs_you_alert(session, decisions=None):
    code = session.get("code") or "?"
    p = session.get("pending") or {}
    if p and p.get("tool_name") == "AskUserQuestion":
        body = "asks: %s" % (p.get("summary") or "")[:200]
    elif p:
        body = "%s: %s" % (p.get("tool_name") or "tool", (p.get("summary") or "")[:200])
    else:
        body = "waiting for input"
    return {"level": "warn", "key": "session:%s:needs-you" % code, "title": "%s needs you" % code, "body": body,
            "url": session.get("remote_url"), "kind": "permission" if p else "needs-you", "escalate": True,
            "board": permission_decision_id(session, decisions)}


def decision_alert(d):
    who = d.get("from") or "?"
    if fleet.is_production(d):
        return {"level": "urgent", "key": "decision:%s" % d.get("id"), "kind": "gate", "escalate": True,
                "title": "%s requests PRODUCTION approval" % who,
                "body": (d.get("question") or "")[:300], "url": d.get("artifact_ref") or d.get("url")}
    if d.get("kind") == "permission":
        title = "%s asks permission" % who
        body = "%s: %s" % (d.get("tool_name") or "tool", (d.get("summary") or "")[:200])
        kind = "permission"
    else:
        title = "%s asks: %s" % (who, (d.get("question") or "")[:120])
        opts = d.get("options") or []
        body = ("options: %s" % " / ".join(str(o) for o in opts)) if opts else (d.get("context") or "")[:300]
        kind = "decision"
    return {"level": "warn", "key": "decision:%s" % d.get("id"), "title": title, "body": body,
            "url": d.get("artifact_ref"), "kind": kind, "escalate": True}


def limit_alert(lim, pending_n):
    resets = to_ms(lim.get("resets_at"))
    weekly = lim.get("kind") == "weekly"
    return {"level": "urgent", "key": "limit:%s" % lim.get("hit_at"), "kind": "limit", "escalate": False,
            "repeat": False,
            "title": "usage limit hit (%s), resets ~%s" % (lim.get("kind") or "unknown", fmt_time(resets, weekly)),
            "body": "board frozen; decisions pending: %d — switch account / start failover" % pending_n}


def usage_alert(label, kid, pct, resets_ms, threshold, level):
    weekly = kid == "7d"
    advice = {"urgent": "switch account now", "high": "prepare to switch account"}.get(level, "heads up")
    when = ("resets %s" if weekly else "resets ~%s") % fmt_time(resets_ms, weekly)
    return {"level": level, "key": "usage:%s:%d" % (kid, int(threshold)), "kind": "usage", "escalate": False,
            "repeat": False, "resets_at": resets_ms,
            "title": "%s usage %d%% — %s — %s" % (label, int(pct), when, advice), "body": ""}


def on_needs_you(session, decisions=None, **kw):
    return send(**needs_you_alert(session, decisions), **kw)


def on_decision(decision, **kw):
    return send(**decision_alert(decision), **kw)


def on_limit_hit(lim, pending_n=0, **kw):
    return send(**limit_alert(lim, pending_n), **kw)


def _same_window(e, resets_ms, now, span):
    old = e.get("resets_at")
    if old and resets_ms:
        return abs(int(old) - int(resets_ms)) < 30 * 60 * 1000
    return now - int(e.get("first_at") or 0) < span


def _sync_snapshot(data, snap, now):
    """Auto-ack (resolve) gone conditions and open alerts for new ones. Returns new keys."""
    sessions = {s.get("code"): s for s in snap.get("sessions") or [] if isinstance(s, dict)}
    pending = {d.get("id"): d for d in snap.get("decisions") or []
               if isinstance(d, dict) and d.get("status") == "pending"}
    lim = snap.get("limit") if isinstance(snap.get("limit"), dict) else None
    for key, e in data.items():
        if e.get("resolved"):
            continue
        gone = False
        if key.startswith("session:") and key.endswith(":needs-you"):
            s = sessions.get(key[len("session:"):-len(":needs-you")])
            gone = not s or s.get("state") != "needs-you"
        elif key.startswith("decision:"):
            gone = key[len("decision:"):] not in pending
        elif key.startswith("limit:"):
            gone = not lim or "limit:%s" % lim.get("hit_at") != key
        if gone:
            e["resolved"] = True
            if not e.get("acked_at"):
                e["acked_at"] = now
                e["acked_by"] = "auto"
    alerts = [needs_you_alert(s, pending.values()) for s in sessions.values() if s.get("state") == "needs-you"]
    for d in pending.values():
        if (d.get("kind") == "permission" or d.get("source") == "pane") and not fleet.is_production(d):
            continue  # covered by the session's needs-you alert
        alerts.append(decision_alert(d))
    if lim:
        n = sum(1 for d in pending.values())
        alerts.append(limit_alert(lim, n))
    new = []
    for a in alerts:
        e = data.get(a["key"])
        if e is None or e.get("resolved"):
            data[a["key"]] = _open_episode(e, a, now)
            new.append(a["key"])
        elif e.get("board") != a.get("board"):
            e["board"] = a.get("board")  # a session's next permission prompt: link to its card
    return new


def _sync_usage(data, usage, cfg, now):
    new = []
    if not isinstance(usage, dict):
        return new
    levels = threshold_levels(cfg)
    for field, kid, label, span in WINDOWS:
        w = usage.get(field)
        if not isinstance(w, dict) or w.get("pct") is None:
            continue
        try:
            pct = float(w["pct"])
        except (TypeError, ValueError):
            continue
        resets = to_ms(w.get("resets_at"))
        if resets and resets <= now:
            continue  # the window already reset; this reading is stale
        crossed = [(t, lvl) for t, lvl in levels if pct >= t]
        if not crossed:
            continue
        t, lvl = crossed[-1]
        # Any alert for this window at this threshold or higher already raised? Then nothing new.
        done = False
        for tt, _ in levels:
            if tt < t:
                continue
            e = data.get("usage:%s:%d" % (kid, int(tt)))
            if e and _same_window(e, resets, now, span):
                done = True
        if done:
            continue
        a = usage_alert(label, kid, pct, resets, t, lvl)
        data[a["key"]] = _open_episode(None, a, now)
        new.append(a["key"])
    # Windows that have reset close their usage alerts.
    for key, e in data.items():
        if key.startswith("usage:") and not e.get("resolved") and e.get("resets_at") and int(e["resets_at"]) <= now:
            e["resolved"] = True
    return new


# ------------------------------------------------------------------ resources and operator (J6)
# resources.json (lib/resources.py) and operator.json -> res:* and operator:stale alerts, opened
# and auto-resolved here on every tick (the way limit: is). docs/contract.md, "Resources".

def _res_lib():
    import resources  # noqa: WPS433 (lazy: only the tick needs it)
    return resources


def _fmt_mb(v):
    if v is None:
        return "?"
    return "%.1f GB" % (v / 1024.0) if v >= 1024 else "%d MB" % v


def _reclaim_hint(idle):
    """One sentence naming idle teammates by session, or "" when there are none."""
    if not idle:
        return ""
    parts = []
    for code, tms in idle:
        parts.append("%s %d idle (%s)" % (code or "orphaned", len(tms), _fmt_mb(sum(t.get("rss_mb") or 0 for t in tms))))
    return "Reclaim idle teammates first: %s." % ", ".join(parts)


def _largest(res, n=3):
    rows = []
    for s in res.get("sessions") or []:
        for c in s.get("children") or []:
            if c.get("heavy"):
                rows.append((c.get("rss_mb") or 0, "%s %s %s" % (s.get("code"), c.get("comm"), _fmt_mb(c.get("rss_mb")))))
    rows.sort(key=lambda r: -r[0])
    return ("Largest: %s." % ", ".join(r[1] for r in rows[:n])) if rows else ""


def resource_alerts(res, rcfg=None):
    """Alert dicts for a resources.json document (empty when disabled or missing)."""
    out = []
    if not isinstance(res, dict) or res.get("enabled") is False:
        return out
    R = _res_lib()
    levels = res.get("levels") if isinstance(res.get("levels"), dict) else {}
    idle = R.idle_teammates(res)
    # memory pressure: mem (PSI included) or an OOM kill; swap alone never promotes anything
    mem_rank = max(R.LEVELS.index(levels.get(k)) if levels.get(k) in R.LEVELS else 0 for k in ("mem", "oom"))
    hint = _reclaim_hint(idle)
    m, sw = res.get("mem") or {}, res.get("swap") or {}
    for metric in sorted(levels):
        lvl = levels[metric]
        base = metric.split(":", 1)[0]
        if base not in ("mem", "swap", "disk") or lvl not in ("warn", "high", "critical"):
            continue
        if base == "mem":
            title = "Memory %s: %s%% available (%s of %s)" % (lvl, m.get("available_pct"), _fmt_mb(m.get("available_mb")),
                                                             _fmt_mb(m.get("total_mb")))
            if m.get("psi_some_avg60"):
                title += ", pressure %.0f" % m["psi_some_avg60"]
        elif base == "swap":
            title = "Swap %s: %s%% used of %s" % (lvl, sw.get("used_pct"), _fmt_mb(sw.get("total_mb")))
        else:
            d = next((x for x in res.get("disks") or [] if "disk:%s" % x.get("id") == metric), {})
            used = ", ".join(str(x) for x in d.get("labels") or []) or "?"
            title = "Disk %s (%s) %s: %s%% free (%s)" % (metric[5:], used, lvl, d.get("free_pct"), _fmt_mb(d.get("free_mb")))
        body = " ".join(x for x in ((hint if base != "disk" else ""), _largest(res) if base != "disk" else "") if x)
        out.append({"level": "warn" if lvl == "warn" else "high", "key": "res:%s" % metric, "kind": "resources",
                    "escalate": False, "repeat": True, "title": title, "body": body})
    if res.get("oom_recent") and isinstance(res.get("oom_kills"), int):
        out.append({"level": "high", "key": "res:oom:%d" % res["oom_kills"], "kind": "resources", "escalate": False,
                    "repeat": False, "title": "OOM kill: the kernel killed a process (%d so far)" % res["oom_kills"],
                    "body": " ".join(x for x in ("A test run or browser may have died; check the sessions.", hint,
                                                 _largest(res)) if x)})
    mins = int(float(res.get("teammate_idle_minutes") or (rcfg or {}).get("teammate_idle_minutes") or 30))
    for code, tms in idle:
        rss = _fmt_mb(sum(t.get("rss_mb") or 0 for t in tms))
        names = ", ".join("%s (%s)" % (t.get("name"), _fmt_mb(t.get("rss_mb"))) for t in tms[:6])
        if code:
            key, title = "res:idle-teammates:%s" % code, "%s has %d teammate(s) idle > %d min (%s)" % (code, len(tms), mins, rss)
            body = "%s. The board never stops processes: ask %s to stop the teammates it no longer needs." % (names, code)
        else:
            key, title = "res:orphan-teammates", "%d orphaned teammate(s) idle > %d min (%s)" % (len(tms), mins, rss)
            body = "%s. Their parent session is gone; close them from their tmux pane if they are done." % names
        out.append({"level": "warn" if mem_rank >= 1 else "info", "key": key, "kind": "resources", "escalate": False,
                    "repeat": False, "title": title, "body": body})
    return out


def operator_alert(health):
    return {"level": "high", "key": "operator:stale", "kind": "operator", "escalate": True, "repeat": True,
            "title": "Board operator is not running — %d item(s) waiting" % health.get("pending", 0),
            "body": "%s. Restart the operator session (prompts/board-operator.md); answers queue until it is back."
                    % "; ".join(health.get("reasons") or ["no heartbeat"])}


def _pending_count(snap, h):
    """What `board-serve --watch` hands the operator (serve.actionable): answered questions and
    gates (not permission prompts: board-serve presses those itself) and new messages. Decisions
    still pending wait on the owner, not on the operator."""
    n = sum(1 for d in (snap or {}).get("decisions") or []
            if isinstance(d, dict) and d.get("status") == "answered" and d.get("kind") != "permission"
            and not (d.get("source") == "pane" and d.get("relay") != "operator"))
    mdir = os.path.join(h, "messages")
    try:
        names = [f for f in os.listdir(mdir) if f.endswith(".json")]
    except OSError:
        names = []
    for f in names:
        m = read_json(os.path.join(mdir, f))
        if isinstance(m, dict) and m.get("status") == "new":
            n += 1
    return n


def _sync_resources(data, snap, now, fcfg=None):
    """Open, raise and resolve res:* and operator:stale. Returns new keys. A failure here never
    stops the rest of the tick."""
    try:
        return _sync_resources_now(data, snap, now, fcfg)
    except Exception as exc:  # noqa: BLE001
        log_event({"ts": now, "type": "alert", "key": "res:*", "channel": None, "level": None, "ok": False,
                   "error": "resources sync: %s: %s" % (type(exc).__name__, exc)})
        return []


def _sync_resources_now(data, snap, now, fcfg=None):
    R = _res_lib()
    h = home()
    fcfg = fcfg if fcfg is not None else fleet.load_fleet()
    rcfg = R.settings(fcfg)
    res = read_json(os.path.join(h, "resources.json"))
    fresh = isinstance(res, dict) and now - int(res.get("at") or 0) <= max(120, 4 * rcfg["interval"]) * 1000
    alerts = resource_alerts(res, rcfg) if fresh else []
    health = R.operator_health(read_json(os.path.join(h, "operator.json")), res, rcfg, _pending_count(snap, h), now)
    if health["stale"]:
        alerts.append(operator_alert(health))
    want = {a["key"]: a for a in alerts}
    for key, e in data.items():
        if e.get("resolved") or key in want:
            continue
        if key == "operator:stale" or (key.startswith("res:") and fresh):
            e["resolved"] = True
            if not e.get("acked_at"):
                e["acked_at"] = now
                e["acked_by"] = "auto"
    new = []
    for a in alerts:
        e = data.get(a["key"])
        if e is None or e.get("resolved") or (e.get("acked_at") and rank(a["level"]) > rank(e.get("level"))):
            # A repeating alert keeps its send times across episodes (no flapping re-sends).
            data[a["key"]] = _open_episode(e if a.get("repeat") and e else None, a, now)
            new.append(a["key"])
        elif rank(a["level"]) > rank(e.get("level")):
            e.update({"level": a["level"], "title": guard.scrub(a["title"]), "body": guard.scrub(a["body"]),
                      "last_at": now})
    return new


# ------------------------------------------------------------------ scheduler jams (J1-J8)
# scheduler.json `jams` (lib/scheduler.py + lib/advisories.py) -> jam:<rule>:<scope>, opened and
# auto-resolved here. J5/J6 carry no alert (res:* and operator:stale cover them); advisories never alert.

def jam_alerts(doc):
    out = []
    if not isinstance(doc, dict) or not doc.get("enabled"):
        return out
    for j in doc.get("jams") or []:
        if not isinstance(j, dict) or j.get("alert") not in ("info", "warn", "high"):
            continue
        m = re.fullmatch(r"j-(J[0-9][a-z]?)-([A-Za-z0-9][A-Za-z0-9._-]{0,39})", str(j.get("id")))
        if not m:
            continue                       # keys, titles and subjects only ever carry plain tokens
        who = ", ".join(x for x in j.get("sessions") or [] if isinstance(x, str)
                        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", x))
        text = " ".join(re.sub("[\x00-\x1f\x7f-\x9f]", " ", str(j.get("text") or "")).split())[:200]
        out.append({"level": j["alert"], "key": "jam:%s:%s" % m.groups(), "kind": "jam", "escalate": False,
                    "repeat": False, "title": "Jam %s: %s" % (m.group(1), text),
                    "body": ("Sessions: %s." % who) if who else ""})
    return out


def _sync_jams(data, now):
    try:
        want = {a["key"]: a for a in jam_alerts(read_json(os.path.join(home(), "scheduler.json")))}
        for key, e in data.items():
            if key.startswith("jam:") and key not in want and not e.get("resolved"):
                e["resolved"] = True
                if not e.get("acked_at"):
                    e["acked_at"], e["acked_by"] = now, "auto"
        for key, a in want.items():
            e = data.get(key)
            if e is None or e.get("resolved"):
                data[key] = _open_episode(None, a, now)
    except Exception as exc:  # noqa: BLE001 - never stops the tick
        log_event({"ts": now, "type": "alert", "key": "jam:*", "channel": None, "level": None, "ok": False,
                   "error": "jam sync: %s: %s" % (type(exc).__name__, exc)})


def baseline(now=None, cfg=None):
    """Treat every condition open right now as already known: open alerts for them (as tick()
    would) and ack them with acked_by="baseline", without delivering anything. Only conditions
    that appear later alert. Creates alerts.json even when nothing is open, so the collector
    knows the baseline was taken. Returns the keys acked. Never raises."""
    try:
        now = now if now is not None else now_ms()
        cfg = cfg if cfg is not None else load_config()
        h = home()
        snap = read_json(os.path.join(h, "snapshot.json"))
        usage = read_json(os.path.join(h, "usage.json"))
        seeded = []
        with Store() as st:
            if isinstance(snap, dict):
                _sync_snapshot(st.data, snap, now)
            _sync_usage(st.data, usage, cfg, now)
            _sync_resources(st.data, snap, now)
            _sync_jams(st.data, now)
            for key, e in st.data.items():
                if not e.get("acked_at") and not e.get("resolved"):
                    e["acked_at"] = now
                    e["acked_by"] = "baseline"
                    seeded.append(key)
        path = os.path.join(h, "alerts.json")
        if not os.path.exists(path):
            write_json_atomic(path, {})
        log_event({"ts": now, "type": "alert-baseline", "keys": seeded})
        return seeded
    except Exception:  # noqa: BLE001
        return []


def tick(now=None, env=None, cfg=None):
    """Escalation pass. Reads snapshot.json + usage.json + alerts.json, auto-acks, discovers new
    conditions, escalates unacked keys and delivers what is due. Never raises."""
    try:
        now = now if now is not None else now_ms()
        env = env if env is not None else load_env()
        cfg = cfg if cfg is not None else load_config()
        h = home()
        snap = read_json(os.path.join(h, "snapshot.json"))
        usage = read_json(os.path.join(h, "usage.json"))
        esc = cfg.get("escalate_minutes") or {}
        high_ms = float(esc.get("high", 10)) * 60000
        urgent_ms = float(esc.get("urgent", 30)) * 60000
        perm_ms = float(esc.get("permission_urgent", 10)) * 60000
        repeat_ms = float(cfg.get("repeat_minutes") or 0) * 60000
        results = []
        with Store() as st:
            if isinstance(snap, dict):
                _sync_snapshot(st.data, snap, now)
            _sync_usage(st.data, usage, cfg, now)
            _sync_resources(st.data, snap, now)
            _sync_jams(st.data, now)
            for key, e in st.data.items():
                if e.get("acked_at") or e.get("resolved"):
                    continue
                if e.get("escalate"):
                    age = now - int(e.get("first_at") or now)
                    target = e.get("level") or "warn"
                    if e.get("kind") in ("permission", "gate") and age >= perm_ms:
                        target = "urgent"
                    elif age >= urgent_ms:
                        target = "urgent"
                    elif age >= high_ms:
                        target = "high" if rank(target) < rank("high") else target
                    if rank(target) > rank(e.get("level")):
                        e["level"] = target
                        e["escalated_at"] = now
                last = (e.get("sent") or {}).get(e["level"])
                if last is None:
                    due = now >= int(e.get("retry_at") or 0)
                else:
                    due = bool(e.get("repeat", True)) and repeat_ms > 0 and now - last >= repeat_ms
                if due:
                    results.append(_deliver(key, e, now, env, cfg))
            for key in [k for k, e in st.data.items() if (e.get("resolved") or e.get("acked_at"))
                        and now - int(e.get("last_at") or e.get("first_at") or 0) > KEEP_MS]:
                st.data.pop(key, None)
        return results
    except Exception as exc:  # noqa: BLE001
        return [{"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}]


def test_channel(channel, level="info", env=None, cfg=None):
    env = env if env is not None else load_env()
    cfg = cfg if cfg is not None else load_config()
    if channel not in ADAPTERS:
        return {"channel": channel, "ok": False, "error": "unknown channel"}
    if not enabled(channel, env):
        return {"channel": channel, "ok": False, "error": "not configured"}
    now = now_ms()
    msg = with_board_link({"level": level, "key": "test:%s" % channel, "title": "test alert", "ts": now,
                           "body": "agent-switchboard test — if you can read this, the %s channel works." % channel}, cfg)
    r = dispatch(msg, [channel], env, cfg)[channel]
    return {"channel": channel, "ok": r["ok"], "error": r["error"]}


def gate(channel, level, key="notify", now=None, cfg=None):
    """May CHANNEL deliver a LEVEL message now (for senders outside send(), such as `board
    notify`)? Returns None, or the reason it is held (logged as alert-suppressed)."""
    now = now if now is not None else now_ms()
    cfg = cfg if cfg is not None else load_config()
    _, held = prefs.filter_channels([channel], level, cfg, now, approvers=cfg.get("approvers"))
    why = held.get(channel)
    if why:
        log_event({"ts": now, "type": "alert-suppressed", "key": key, "channel": channel, "level": level,
                   "reason": why})
    return why


def status(env=None, cfg=None):
    env = env if env is not None else load_env()
    cfg = cfg if cfg is not None else load_config()
    data = read_json(os.path.join(home(), "alerts.json")) or {}
    return {"channels": {c: enabled(c, env) for c in CHANNELS},
            "controls": prefs.status(cfg, now_ms(), home(), cfg.get("approvers")),
            "open": {k: {x: e.get(x) for x in ("level", "kind", "first_at", "sent", "title")}
                     for k, e in data.items() if not e.get("acked_at") and not e.get("resolved")},
            "total": len(data)}


# ------------------------------------------------------------------ CLI

def _print_result(r, as_json):
    if as_json:
        print(json.dumps(r, ensure_ascii=False))
        return
    chans = " ".join("%s=%s" % (c, "ok" if v["ok"] else "FAIL(%s)" % v["error"]) for c, v in r.get("channels", {}).items())
    chans = " ".join(x for x in [chans] + ["%s=held(%s)" % (c, why) for c, why in (r.get("suppressed") or {}).items()] if x)
    line = "%s %s %s %s" % (fleet.safe_text(r.get("key")), r.get("level"), r.get("status"), fleet.safe_text(chans or r.get("error") or ""))
    print(line.rstrip())


def main(argv):
    ap = argparse.ArgumentParser(prog="board-notify", description="agent-switchboard alerting")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("send")
    p.add_argument("--level", required=True, choices=LEVELS)
    p.add_argument("--key", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--url")
    p.add_argument("--board", help="decision/message/consensus id to open on the board (default: from KEY)")
    p.add_argument("--kind")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--escalate", dest="escalate", action="store_true", default=None)
    g.add_argument("--no-escalate", dest="escalate", action="store_false")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ack")
    p.add_argument("keys", nargs="+")
    p.add_argument("--by", default="cli", help="who acknowledged (recorded as acked_by)")
    p = sub.add_parser("tick")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("test")
    p.add_argument("--channel", required=True, choices=CHANNELS)
    p.add_argument("--level", default="info", choices=LEVELS)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("gate")
    p.add_argument("--channel", required=True, choices=CHANNELS)
    p.add_argument("--level", required=True, choices=LEVELS)
    p.add_argument("--key", default="notify")
    args = ap.parse_args(argv)

    if args.cmd == "send":
        r = send(args.level, args.key, args.title, args.body, args.url, kind=args.kind, escalate=args.escalate,
                 board=args.board)
        _print_result(r, args.json)
        return 0
    if args.cmd == "ack":
        n = 0
        for k in args.keys:
            for key in ack(k, by=args.by):
                print("acked %s" % key)
                n += 1
        return 0 if n else 1
    if args.cmd == "tick":
        for r in tick():
            _print_result(r, args.json)
        return 0
    if args.cmd == "test":
        r = test_channel(args.channel, args.level)
        print("%s: %s" % (r["channel"], "ok" if r["ok"] else "FAIL (%s)" % r["error"]))
        return 0 if r["ok"] else (3 if r["error"] == "not configured" else 1)
    if args.cmd == "status":
        s = status()
        if args.json:
            print(json.dumps(s, ensure_ascii=False))
        else:
            print("channels: " + " ".join("%s=%s" % (c, "on" if v else "off") for c, v in s["channels"].items()))
            for k, e in s["open"].items():
                print("open: %s %s %s" % (fleet.safe_text(k), e["level"], fleet.safe_text(e.get("title") or "")))
            print("entries: %d" % s["total"])
            for line in prefs.status_lines(s["controls"], now_ms()):
                print("controls: " + line)
        return 0
    if args.cmd == "gate":
        return 10 if gate(args.channel, args.level, args.key) else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
