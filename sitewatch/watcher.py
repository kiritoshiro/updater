"""Alert watcher, Part A step 1: file-level changes and origin health, alerts to Discord.

Runs as root every five minutes and never executes site code: it only lists, lstats and hashes
files. See docs/WATCHER.md for the checks, levels and safety rules.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.request import Request, build_opener

from . import collector

COLLECTOR_CONFIG = Path("/etc/site-watch/collector.json")
WATCH_CONFIG = Path("/etc/site-watch/watch.json")
STATE_DIR = Path("/var/lib/site-watch/watch")

LEVELS = ("CRITICAL", "WARNING", "INFO")
MAX_HASH_BYTES = 16 * 1024 * 1024
UPLOADS_MAX_ENTRIES = 300_000
UPLOADS_MAX_SECONDS = 40
ROOT_FILES = ("wp-config.php", "index.php", ".htaccess", ".user.ini")
EXECUTABLE = re.compile(r".*\.(php[0-9s]?|pht|phtml|phar)\Z", re.IGNORECASE | re.DOTALL)
SILENCE = re.compile(rb"\s*<\?php\s*(//[^\n]*|/\*.*?\*/|#[^\n]*)?\s*(\?>)?\s*", re.DOTALL)
DISCORD_LIMIT = 1900
WEBHOOK = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{5,30}/[A-Za-z0-9_-]{20,200}\Z")
MENTION = re.compile(r"<@!?(\d{5,30})>|<@&(\d{5,30})>|@everyone|@here\Z")


# --- configuration ------------------------------------------------------------------------------

def validate_watch_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("watch.json must be an object")
    url = raw.get("webhook_url")
    if not isinstance(url, str) or not WEBHOOK.fullmatch(url):
        raise ValueError("webhook_url must be a Discord webhook URL")
    mention = raw.get("mention")
    if mention is not None and (not isinstance(mention, str) or not MENTION.fullmatch(mention)):
        raise ValueError("mention must be <@user-id>, <@&role-id>, @everyone or @here")
    hour = raw.get("digest_hour", 8)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("digest_hour must be 0-23")
    ignore = raw.get("ignore", [])
    if not isinstance(ignore, list):
        raise ValueError("ignore must be a list")
    for pattern in ignore:
        # Only noise inside uploads/ may be ignored; never the whole of it, never code folders.
        if (not isinstance(pattern, str) or not pattern.startswith("uploads/") or ".." in pattern
                or pattern.rstrip("/*") == "uploads" or not re.fullmatch(r"[A-Za-z0-9_.*?/ -]{1,200}", pattern)):
            raise ValueError("ignore patterns must be paths inside uploads/ relative to wp-content")
    return {"webhook_url": url, "mention": mention, "digest_hour": hour, "ignore": ignore}


def load_watch_config(path=WATCH_CONFIG):
    path = Path(path).resolve(strict=True)
    collector.check_root_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("watch.json must have mode 0600")
    return validate_watch_config(json.loads(path.read_text(encoding="utf-8")))


# --- file snapshots -----------------------------------------------------------------------------

def entry(path):
    """Metadata of one path without following symlinks."""
    info = os.lstat(path)
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else \
        "link" if stat.S_ISLNK(info.st_mode) else "other"
    return {"type": kind, "size": info.st_size if kind == "file" else None, "mtime": int(info.st_mtime)}


def read_regular(path, limit):
    """Read a regular file up to `limit` bytes; None for symlinks, special or oversized files."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return stream.read(limit + 1)


def file_hash(path):
    data = read_regular(path, MAX_HASH_BYTES)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def listing(directory, hashed=False):
    """Top-level entries of a directory; None when the directory is missing."""
    if not os.path.isdir(directory) or os.path.islink(directory):
        return None
    result = {}
    with os.scandir(directory) as entries:
        for item in entries:
            meta = entry(item.path)
            if hashed and meta["type"] == "file":
                meta["sha256"] = file_hash(item.path)
            result[item.name] = meta
    return result


def benign_index(path, meta):
    if meta["size"] is None or meta["size"] > 256 or os.path.basename(path).lower() != "index.php":
        return False
    data = read_regular(path, 256)
    return data is not None and SILENCE.fullmatch(data) is not None


