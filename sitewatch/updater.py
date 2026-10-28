"""Validated WordPress updates: backup, update as the site owner, origin health check, rollback.

Root runs this through one sudoers rule (docs/UPDATER.md). Every argument is checked against the
collector's configuration and last inventory; nothing reaches WP-CLI as free text. Site files and
the database are only read and written as the site's owner, never as root, so a planted symlink
in a site cannot redirect a privileged write.
"""

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from . import collector
from .collector import CheckFailed
from .model import SLUG, decode_status, freshness, numeric_version, utcnow

CONFIG = Path("/etc/site-watch/collector.json")
STATE_DIR = Path("/var/lib/site-watch")
BACKUP_ROOT = Path("/var/backups/site-update")
WATCHER = Path("/usr/local/sbin/site-watch")
LOG_NAME = "update-log.jsonl"
KINDS = ("plugin", "theme", "core", "translations", "all")
KEEP_DAYS = 7
KEEP_LOG_LINES = 1000
MIN_FREE_BYTES = 2 * 1024 ** 3
UPDATE_LIMIT = 600
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_UNZIPPED_BYTES = 256 * 1024 * 1024
HEALTH_ATTEMPTS, HEALTH_DELAY = 3, 5
LOCK_WAIT = 20 * 60
MAX_INVENTORY_AGE = 3 * 3600
EXIT_CODES = {"updated": 0, "unchanged": 0, "refused": 2, "rolled_back": 3, "rollback_failed": 4}
CORE_FILES = re.compile(r"(?!wp-config\.php\Z)[a-z0-9-]+\.php\Z|license\.txt\Z|readme\.html\Z|wp-admin\Z|wp-includes\Z")
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
VERSION_HEADER = re.compile(rb"^[ \t/*#@]*Version:[ \t]*([0-9][0-9a-zA-Z.+-]*)", re.MULTILINE | re.IGNORECASE)


class Refused(Exception):
    """A fixed, public reason. Nothing was changed."""


# --- commands -----------------------------------------------------------------------------------

def execute(argv, timeout=65, stdin=None, stdout=None):
    """Run a fixed argv. Output is never shown or logged: a failing site may print secrets."""
    try:
        proc = subprocess.run(argv, stdin=stdin or subprocess.DEVNULL, stdout=stdout or subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False, env=collector.ENV, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckFailed("Command unavailable or timed out") from exc
    if proc.returncode:
        raise CheckFailed("Command failed")
    return (proc.stdout or b"").decode("utf-8", errors="replace").strip()


class NoForeignRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not github_host(newurl):
            raise CheckFailed("Download redirected outside GitHub")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def github_host(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host == "github.com" or host.endswith(".githubusercontent.com"))


def download(url, limit=MAX_ZIP_BYTES):
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "wp-site-watch/0.1"})
    try:
        with build_opener(NoForeignRedirect()).open(request, timeout=60) as response:
            data = response.read(limit + 1)
    except OSError as exc:
        raise CheckFailed("Release download failed") from exc
    if len(data) > limit:
        raise CheckFailed("Release archive is too large")
    return data


def latest_release(repository, fetch_json=collector.fetch_json):
    """The latest stable release's single ZIP asset, with GitHub's own SHA-256 digest."""
    try:
        release = fetch_json(f"https://api.github.com/repos/{repository}/releases/latest", "application/vnd.github+json")
        if release.get("draft") or release.get("prerelease") or numeric_version(release["tag_name"]) is None:
            raise Refused("The latest GitHub release is not a stable numbered release")
        assets = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(".zip")]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused("The GitHub release could not be read") from exc
    if len(assets) != 1:
        raise Refused("The GitHub release must have exactly one ZIP asset")
    digest = DIGEST.fullmatch(str(assets[0].get("digest", "")))
    url = str(assets[0].get("browser_download_url", ""))
    if not digest or not github_host(url):
        raise Refused("The GitHub release asset has no SHA-256 digest or an unexpected address")
    return {"version": str(release["tag_name"]).removeprefix("v"), "url": url, "sha256": digest[1]}


def check_archive(data, sha256, kind, slug, version):
    """Refuse anything but one folder named like the installed slug, at the release's version."""
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Refused("The downloaded archive does not match the release's SHA-256 digest")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        headers = []
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if ("\\" in entry.filename or path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != slug or stat.S_ISLNK(entry.external_attr >> 16)):
                raise Refused("The release archive must contain only the folder " + slug)
            if len(path.parts) == 2 and (path.name == "style.css" if kind == "theme" else path.suffix == ".php"):
                headers.append(archive.read(entry)[:8192])
        if sum(entry.file_size for entry in entries) > MAX_UNZIPPED_BYTES:
            raise Refused("The release archive is too large when unpacked")
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise Refused("The release archive is not a valid ZIP file") from exc
    found = {match[1].decode() for header in headers for match in [VERSION_HEADER.search(header)] if match}
    if not any(numeric_version(v) == numeric_version(version) for v in found):
        raise Refused(f"The release archive's version header does not say {version}")


