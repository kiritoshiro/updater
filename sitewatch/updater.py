"""Validated WordPress updates: backup, update as the site owner, origin health check, rollback.

Root runs this through one sudoers rule (docs/UPDATER.md). Every argument is checked against the
collector's configuration and last inventory; nothing reaches WP-CLI as free text. Site files and
the database are only read and written as the site's owner, never as root, so a planted symlink
in a site cannot redirect a privileged write.
"""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
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
NIGHTLY_NAME = "nightly.json"
KINDS = ("plugin", "theme", "core", "translations", "all")
KEEP_DAYS = 7
KEEP_LOG_LINES = 1000
MIN_FREE_BYTES = 2 * 1024 ** 3
UPDATE_LIMIT = 600
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_UNZIPPED_BYTES = 256 * 1024 * 1024
HEALTH_ATTEMPTS, HEALTH_DELAY = 3, 5
SETTLE_SECONDS = 3  # PHP's opcache rechecks changed files every 2 s by default
LOCK_WAIT, DASHBOARD_LOCK_WAIT = 20 * 60, 5 * 60
MAX_INVENTORY_AGE = 3 * 3600
EXIT_CODES = {"updated": 0, "unchanged": 0, "refused": 2, "rolled_back": 3, "rollback_failed": 4}
CORE_FILES = re.compile(r"(?!wp-config\.php\Z)[a-z0-9-]+\.php\Z|license\.txt\Z|readme\.html\Z|wp-admin\Z|wp-includes\Z")
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
VERSION_HEADER = re.compile(rb"^[ \t/*#@]*Version:[ \t]*([0-9][0-9a-zA-Z.+-]*)", re.MULTILINE | re.IGNORECASE)


class Refused(Exception):
    """A fixed, public reason. Nothing was changed."""


# --- commands -----------------------------------------------------------------------------------