def uploads_executables(uploads, ignore=(), clock=time.monotonic):
    """PHP-like files anywhere under uploads/ (symlinks not followed). Returns (files, truncated)."""
    found, seen, started = {}, 0, clock()
    if not os.path.isdir(uploads) or os.path.islink(uploads):
        return found, False
    for root, dirs, files in os.walk(uploads, followlinks=False):
        rel_root = os.path.relpath(root, os.path.dirname(uploads)).replace(os.sep, "/")
        dirs[:] = [d for d in dirs if not any(fnmatch.fnmatchcase(f"{rel_root}/{d}", p) or
                                              fnmatch.fnmatchcase(f"{rel_root}/{d}/", p) for p in ignore)]
        seen += len(dirs) + len(files)
        if seen > UPLOADS_MAX_ENTRIES or clock() - started > UPLOADS_MAX_SECONDS:
            return found, True
        for name in files:
            rel = f"{rel_root}/{name}"
            if not EXECUTABLE.fullmatch(name) or any(fnmatch.fnmatchcase(rel, p) for p in ignore):
                continue
            path = os.path.join(root, name)
            meta = entry(path)
            meta["sha256"] = file_hash(path) if meta["type"] == "file" else None
            meta["benign"] = benign_index(path, meta)
            found[rel] = meta
    return found, False


def snapshot_site(site, ignore=()):
    """Everything step 1 compares for one WordPress site."""
    root = Path(site["path"])
    if not root.is_dir():
        raise OSError("Site path unavailable")
    content = root / "wp-content"
    dropins = {}
    if content.is_dir():
        with os.scandir(content) as items:
            for item in items:
                if item.name.lower().endswith(".php") and not item.is_dir(follow_symlinks=False):
                    meta = entry(item.path)
                    meta["sha256"] = file_hash(item.path) if meta["type"] == "file" else None
                    dropins[item.name] = meta
    root_files = {}
    for name in ROOT_FILES:
        path = root / name
        if os.path.lexists(path):
            meta = entry(path)
            meta["sha256"] = file_hash(path) if meta["type"] == "file" else None
            root_files[name] = meta
    uploads, truncated = uploads_executables(str(content / "uploads"), ignore)
    return {"plugins": listing(content / "plugins"), "mu_plugins": listing(content / "mu-plugins", hashed=True),
            "dropins": dropins, "root_files": root_files, "uploads": uploads, "uploads_truncated": truncated,
            "themes": listing(content / "themes")}


# --- comparison ---------------------------------------------------------------------------------