# --- one update -----------------------------------------------------------------------------------

class Updater:
    def __init__(self, site, inventory, run=execute, health=None, release=latest_release, fetch=download,
                 sleep=time.sleep, backups=BACKUP_ROOT, say=print):
        self.site, self.inventory, self.run, self.sleep, self.backups, self.say = site, inventory, run, sleep, backups, say
        self.release, self.fetch = release, fetch
        self.health = health or (lambda: [collector.origin_health(site["id"], site["origin_ip"], path, site.get("ca_file"))
                                          for path in site.get("health_paths", ["/", "/wp-login.php"])])

    def wp(self, *args, limit=60, **streams):
        return self.run(collector.wp_argv(self.site, *args, limit=limit), timeout=limit + 10, **streams)

    def owner(self, *argv, limit=300, **streams):
        return self.run(collector.as_owner(self.site, *argv, limit=limit), timeout=limit + 10, **streams)

    def healthy(self):
        for attempt in range(HEALTH_ATTEMPTS):
            checks = self.health()
            if checks and all(check["ok"] for check in checks):
                return True, checks
            if attempt + 1 < HEALTH_ATTEMPTS:
                self.sleep(HEALTH_DELAY)
        return False, checks

    def source(self, kind, slug):
        """wordpress.org or github, after checking the update is in the collector's inventory."""
        if kind in ("core", "translations"):
            if kind == "core" and not (self.inventory.get("core") or {}).get("update_version"):
                raise Refused("The last inventory reports no WordPress core update for this site")
            return "wordpress.org"
        item = next((i for i in self.inventory.get(f"{kind}s", []) if i.get("name") == slug), None)
        if item is None:
            raise Refused(f"No {kind} named {slug} in this site's last inventory")
        if not item.get("update_version"):
            raise Refused(f"The last inventory reports no update for {slug}")
        return "github" if f"{kind}:{slug}" in self.site.get("github", {}) else "wordpress.org"

    def version(self, kind, slug):
        try:
            if kind == "core":
                return self.wp("core", "version")
            if kind in ("plugin", "theme"):
                return self.wp(kind, "get", slug, "--field=version")
        except CheckFailed:
            return None
        return None

    def files(self, kind, slug):
        """(folder, names) to back up, relative to a folder only the site owner writes."""
        root = Path(self.site["path"])
        if kind == "core":
            return root, sorted(name for name in os.listdir(root) if CORE_FILES.match(name))
        if kind == "translations":
            return root / "wp-content", ["languages"] if (root / "wp-content" / "languages").is_dir() else []
        base = root / "wp-content" / f"{kind}s"
        for name in (slug, f"{slug}.php") if kind == "plugin" else (slug,):
            info = os.lstat(base / name) if os.path.lexists(base / name) else None
            if info and stat.S_ISLNK(info.st_mode):
                raise Refused(f"{slug} is a symbolic link; update it by hand")
            if info:
                return base, [name]
        raise Refused(f"{slug} was not found in wp-content/{kind}s")

    def backup(self, kind, slug, label):
        free = shutil.disk_usage(self.backups).free
        if free < MIN_FREE_BYTES:
            raise Refused("Less than 2 GB free for the backup")
        directory = self.backups / self.site["id"] / label
        directory.mkdir(mode=0o700, parents=True)
        folder, names = self.files(kind, slug)
        try:
            with (directory / "database.sql").open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                self.wp("db", "export", "-", limit=UPDATE_LIMIT, stdout=stream)
            if names:
                with (directory / "files.tar").open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    self.owner("/usr/bin/tar", "-C", str(folder), "-cf", "-", "--", *names, stdout=stream)
        except CheckFailed as exc:
            raise Refused("The backup failed; nothing was updated") from exc
        (directory / "backup.json").write_text(json.dumps({"site": self.site["id"], "kind": kind, "slug": slug,
                                                           "folder": str(folder), "names": names}), encoding="utf-8")
        return directory, folder, names

    def restore(self, directory, folder, names, kind):
        """Put the backed-up files and database back. Everything runs as the site owner."""
        if names:
            aside = []
            if kind in ("plugin", "theme") and (folder / names[0]).is_dir():
                aside = [str(folder / f".{names[0]}.site-update-failed")]
                self.owner("/usr/bin/rm", "-rf", "--one-file-system", "--", aside[0])
                self.owner("/usr/bin/mv", "-T", "--", str(folder / names[0]), aside[0])
            with (directory / "files.tar").open("rb") as stream:
                self.owner("/usr/bin/tar", "-C", str(folder), "-xf", "-", stdin=stream)
            for path in aside:
                self.owner("/usr/bin/rm", "-rf", "--one-file-system", "--", path)
        with (directory / "database.sql").open("rb") as stream:
            self.wp("db", "import", "-", limit=UPDATE_LIMIT, stdin=stream)
        try:
            self.wp("maintenance-mode", "deactivate")
        except CheckFailed:
            pass  # not in maintenance mode

    def apply(self, kind, slug, source, archive=None):
        if kind == "translations":
            for target in (("core",), ("plugin", "--all"), ("theme", "--all")):
                self.wp("language", target[0], "update", *target[1:], limit=UPDATE_LIMIT)
        elif kind == "core":
            self.wp("core", "update", limit=UPDATE_LIMIT)
            self.wp("core", "update-db", limit=UPDATE_LIMIT)
        elif source == "wordpress.org":
            self.wp(kind, "update", slug, limit=UPDATE_LIMIT)
        else:
            self.install_archive(kind, slug, archive)

    def prepare_release(self, kind, slug):
        """Download and check our GitHub release before anything is backed up or changed."""
        release = self.release(self.site["github"][f"{kind}:{slug}"])
        current = numeric_version(self.version(kind, slug))
        if current is not None and numeric_version(release["version"]) <= current:
            raise Refused(f"{slug} is already at the latest GitHub release")
        try:
            data = self.fetch(release["url"])
        except CheckFailed as exc:
            raise Refused("The release archive could not be downloaded") from exc
        check_archive(data, release["sha256"], kind, slug, release["version"])
        return data

    def install_archive(self, kind, slug, data):
        # Root-owned folder: the site owner may read the archive but cannot swap it.
        folder = Path(tempfile.mkdtemp(prefix="site-update-"))
        try:
            os.chmod(folder, 0o711)
            archive = folder / f"{slug}.zip"
            archive.write_bytes(data)
            os.chmod(archive, 0o644)
            self.wp(kind, "install", str(archive), "--force", limit=UPDATE_LIMIT)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def update(self, kind, slug=None):
        result = {"time": utcnow(), "site": self.site["id"], "kind": kind, "slug": slug, "source": None,
                  "from": None, "to": None, "result": "refused", "message": "", "backup": None}
        started = time.monotonic()
        try:
            result["source"] = self.source(kind, slug)
            ok, checks = self.healthy()
            if not ok:
                raise Refused("The site fails its origin health check before the update; fix that first")
            result["from"] = self.version(kind, slug)
            if kind in ("plugin", "theme", "core") and result["from"] is None:
                raise Refused("The installed version could not be read")
            archive = self.prepare_release(kind, slug) if result["source"] == "github" else None
            label = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}" + (f"-{slug}" if slug else "")
            directory, folder, names = self.backup(kind, slug, label)
            result["backup"] = str(directory)
            self.quiet()
            self.say(f"Backup saved in {directory}. Updating…")
            try:
                self.apply(kind, slug, result["source"], archive)
                failure = None
            except (CheckFailed, OSError):
                failure = "The update command failed"
            result["to"] = self.version(kind, slug)
            ok, checks = self.healthy() if failure is None else (False, [])
            if ok and kind != "translations" and result["to"] == result["from"]:
                result.update(result="unchanged", message="WP-CLI reported no newer version; nothing changed")
            elif ok:
                result.update(result="updated", message="Updated; the origin health check passed")
            else:
                failure = failure or "The site failed its origin health check after the update"
                result.update(self.rollback(directory, folder, names, kind, slug, result["from"], failure))
            result["health"] = [{"path": c["path"], "status": c.get("status"), "ok": c["ok"]} for c in checks]
        except Refused as exc:
            result["message"] = str(exc)
        result["seconds"] = round(time.monotonic() - started)
        return result

    def rollback(self, directory, folder, names, kind, slug, before, failure):
        self.say(f"{failure}. Restoring the backup…")
        try:
            self.restore(directory, folder, names, kind)
        except (CheckFailed, OSError):
            return {"result": "rollback_failed", "message": f"{failure}. Restoring the backup FAILED; restore by hand from {directory}"}
        ok, _ = self.healthy()
        back = self.version(kind, slug)
        if ok and back == before:
            return {"result": "rolled_back", "to": back, "message": f"{failure}. The backup was restored and the site is healthy again"}
        return {"result": "rollback_failed", "to": back,
                "message": f"{failure}. The backup was restored but the site is still unhealthy or at the wrong version; check it now ({directory})"}

    def quiet(self):
        """Ask the alert watcher, if installed, to treat the expected file changes as INFO."""
        try:
            info = WATCHER.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
                execute([str(WATCHER), "quiet", self.site["id"], "30"], timeout=30)
        except (OSError, CheckFailed):
            pass

    def pending(self):
        """Every plugin and theme update in the inventory, for `all`. Core stays a separate decision."""
        return [(kind, item["name"]) for kind in ("plugin", "theme") for item in self.inventory.get(f"{kind}s", [])
                if item.get("update_version") and SLUG.fullmatch(str(item.get("name", "")))]