def execute(argv, timeout=65, stdin=None, stdout=None):
    """Run a fixed argv. Output is never shown or logged: a failing site may print secrets.
    Commands run in their own session, so a terminal hangup or Ctrl+C reaches only site-update.
    A stop request (see interrupt) is taken here and only here: the command is stopped and waited
    for, then KeyboardInterrupt is raised at this known point, unless a restore is running."""
    stop_requested()
    try:
        proc = subprocess.Popen(argv, stdin=stdin or subprocess.DEVNULL, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=collector.ENV, cwd="/", start_new_session=os.name == "posix")
    except OSError as exc:
        raise CheckFailed("Command unavailable") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                output, _ = proc.communicate(timeout=max(0.1, min(1, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                stop_requested()
    except BaseException as exc:
        stop(proc)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise CheckFailed("Command timed out") from exc
        raise
    if proc.returncode:
        raise CheckFailed("Command failed")
    return (output or b"").decode("utf-8", errors="replace").strip()


def stop(proc):
    """SIGTERM to sudo, which passes it to timeout, which passes it to WP-CLI; then wait for all of
    them, so nothing keeps changing the site while a rollback runs."""
    with signals_held():
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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
    except (OSError, http.client.HTTPException) as exc:
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
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
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
        if sum(entry.file_size for entry in entries) > MAX_UNZIPPED_BYTES:
            raise Refused("The release archive is too large when unpacked")
        headers = []
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if ("\\" in entry.filename or path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != slug or stat.S_ISLNK(entry.external_attr >> 16)):
                raise Refused("The release archive must contain only the folder " + slug)
            if len(path.parts) == 2 and (path.name == "style.css" if kind == "theme" else path.suffix == ".php"):
                with archive.open(entry) as stream:
                    headers.append(stream.read(8192))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise Refused("The release archive is not a valid ZIP file") from exc
    found = {match[1].decode() for header in headers for match in [VERSION_HEADER.search(header)] if match}
    if not any(numeric_version(v) == numeric_version(version) for v in found):
        raise Refused(f"The release archive's version header does not say {version}")


# --- one update -----------------------------------------------------------------------------------

class Updater:
    def __init__(self, site, inventory, run=execute, health=None, release=latest_release, fetch=download,
                 sleep=time.sleep, backups=BACKUP_ROOT, say=print, restore_database=True):
        self.site, self.inventory, self.run, self.sleep, self.backups = site, inventory, run, sleep, backups
        # False: a failed update restores the files only and leaves the database as it is (the
        # database backup is still taken, for a manual restore).
        self.restore_database = restore_database
        # Nightly: WordPress core only within the installed x.y branch (wp core update --minor).
        self.core_minor_only = False
        self.say = quietly(say)
        self.interrupted = False
        self.release, self.fetch = release, fetch
        self.health = health or (lambda: [collector.origin_health(site["id"], site["origin_ip"], path, site.get("ca_file"))
                                          for path in site.get("health_paths", ["/", "/wp-login.php"])])

    def wp(self, *args, limit=60, **streams):
        return self.run(collector.wp_argv(self.site, *args, limit=limit), timeout=limit + 10, **streams)

    def owner(self, *argv, limit=300, **streams):
        return self.run(collector.as_owner(self.site, *argv, limit=limit), timeout=limit + 10, **streams)

    def healthy(self):
        """Origin checks, retried. A stop request is taken before each attempt (not during a restore)."""
        for attempt in range(HEALTH_ATTEMPTS):
            stop_requested()
            checks = self.health()
            if checks and all(check["ok"] for check in checks):
                return True, checks
            if attempt + 1 < HEALTH_ATTEMPTS:
                self.sleep(HEALTH_DELAY)
        return False, checks

    def source(self, kind, slug):
        """wordpress.org or github, after checking the update is in the collector's inventory."""
        if kind in ("core", "translations"):
            core = self.inventory.get("core") or {}
            if kind == "core" and not (core.get("update_version") or core.get("minor_update_version")):
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
        folder, names = self.files(kind, slug)
        try:
            directory.parent.mkdir(mode=0o700, exist_ok=True)
            for attempt in range(2, 12):  # two backups of one item within a second get -2, -3, ...
                try:
                    directory.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    directory = directory.with_name(f"{label}-{attempt}")
            else:
                raise OSError("No free backup folder name")
            with (directory / "database.sql").open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                self.wp("db", "export", "-", limit=UPDATE_LIMIT, stdout=stream)
            if names:
                with (directory / "files.tar").open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    self.owner("/usr/bin/tar", "-C", str(folder), "-cf", "-", "--", *names, stdout=stream)
            with (directory / "backup.json").open("x", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"site": self.site["id"], "kind": kind, "slug": slug, "folder": str(folder), "names": names}, stream)
        except (CheckFailed, OSError) as exc:
            raise Refused("The backup failed; nothing was updated") from exc
        return directory, folder, names

    def restore(self, directory, folder, names):
        """Put the backed-up files and database back. Everything runs as the site owner. Backed-up
        folders are moved aside first, so files the new version added do not survive the rollback."""
        if names:
            aside = []
            for name in names:
                if (folder / name).is_dir() and not (folder / name).is_symlink():
                    aside.append(str(folder / f".{name}.site-update-failed"))
                    self.owner("/usr/bin/rm", "-rf", "--one-file-system", "--", aside[-1])
                    self.owner("/usr/bin/mv", "-T", "--", str(folder / name), aside[-1])
            with (directory / "files.tar").open("rb") as stream:
                self.owner("/usr/bin/tar", "-C", str(folder), "-xf", "-", stdin=stream)
            for path in aside:
                self.owner("/usr/bin/rm", "-rf", "--one-file-system", "--", path)
        if self.restore_database:
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
            self.wp("core", "update", *(["--minor"] if self.core_minor_only else []), limit=UPDATE_LIMIT)
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
        folder = Path(tempfile.mkdtemp(prefix="site-update-", dir="/tmp" if os.name == "posix" else None))
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
            self.last_backup = (directory, folder, names)
            result["backup"] = str(directory)
            try:
                self.change(kind, slug, archive, result)
                failure = None if result["result"] in ("updated", "unchanged") else result.pop("failure")
            except BaseException as exc:
                # Anything unexpected after the backup, including Ctrl+C or SIGTERM: put the site back.
                self.interrupted = self.interrupted or not isinstance(exc, Exception)
                failure = "The update was interrupted" if self.interrupted else "The update stopped on an unexpected error"
            if failure:
                result.update(self.rollback(directory, folder, names, kind, slug, result["from"], failure))
            if result["result"] == "rollback_failed":
                keep(directory)
            self.interrupted = self.interrupted or STOP["requested"]
        except Refused as exc:
            result["message"] = str(exc)
        except BaseException as exc:
            self.interrupted = self.interrupted or not isinstance(exc, Exception)
            reason = "Interrupted" if self.interrupted else "Stopped on an unexpected error"
            if result["backup"] and result["result"] in ("refused", "failed"):
                # Defensive: something escaped after the backup; put the site back.
                result.update(self.rollback(*self.last_backup, kind, slug, result["from"], reason))
            elif not result["backup"]:
                result["message"] = reason + " before anything was changed"
        result["seconds"] = round(time.monotonic() - started)
        return result

    def change(self, kind, slug, archive, result):
        """Update and check. Sets result to updated/unchanged, or to failed with a reason."""
        self.quiet()
        self.say(f"Backup saved in {result['backup']}. Updating…")
        try:
            self.apply(kind, slug, result["source"], archive)
        except (CheckFailed, OSError):
            result.update(result="failed", failure="The update command failed")
            return
        result["to"] = self.version(kind, slug)
        if kind != "translations" and result["to"] is None:
            result.update(result="failed", failure="The new version could not be read")
            return
        old, new = numeric_version(result["from"]), numeric_version(result["to"])
        if kind == "core" and self.core_minor_only and old and new and new[:2] != old[:2]:
            result.update(result="failed", failure="WordPress moved to a new major version; nightly updates stay within the installed branch")
            return
        stop_requested()
        self.sleep(SETTLE_SECONDS)
        ok, checks = self.healthy()
        stop_requested()  # a stop during the last check still rolls back, as documented
        result["health"] = [{"path": c["path"], "status": c.get("status"), "ok": c["ok"]} for c in checks]
        if not ok:
            result.update(result="failed", failure="The site failed its origin health check after the update")
        elif kind != "translations" and result["to"] == result["from"]:
            result.update(result="unchanged", message="WP-CLI reported no newer version; nothing changed")
        else:
            result.update(result="updated", message="Updated; the origin health check passed")

    def rollback(self, directory, folder, names, kind, slug, before, failure):
        """Never raises: a failed restore or re-check is reported as rollback_failed."""
        self.say(f"{failure}. Restoring the backup…")
        with signals_held():
            try:
                self.restore(directory, folder, names)
            except Exception:
                return {"result": "rollback_failed", "message": f"{failure}. Restoring the backup FAILED; restore by hand from {directory}"}
            try:
                ok, _ = self.healthy()
                back = self.version(kind, slug)
            except Exception:
                ok, back = False, None
        what = "The backup was restored" if self.restore_database else "The files were restored (the database was left as it was)"
        if ok and back == before:
            return {"result": "rolled_back", "to": back, "message": f"{failure}. {what} and the site is healthy again"}
        return {"result": "rollback_failed", "to": back,
                "message": f"{failure}. {what} but the site is still unhealthy or at the wrong version; check it now ({directory})"}

    def quiet(self):
        """Ask the alert watcher, if installed, to treat the expected file changes as INFO."""
        try:
            info = WATCHER.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
                execute([str(WATCHER), "quiet", self.site["id"], "30"], timeout=30)
        except (OSError, CheckFailed):
            pass

    def pending(self, core_minor=False):
        """Every plugin and theme update in the inventory, for `all`. A new WordPress major version
        stays a manual decision; with core_minor (nightly), a release of the installed x.y branch,
        which is how WordPress ships security fixes, comes last."""
        items = [(kind, item["name"]) for kind in ("plugin", "theme") for item in self.inventory.get(f"{kind}s", [])
                 if item.get("update_version") and SLUG.fullmatch(str(item.get("name", "")))]
        core = self.inventory.get("core") or {}
        current = numeric_version(core.get("version"))
        offered = numeric_version(core.get("minor_update_version") or core.get("update_version"))
        if core_minor and current and offered and offered > current and offered[:2] == current[:2]:
            items.append(("core", None))
        return items


def quietly(say):
    """Progress output must never stop an update, e.g. when the SSH session has gone."""
    def wrapped(*args):
        try:
            say(*args)
        except (OSError, ValueError):
            pass
    return wrapped


STOP = {"held": 0, "requested": False}


def interrupt(*_):
    """Ctrl+C and SIGTERM only set a flag. The update stops at the next safe point, the running
    command (execute), and is rolled back if the backup exists. Python code is never interrupted
    halfway, so a result cannot be lost or mislabelled."""
    STOP["requested"] = True


def stop_requested():
    if STOP["requested"] and not STOP["held"]:
        raise KeyboardInterrupt


@contextmanager
def signals_held():
    STOP["held"] += 1
    try:
        yield
    finally:
        STOP["held"] -= 1


def keep(directory):
    """A failed rollback's backup may be the only good copy: prune_backups leaves it alone."""
    try:
        (directory / "KEEP").write_text("Rollback failed; delete this folder by hand once the site is fixed.\n", encoding="utf-8")
    except OSError:
        pass



# --- state, lock and log --------------------------------------------------------------------------

def private_directory(path, mode=0o700):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError(f"{path} must be a root-owned directory with mode 0700")
    return path


@contextmanager
def update_lock(directory, say=print, wait=LOCK_WAIT):
    """One update at a time across all sites; a second request waits its turn."""
    import fcntl
    with (directory / ".update.lock").open("a") as handle:
        deadline = time.monotonic() + wait
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
                stop_requested()
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
                if (backup.is_dir() and not backup.is_symlink() and backup.stat().st_mtime < cutoff
                        and not (backup / "KEEP").exists()):
                    shutil.rmtree(backup)


def read_nightly(directory=STATE_DIR):
    """{site: {"enabled": bool, "changed_at": str, "by": str}}; missing or unreadable means all off."""
    try:
        data = json.loads((directory / NIGHTLY_NAME).read_text(encoding="utf-8"))
        sites = data.get("sites", {}) if isinstance(data, dict) else {}
        if not isinstance(sites, dict):
            return {}
        return {k: v for k, v in sites.items() if isinstance(v, dict) and type(v.get("enabled")) is bool}
    except (OSError, ValueError):
        return {}


def write_nightly(directory, site_id, enabled, user):
    """Atomic, root:sitewatch 0640 so the dashboard can show the switches."""
    import fcntl
    import grp
    with (directory / ".nightly.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two switches at once must not lose one
        sites = read_nightly(directory)
        sites[site_id] = {"enabled": enabled, "changed_at": utcnow(), "by": user}
        replace_nightly(directory, sites, grp.getgrnam("sitewatch").gr_gid)
    return sites[site_id]


def replace_nightly(directory, sites, group):
    fd, name = tempfile.mkstemp(prefix=".nightly-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, group)
            json.dump({"sites": sites}, stream, indent=2)
            stream.write("\n")
        os.replace(name, directory / NIGHTLY_NAME)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_items(updater, items, user, say, results):
    """Update items one by one; log each as soon as it is done; stop at the first problem."""
    for kind, slug in items:
        result = updater.update(kind, slug)
        result["user"] = user
        try:
            append_log(STATE_DIR, [result])
        except (OSError, KeyError, ValueError):
            result["message"] += ". The update log could not be written"
        results.append(result)
        say(describe(result))
        if result["result"] not in ("updated", "unchanged") or updater.interrupted or STOP["requested"]:
            return False
    return True


def load_inventory(site):
    status = decode_status((STATE_DIR / "status.json").read_bytes())
    if freshness(status, max_age=MAX_INVENTORY_AGE)["stale"]:
        raise ValueError("The inventory is older than 3 hours; check the collector timer first")
    inventory = next((s for s in status["sites"] if s["id"] == site["id"]), None)
    if inventory is None:
        raise ValueError("The site is missing from the last inventory; wait for the collector")
    return inventory


def skipped(site, reason, user):
    """A nightly site that could not be updated at all, as a log item the dashboard shows."""
    return {"time": utcnow(), "site": site["id"], "kind": "all", "slug": None, "source": None, "from": None, "to": None,
            "result": "refused", "message": f"Nightly run skipped this site: {reason}", "backup": None, "seconds": 0, "user": user}


def nightly_run(config, say, results, user="nightly"):
    """Timer job: every site whose nightly switch is on gets its plugin, theme and WordPress minor
    updates, each with backup, health check and rollback. Runs the collector first. Returns the
    number of sites that had to be skipped, so the service can fail visibly."""
    policy = read_nightly()
    sites = [s for s in config["sites"] if s.get("kind", "wordpress") == "wordpress" and policy.get(s["id"], {}).get("enabled")]
    if not sites:
        say("No site has nightly updates switched on.")
        return 0
    skipped_sites = 0
    try:
        with signals_held():
            execute(["/usr/bin/systemctl", "start", "site-collect.service"], timeout=40 * 60)
    except CheckFailed:
        say("The collector run before the nightly updates failed; using the last inventory if it is recent.")
    with update_lock(STATE_DIR, say, LOCK_WAIT):
        prune_backups(BACKUP_ROOT)
        for site in sites:
            try:
                collector.validate_owner(site)
                updater = Updater(site, load_inventory(site), say=say)
            except (ValueError, OSError, KeyError, CheckFailed) as exc:
                item = skipped(site, str(exc) if isinstance(exc, ValueError) else "setup problem (owner, path or inventory)", user)
                try:
                    append_log(STATE_DIR, [item])
                except (OSError, KeyError, ValueError):
                    pass
                say(describe(item))
                skipped_sites += 1
                continue
            updater.core_minor_only = True
            items = updater.pending(core_minor=True)
            if not items:
                say(f"{site['id']}: nothing to update.")
            run_items(updater, items, user, say, results)
            if updater.interrupted or STOP["requested"]:
                break
    return skipped_sites


def describe(r):
    target = r["kind"] + (f" {r['slug']}" if r["slug"] else "")
    versions = f" {r['from']} → {r['to']}" if r.get("from") and r.get("to") and r["from"] != r["to"] else ""
    return f"{r['site']} {target}{versions}: {r['result'].upper()}. {r['message']}."


def main(argv=None):
    parser = argparse.ArgumentParser(prog="site-update", description="Validated WordPress update with backup and rollback")
    parser.add_argument("--json", action="store_true", help="print one JSON line for the dashboard")
    parser.add_argument("--files-only-rollback", action="store_true",
                        help="if the update fails, restore the files but leave the database as it is")
    parser.add_argument("--nightly-run", action="store_true", help="run the nightly updates (systemd timer)")
    parser.add_argument("site", nargs="?")
    parser.add_argument("kind", nargs="?", choices=KINDS + ("nightly",))
    parser.add_argument("slug", nargs="?")
    args = parser.parse_args(argv)
    if args.nightly_run:
        if args.site or args.files_only_rollback:
            parser.error("--nightly-run takes no other arguments")
    elif not args.site or not args.kind:
        parser.error("name a site and what to update")
    elif args.kind == "nightly":
        if args.slug not in ("on", "off") or args.files_only_rollback:
            parser.error("nightly takes on or off")
    elif (args.kind in ("plugin", "theme")) != (args.slug is not None) or (args.slug and not SLUG.fullmatch(args.slug)):
        parser.error("plugin and theme need one valid slug; core, translations and all take none")
    elif args.files_only_rollback and args.kind == "core":
        # core update-db upgrades the database; old core files on a newer database are not a rollback.
        parser.error("a WordPress core rollback always restores the database")
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("site-update must run as root on Linux, through sudo")
    say = quietly((lambda *_: None) if args.json else lambda *a: print(*a, flush=True))
    # A dropped SSH session must not stop an update halfway; SIGTERM and Ctrl+C stop it at the next
    # safe point and roll back.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    sudo_user = os.environ.get("SUDO_USER", "")
    user = sudo_user if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", sudo_user) else "root"
    results, skipped_sites = [], 0
    try:
        config = collector.load_config(CONFIG)
        if args.nightly_run:
            private_directory(BACKUP_ROOT)
            # Started by hand through sudo: log who did it, not "nightly".
            skipped_sites = nightly_run(config, say, results, user if sudo_user else "nightly")
        else:
            site = next((s for s in config["sites"] if s["id"] == args.site), None)
            if site is None or site.get("kind", "wordpress") != "wordpress":
                raise ValueError("Unknown WordPress site")
            if args.kind == "nightly":
                setting = write_nightly(STATE_DIR, site["id"], args.slug == "on", user)
                say(f"{site['id']}: nightly updates {'ON' if setting['enabled'] else 'OFF'}.")
                if args.json:
                    print(json.dumps({"nightly": {site["id"]: setting}}))
                return 0
            collector.validate_owner(site)
            inventory = load_inventory(site)
            private_directory(BACKUP_ROOT)
            with update_lock(STATE_DIR, say, DASHBOARD_LOCK_WAIT if args.json else LOCK_WAIT):
                prune_backups(BACKUP_ROOT)
                updater = Updater(site, inventory, say=say, restore_database=not args.files_only_rollback)
                run_items(updater, updater.pending() if args.kind == "all" else [(args.kind, args.slug)], user, say, results)
                if not results:
                    say("No plugin or theme updates in the last inventory.")
        if any(r["result"] in ("updated", "rolled_back", "rollback_failed") for r in results):
            try:  # refresh the dashboard's inventory in the background
                with signals_held():
                    execute(["/usr/bin/systemctl", "start", "--no-block", "site-collect.service"], timeout=30)
            except CheckFailed:
                pass
    except (ValueError, OSError, KeyError, CheckFailed, KeyboardInterrupt) as exc:
        message = (str(exc) if isinstance(exc, ValueError) else "Interrupted" if isinstance(exc, KeyboardInterrupt)
                   else "Setup failed. Check the root-owned config, status.json and backup folder.")
        if results:
            message += "; finished items are in the update log"
        try:
            print(json.dumps({"error": message}) if args.json else f"site-update: {message}", file=sys.stdout if args.json else sys.stderr)
        except (OSError, ValueError):
            pass
        return 1
    if args.json:
        try:
            print(json.dumps({"results": results}))
        except (OSError, ValueError):
            pass  # the dashboard gave up; every item is in the update log
    return max([EXIT_CODES.get(r["result"], 4) for r in results] + [1 if skipped_sites else 0])


if __name__ == "__main__":
    sys.exit(main())