def clean(name):
    """Attacker-controlled names: no control characters, no backticks, bounded length."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(name)).replace("`", "'")
    return text if len(text) <= 120 else text[:117] + "..."


def stamp(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "unknown time"


def describe(meta):
    size = f", {meta['size']:,} bytes" if meta.get("size") is not None else ""
    return f"{meta['type']}{size}, modified {stamp(meta.get('mtime'))}"


# (key in snapshot, label, path under the site root, new, changed, removed, compare contents)
CATEGORIES = (
    ("plugins", "plugin", "wp-content/plugins", "CRITICAL", None, "WARNING", False),
    ("mu_plugins", "must-use plugin", "wp-content/mu-plugins", "CRITICAL", "CRITICAL", "WARNING", True),
    ("dropins", "drop-in", "wp-content", "CRITICAL", "CRITICAL", "WARNING", True),
    ("root_files", "core file", "", "CRITICAL", "CRITICAL", "WARNING", True),
    ("uploads", "PHP file in uploads", "wp-content", "CRITICAL", "CRITICAL", "INFO", True),
    ("themes", "theme", "wp-content/themes", "WARNING", None, "INFO", False),
)


def diff_site(site, old, new):
    findings = []
    root = site["path"].rstrip("/")
    for key, label, base, on_new, on_change, on_remove, contents in CATEGORIES:
        before, after = old.get(key) or {}, new.get(key) or {}
        where = f"{root}/{base}".rstrip("/")
        for name in sorted(after.keys() - before.keys()):
            meta, level = after[name], on_new
            if key == "uploads" and meta.get("benign"):
                level = "INFO"
            findings.append((level, f"New {label} `{clean(name)}` ({describe(meta)})", f"ls -la '{where}/{clean(name)}'"))
        for name in sorted(before.keys() - after.keys()):
            findings.append((on_remove, f"Removed {label} `{clean(name)}`", None))
        for name in sorted(before.keys() & after.keys()):
            a, b = before[name], after[name]
            if a["type"] != b["type"]:
                findings.append(("CRITICAL" if key != "themes" else "WARNING",
                                 f"{label.capitalize()} `{clean(name)}` changed from {a['type']} to {b['type']}",
                                 f"ls -la '{where}/{clean(name)}'"))
            elif contents and on_change and (a.get("sha256"), a.get("size")) != (b.get("sha256"), b.get("size")):
                findings.append((on_change, f"Changed {label} `{clean(name)}` ({describe(b)})", f"ls -la '{where}/{clean(name)}'"))
    if new.get("uploads_truncated") and not old.get("uploads_truncated"):
        findings.append(("WARNING", "The uploads scan hit its size/time limit; PHP files beyond it are not checked", None))
    return findings


def first_run_findings(new):
    """Pre-existing PHP in uploads may be an old backdoor: list it once."""
    return [("WARNING", f"PHP file already in uploads at first run: `{clean(name)}` ({describe(meta)})", None)
            for name, meta in sorted(new.get("uploads", {}).items()) if not meta.get("benign")]


# --- origin health ------------------------------------------------------------------------------

def check_origin(site):
    paths = site.get("health_paths", ["/", "/wp-login.php"] if site.get("kind", "wordpress") == "wordpress" else ["/"])
    return [collector.origin_health(site["id"], site["origin_ip"], path, site.get("ca_file")) for path in paths]


def origin_transition(previous, checks, first):
    """Returns (new origin state, findings). Two failing runs in a row raise one CRITICAL."""
    failing = [c for c in checks if not c["ok"]]
    state = dict(previous or {"fails": 0, "down": False})
    if not failing:
        findings = [("INFO", "Site is answering again at the origin", None)] if state["down"] else []
        return {"fails": 0, "down": False}, findings
    state["fails"] += 1
    detail = "; ".join(f"{clean(c['path'])}: {c.get('status') or c.get('error') or 'failed'}"
                       + (" (critical-error page)" if c.get("critical_error") else "") for c in failing)
    if first:
        return {"fails": 2, "down": True}, [("INFO", f"Site was already failing at the origin at first run ({detail})", None)]
    if state["fails"] >= 2 and not state["down"]:
        state["down"] = True
        return state, [("CRITICAL", f"Site down at the origin on two runs in a row: {detail}", None)]
    return state, []


# --- other WordPress installs -------------------------------------------------------------------

def discover_installs(home="/home"):
    found = set()
    for pattern in ("*/public_html/wp-config.php", "*/domains/*/public_html/wp-config.php"):
        for path in Path(home).glob(pattern):
            found.add(str(path.parent))
    return found


# --- alerts -------------------------------------------------------------------------------------

def format_messages(site_id, level, findings, mention=None, now=None):
    header = f"**{level} · {clean(site_id)}** ({(now or datetime.now()).strftime('%Y-%m-%d %H:%M')})"
    if level == "CRITICAL" and mention:
        header = f"{mention} {header}"
    lines = []
    for _, text, command in findings:
        lines.append(f"• {text}")
        if command:
            lines.append(f"  Look: `{command}`")
    messages, current = [], header
    for line in lines:
        if len(current) + 1 + len(line) > DISCORD_LIMIT:
            messages.append(current)
            current = f"{header} (continued)"
        current += "\n" + line[:DISCORD_LIMIT - len(header) - 20]
    messages.append(current)
    return messages


def allowed_mentions(mention, content):
    if not mention or mention not in content:
        return {"parse": []}
    match = MENTION.fullmatch(mention)
    if match and match.group(1):
        return {"parse": [], "users": [match.group(1)]}
    if match and match.group(2):
        return {"parse": [], "roles": [match.group(2)]}
    return {"parse": ["everyone"]}


def send_discord(url, content, mention=None, opener=None):
    body = json.dumps({"content": content, "username": "Site Watch",
                       "allowed_mentions": allowed_mentions(mention, content)}).encode("utf-8")
    request = Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json", "User-Agent": "wp-site-watch/0.1"})
    try:
        with (opener or build_opener(collector.NoRedirect())).open(request, timeout=15) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


class Spool:
    """Undelivered messages, resent oldest first and marked as delayed."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def add(self, content, created=None):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        created = created or datetime.now(timezone.utc).isoformat(timespec="seconds")
        fd, name = tempfile.mkstemp(prefix=f"{time.time_ns():020d}-", suffix=".json", dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"created": created, "content": content}, stream)

    def items(self):
        return sorted(self.directory.glob("*.json")) if self.directory.is_dir() else []

    def flush(self, deliver):
        sent = 0
        for path in self.items():
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            local = datetime.fromisoformat(item["created"]).astimezone().strftime("%Y-%m-%d %H:%M")
            if not deliver(f"(delayed, raised {local}) {item['content']}"[:2000]):
                break
            path.unlink(missing_ok=True)
            sent += 1
        return sent


# --- state --------------------------------------------------------------------------------------