# --- state, lock and log --------------------------------------------------------------------------

def private_directory(path, mode=0o700):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError(f"{path} must be a root-owned directory with mode 0700")
    return path


@contextmanager
def update_lock(directory, say=print):
    """One update at a time across all sites; a second request waits its turn."""
    import fcntl
    with (directory / ".update.lock").open("a") as handle:
        deadline = time.monotonic() + LOCK_WAIT
        waiting = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError("Another update is still running; try again later")
                if not waiting:
                    say("Waiting for another update to finish…")
                    waiting = True
                time.sleep(2)
        yield


def append_log(directory, results):
    """Group-readable JSON lines for the dashboard. Versions, names and paths only."""
    import grp
    path = directory / LOG_NAME
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = (lines + [json.dumps(r, ensure_ascii=True) for r in results])[-KEEP_LOG_LINES:]
    fd, name = tempfile.mkstemp(prefix=".update-log-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, grp.getgrnam("sitewatch").gr_gid)
            stream.write("\n".join(lines) + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prune_backups(root, now=None):
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    for site in root.iterdir():
        if site.is_dir() and not site.is_symlink():
            for backup in site.iterdir():
                if backup.is_dir() and not backup.is_symlink() and backup.stat().st_mtime < cutoff:
                    shutil.rmtree(backup)


def describe(r):
    target = r["kind"] + (f" {r['slug']}" if r["slug"] else "")
    versions = f" {r['from']} → {r['to']}" if r.get("from") and r.get("to") and r["from"] != r["to"] else ""
    return f"{r['site']} {target}{versions}: {r['result'].upper()}. {r['message']}."


def main(argv=None):
    parser = argparse.ArgumentParser(prog="site-update", description="Validated WordPress update with backup and rollback")
    parser.add_argument("--json", action="store_true", help="print one JSON line for the dashboard")
    parser.add_argument("site")
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("slug", nargs="?")
    args = parser.parse_args(argv)
    if (args.kind in ("plugin", "theme")) != (args.slug is not None) or (args.slug and not SLUG.fullmatch(args.slug)):
        parser.error("plugin and theme need one valid slug; core, translations and all take none")
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("site-update must run as root on Linux, through sudo")
    say = (lambda *_: None) if args.json else print
    try:
        config = collector.load_config(CONFIG)
        site = next((s for s in config["sites"] if s["id"] == args.site), None)
        if site is None or site.get("kind", "wordpress") != "wordpress":
            parser.error("Unknown WordPress site")
        collector.validate_owner(site)
        status = decode_status((STATE_DIR / "status.json").read_bytes())
        if freshness(status, max_age=MAX_INVENTORY_AGE)["stale"]:
            raise ValueError("The inventory is older than 3 hours; check the collector timer first")
        inventory = next((s for s in status["sites"] if s["id"] == site["id"]), None)
        if inventory is None:
            raise ValueError("The site is missing from the last inventory; wait for the collector")
        private_directory(BACKUP_ROOT)
        with update_lock(STATE_DIR, say):
            prune_backups(BACKUP_ROOT)
            updater = Updater(site, inventory, say=say)
            results = []
            for kind, slug in updater.pending() if args.kind == "all" else [(args.kind, args.slug)]:
                result = updater.update(kind, slug)
                result["user"] = os.environ.get("SUDO_USER") if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", os.environ.get("SUDO_USER", "")) else "root"
                results.append(result)
                say(describe(result))
                if result["result"] not in ("updated", "unchanged"):
                    break
            if not results:
                say("No plugin or theme updates in the last inventory.")
            append_log(STATE_DIR, results)
        if any(r["result"] in ("updated", "rolled_back", "rollback_failed") for r in results):
            try:  # refresh the dashboard's inventory in the background
                execute(["/usr/bin/systemctl", "start", "--no-block", "site-collect.service"], timeout=30)
            except CheckFailed:
                pass
    except (ValueError, OSError, KeyError, CheckFailed) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Setup failed. Check the root-owned config, status.json and backup folder."
        print(json.dumps({"error": message}) if args.json else f"site-update: {message}", file=sys.stdout if args.json else sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"results": results}))
    return max([EXIT_CODES[r["result"]] for r in results] or [0])


if __name__ == "__main__":
    sys.exit(main())