def empty_state():
    return {"version": 1, "sites": {}, "origin": {}, "unchecked": [], "installs": None,
            "quiet": {}, "pending_info": [], "last_digest": None}


def load_state(directory, keep_bad=True):
    path = Path(directory) / "state.json"
    if not path.exists():
        return empty_state(), None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported state")
        return {**empty_state(), **state}, None
    except (OSError, ValueError):
        if keep_bad:
            path.replace(path.with_name(f"state.json.corrupt-{int(time.time())}"))
        return empty_state(), ("WARNING", "Watcher state was unreadable; a new baseline was recorded. "
                                          "Changes since the last good run may have been missed.", None)


def save_state(directory, state):
    fd, name = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, Path(directory) / "state.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def quiet_until(state, site_id, now):
    until = state["quiet"].get(site_id, 0)
    return until if until > now else 0


# --- one run ------------------------------------------------------------------------------------

def run(config, watch, state, now=None, installs=None, origin=check_origin, snapshot=snapshot_site):
    """Compare every site with the stored state. Returns (messages to send now, new state)."""
    now = now or time.time()
    clock = datetime.fromtimestamp(now)
    state = json.loads(json.dumps(state))
    first_ever = not state["sites"] and not state["origin"]
    alerts, info = [], []  # alerts: (site, findings); info: (site, text)
    sites = config["sites"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        health = dict(zip([s["id"] for s in sites], pool.map(origin, sites)))
    for site in sites:
        sid, findings = site["id"], []
        first = sid not in state["origin"]
        if site.get("kind", "wordpress") == "wordpress":
            try:
                new = snapshot(site, watch["ignore"])
            except OSError:
                new = None
            if new is None:
                if sid not in state["unchecked"]:
                    state["unchecked"].append(sid)
                    findings.append(("WARNING", "Site files could not be checked (path missing or unreadable)", None))
            else:
                if sid in state["unchecked"]:
                    state["unchecked"].remove(sid)
                    findings.append(("INFO", "Site files can be checked again", None))
                findings += first_run_findings(new) if sid not in state["sites"] else diff_site(site, state["sites"][sid], new)
                state["sites"][sid] = new
        state["origin"][sid], origin_findings = origin_transition(state["origin"].get(sid), health[sid], first)
        findings += origin_findings
        if quiet_until(state, sid, now):
            findings = [("INFO" if level == "WARNING" else level, text, cmd) for level, text, cmd in findings]
        urgent = [f for f in findings if f[0] != "INFO"]
        if urgent:
            alerts.append((sid, urgent))
        info += [(sid, text) for level, text, _ in findings if level == "INFO"]
    current = installs if installs is not None else discover_installs()
    configured = {s["path"].rstrip("/") for s in sites if s.get("path")}
    if state["installs"] is not None:
        for path in sorted(current - set(state["installs"]) - configured):
            alerts.append(("server", [("WARNING", f"New WordPress install found: `{clean(path)}` (not in the watcher config)",
                                       f"ls -la '{clean(path)}'")]))
    state["installs"] = sorted(current)
    messages = []
    for sid, findings in alerts:
        for level in ("CRITICAL", "WARNING"):
            selected = [f for f in findings if f[0] == level]
            if selected:
                messages += format_messages(sid, level, selected, watch.get("mention"), clock)
    if first_ever:
        names = ", ".join(clean(s["id"]) for s in sites)
        messages.append(f"**INFO · Site Watch** ({clock:%Y-%m-%d %H:%M})\nWatching {len(sites)} sites: {names}. "
                        "Baseline recorded; you will be alerted to changes from now on.")
    state["pending_info"] += [{"time": int(now), "site": sid, "text": text} for sid, text in info]
    state["pending_info"] = state["pending_info"][-500:]
    today = clock.strftime("%Y-%m-%d")
    if first_ever:
        state["last_digest"] = today if clock.hour >= watch["digest_hour"] else state["last_digest"]
    elif clock.hour >= watch["digest_hour"] and state["last_digest"] != today:
        messages += digest(state, sites, clock)
        state["pending_info"], state["last_digest"] = [], today
    return messages, state


def digest(state, sites, clock):
    down = [s for s, o in state["origin"].items() if o.get("down")]
    lines = [f"Watcher alive. {len(sites)} sites watched.",
             f"Down at the origin: {', '.join(map(clean, down))}." if down else "All sites answer at the origin.",
             f"Not checked: {', '.join(map(clean, state['unchecked']))}." if state["unchecked"] else "All site files checked."]
    if state["pending_info"]:
        lines.append("Since the last digest:")
        lines += [f"• {stamp(i['time'])} {clean(i['site'])}: {i['text']}" for i in state["pending_info"]]
    else:
        lines.append("Nothing else to report.")
    return format_messages("daily digest", "INFO", [("INFO", line, None) for line in lines], now=clock)


# --- command line -------------------------------------------------------------------------------

@contextmanager
def locked(directory):
    import fcntl
    with (Path(directory) / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another watcher run is active") from exc
        yield


def prepare_state_dir(directory=STATE_DIR):
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Watcher state directory must be a real root-owned 0700 directory")
    return directory


def deliver_all(watch, messages, spool, dry_run=False, send=send_discord, pause=0.6):
    if dry_run:
        for message in messages:
            print(message, end="\n\n")
        return
    def deliver(content):
        ok = send(watch["webhook_url"], content, watch.get("mention"))
        time.sleep(pause)  # Discord allows about 5 webhook messages per 2 seconds.
        return ok
    spool.flush(deliver)
    if spool.items():
        for message in messages:  # Discord unreachable: keep order, try again next run.
            spool.add(message)
        return
    for index, message in enumerate(messages):
        if not deliver(message):
            for rest in messages[index:]:
                spool.add(rest)
            return


def main(argv=None):
    parser = argparse.ArgumentParser(prog="site-watch", description="WP Site Watch alert watcher (Part A, step 1)")
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="check all sites and send alerts")
    run_cmd.add_argument("--dry-run", action="store_true", help="print alerts; send and save nothing")
    commands.add_parser("baseline", help="record the current state of all sites without alerting")
    rebase = commands.add_parser("rebaseline", help="accept the current state of one site")
    rebase.add_argument("site")
    quiet = commands.add_parser("quiet", help="downgrade WARNING to INFO for a while (CRITICAL still alerts)")
    quiet.add_argument("site")
    quiet.add_argument("minutes", type=int)
    commands.add_parser("status", help="last run, per-site state, spool size")
    commands.add_parser("test-alert", help="send one test message")
    args = parser.parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("The watcher must run as root on Linux")
    try:
        config, watch = collector.load_config(COLLECTOR_CONFIG), load_watch_config(WATCH_CONFIG)
        directory = prepare_state_dir()
        spool = Spool(directory / "spool")
        with locked(directory):
            state, problem = load_state(directory, keep_bad=not getattr(args, "dry_run", False))
            if args.command == "test-alert":
                ok = send_discord(watch["webhook_url"], "**INFO · Site Watch** Test alert: delivery works.")
                print("Test alert sent." if ok else "Test alert failed. Check webhook_url and outbound HTTPS.")
                return 0 if ok else 1
            if args.command == "status":
                print(json.dumps({"sites": sorted(state["sites"]), "origin": state["origin"], "unchecked": state["unchecked"],
                                  "quiet": {s: stamp(t) for s, t in state["quiet"].items() if t > time.time()},
                                  "pending_info": len(state["pending_info"]), "last_digest": state["last_digest"],
                                  "spooled": len(spool.items())}, indent=2))
                return 0
            if args.command == "quiet":
                if args.site not in {s["id"] for s in config["sites"]} or not 1 <= args.minutes <= 24 * 60:
                    parser.error("Unknown site or minutes outside 1-1440")
                state["quiet"][args.site] = int(time.time()) + args.minutes * 60
                save_state(directory, state)
                print(f"WARNING downgraded to INFO for {args.site} until {stamp(state['quiet'][args.site])}. CRITICAL still alerts.")
                return 0
            if args.command == "rebaseline":
                site = next((s for s in config["sites"] if s["id"] == args.site), None)
                if site is None:
                    parser.error("Unknown site")
                if site.get("kind", "wordpress") == "wordpress":
                    state["sites"][site["id"]] = snapshot_site(site, watch["ignore"])
                save_state(directory, state)
                print(f"Accepted the current state of {args.site}.")
                return 0
            if args.command == "baseline":
                _, state = run(config, watch, empty_state())
                save_state(directory, state)
                print(f"Baseline recorded for {len(config['sites'])} sites. No alerts were sent.")
                return 0
            messages, new_state = run(config, watch, state)
            if problem:
                messages.insert(0, format_messages("Site Watch", problem[0], [problem])[0])
            deliver_all(watch, messages, spool, dry_run=args.dry_run)
            if not args.dry_run:
                save_state(directory, new_state)
            print(f"Checked {len(config['sites'])} sites; {len(messages)} message(s){' printed' if args.dry_run else ''}.")
            return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"site-watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
