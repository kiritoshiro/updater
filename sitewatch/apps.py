"""Updates for our own web apps: the newest commit of one GitHub branch, installed only when the
repository's security gate passed on that exact commit.

Backup, health check and rollback work as for WordPress (updater.Updater). Every file, Git,
Composer and database operation runs as the app's owner; root only keeps the backup and passes it
through pipes. Three ways to deploy (collector.json "app.type"):

- laravel: a Git checkout; maintenance mode, checkout, composer install, migrations, caches.
- git: a Git checkout and nothing else (the app migrates itself on the next request).
- copy: files copied from one folder of the commit's GitHub archive over the web root (public
  repositories only), as the app's own updater does; storage/last-update.json records the commit.
"""

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote

from . import collector
from .appfiles import check_archive as check_app_archive
from .collector import CheckFailed
from .updater import UPDATE_LIMIT, Refused, Updater, download

API = "https://api.github.com"
GATE_JOBS = ("All security checks passed", "security-gate / All security checks passed")
# Only these workflows may vouch for a commit, and never a pull request run: that one scanned the
# pull request's merge preview, with fewer checks, not the commit on the branch.
GATE_WORKFLOWS = (".github/workflows/security-gate.yml", ".github/workflows/release.yml")
GATES = ("passed", "failed", "pending", "missing")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_APP_ZIP = 128 * 1024 * 1024
CODE_ROOT = Path(__file__).resolve().parent.parent
DB_HELPER = Path(__file__).resolve().with_name("appdb.php")
GITHUB_ERRORS = (OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


# --- GitHub ---------------------------------------------------------------------------------------

def github(path, token=None, fetch=collector.fetch_json):
    return fetch(API + path, "application/vnd.github+json", limit=4 * 1024 * 1024, token=token)


def clean_message(text):
    """First line of a commit message, printable and short: it is shown, never interpreted."""
    line = str(text or "").strip().split("\n", 1)[0]
    return re.sub(r"[\x00-\x1f\x7f]", "", line)[:120] or None


def branch_head(repository, branch, api):
    data = api(f"/repos/{repository}/commits/{quote(branch, safe='')}")
    sha = str(data.get("sha", "")).lower()
    if not SHA.fullmatch(sha):
        raise ValueError("No commit")
    commit = data.get("commit") if isinstance(data.get("commit"), dict) else {}
    date = (commit.get("committer") or {}).get("date") if isinstance(commit.get("committer"), dict) else None
    return {"sha": sha, "date": date if isinstance(date, str) and len(date) <= 40 else None,
            "message": clean_message(commit.get("message"))}


def gate_state(repository, sha, api):
    """passed, failed, pending or missing: the security gate job on exactly this commit."""
    runs = api(f"/repos/{repository}/actions/runs?head_sha={sha}&per_page=50").get("workflow_runs", [])
    seen = set()
    for run in runs if isinstance(runs, list) else []:
        if (not isinstance(run, dict) or run.get("head_sha") != sha or run.get("path") not in GATE_WORKFLOWS
                or run.get("event") == "pull_request" or type(run.get("id")) is not int):
            continue
        jobs = api(f"/repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
        found = False
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict) and job.get("name") in GATE_JOBS and job.get("head_sha") == sha:
                found = True
                if job.get("status") != "completed":
                    seen.add("pending")
                elif job.get("conclusion") == "success":
                    return "passed"
                else:
                    seen.add("failed")
        if not found and run.get("status") != "completed":
            seen.add("pending")  # the gate job starts after the scans
    return "pending" if "pending" in seen else "failed" if "failed" in seen else "missing"


def relation(repository, base, head, api):
    """GitHub's compare status of head against base: ahead, behind, diverged or identical."""
    status = api(f"/repos/{repository}/compare/{base}...{head}?per_page=1").get("status")
    if status not in ("ahead", "behind", "diverged", "identical"):
        raise ValueError("Unexpected compare status")
    return status


# --- installed commit -------------------------------------------------------------------------------

def git_argv(site, *args, limit=60):
    return collector.as_owner(site, "/usr/bin/git", "-c", "core.sshCommand=ssh -o BatchMode=yes -o ConnectTimeout=20",
                              "-C", site["path"], *args, limit=limit)


def record_path(site):
    return str(Path(site["path"]) / "storage" / "last-update.json")


def installed_commit(site, run=collector.run):
    if site["app"]["type"] == "copy":
        record = json.loads(run(collector.as_owner(site, "/usr/bin/cat", "--", record_path(site))))
        sha = str(record.get("sha", "")) if isinstance(record, dict) else ""
    else:
        sha = run(git_argv(site, "rev-parse", "--verify", "HEAD"))
    if not SHA.fullmatch(sha):
        raise CheckFailed("Installed commit unavailable")
    return sha


def app_inventory(site, token, fetch=collector.fetch_json, run=collector.run):
    """The installed commit and, when the branch is ahead of it, the newest commit and its gate."""
    app = site["app"]
    api = lambda path: github(path, token, fetch)
    result = {"type": app["type"], "repository": app["repository"], "branch": app["branch"], "commit": None,
              "relation": None, "update": None, "check_state": "unknown"}
    errors = []
    try:
        result["commit"] = installed_commit(site, run)
    except (CheckFailed, ValueError):
        errors.append("Installed commit unavailable")
    try:
        head = branch_head(app["repository"], app["branch"], api)
        result["relation"] = "identical" if head["sha"] == result["commit"] else (
            relation(app["repository"], result["commit"], head["sha"], api) if result["commit"] else None)
        if result["relation"] == "ahead":
            result["update"] = {**head, "gate": gate_state(app["repository"], head["sha"], api)}
        elif result["relation"] in ("behind", "diverged"):
            errors.append(f"The installed commit is not on the {app['branch']} branch's history")
        result["check_state"] = "checked"
    except GITHUB_ERRORS:
        errors.append("GitHub update check unavailable")
    return result, errors


# --- one update -------------------------------------------------------------------------------------

class AppUpdater(Updater):
    """Updater's flow (check, backup, change, health check, rollback) for kind "app"; slug is the
    commit. A rollback always restores the database: migrations may have changed it."""

    def __init__(self, site, inventory, token=None, fetch_json=collector.fetch_json, **kwargs):
        super().__init__(site, inventory, **kwargs)
        self.app, self.token, self.fetch_json = site["app"], token, fetch_json
        self.before = None  # installed commit and branch, for the rollback
        self.added = []     # copy: top-level names the new commit writes

    def api(self, path):
        return github(path, self.token, self.fetch_json)

    def git(self, *args, limit=60):
        return self.run(git_argv(self.site, *args, limit=limit), timeout=limit + 10)

    def artisan(self, *args):
        return self.owner(self.app["php"], str(Path(self.site["path"]) / "artisan"), *args, "--no-interaction", limit=UPDATE_LIMIT)

    def helper(self, *args, **streams):
        """appfiles.py as the owner, from root-owned code."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from sitewatch.appfiles import main; sys.exit(main(sys.argv[2:]))"
        return self.owner("/usr/bin/python3", "-I", "-c", code, str(CODE_ROOT), *args, limit=UPDATE_LIMIT, **streams)

    def source(self, kind, slug):
        if kind != "app" or not SHA.fullmatch(str(slug)):
            raise Refused("An app update names the commit to install")
        offered = (self.inventory.get("app") or {}).get("update") or {}
        if offered.get("sha") != slug:
            raise Refused("The last inventory does not offer this commit; refresh and try again")
        try:
            head = branch_head(self.app["repository"], self.app["branch"], self.api)
            if head["sha"] != slug:
                raise Refused(f"The {self.app['branch']} branch has moved on since the last check; refresh and try again")
            gate = gate_state(self.app["repository"], slug, self.api)
        except GITHUB_ERRORS as exc:
            raise Refused("GitHub could not be reached to verify the commit") from exc
        if gate != "passed":
            raise Refused(f"The security gate has not passed on this commit ({gate})")
        return "github"

    def version(self, kind, slug):
        try:
            return installed_commit(self.site, lambda argv: self.run(argv, timeout=70))
        except (CheckFailed, ValueError):
            return None

    def prepare_release(self, kind, slug):
        """Fetch (Git) or download and check (copy) before anything is backed up or changed."""
        current = self.version(kind, slug)
        if self.app["type"] == "copy":
            try:
                ahead = relation(self.app["repository"], current, slug, self.api) == "ahead"
            except GITHUB_ERRORS as exc:
                raise Refused("GitHub could not be reached to compare the commits") from exc
            if not ahead:
                raise Refused("The new commit does not build on the installed one")
            try:
                data = download(f"{API}/repos/{self.app['repository']}/zipball/{slug}", limit=MAX_APP_ZIP)
            except CheckFailed as exc:
                raise Refused("The commit archive could not be downloaded") from exc
            try:
                self.added = check_app_archive(data, self.app.get("source", "server"), self.app.get("keep", []))
            except ValueError as exc:
                raise Refused(f"The commit archive was refused: {exc}") from exc
            self.before = {"sha": current}
            return data
        try:
            if self.git("status", "--porcelain=v1", "--untracked-files=no"):
                raise Refused("The app's checkout has local changes; commit or undo them first")
            try:
                branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD")
            except CheckFailed:
                branch = None  # detached, e.g. by the app's own updater
            self.before = {"sha": current, "branch": branch}
            branch_ref = f"refs/remotes/origin/{self.app['branch']}"
            self.git("fetch", "--no-tags", "--quiet", "origin", f"+refs/heads/{self.app['branch']}:{branch_ref}", limit=300)
            fetched = self.git("rev-parse", "--verify", branch_ref)
        except CheckFailed as exc:
            raise Refused("Fetching the branch with the app's deploy key failed") from exc
        if fetched != slug:
            raise Refused("The fetched branch does not match the commit GitHub reports")
        try:
            self.git("merge-base", "--is-ancestor", "HEAD", slug)
        except CheckFailed as exc:
            raise Refused("The new commit does not build on the installed one") from exc
        return None

    def files(self, kind, slug):
        root = Path(self.site["path"])
        if self.app["type"] == "laravel":
            return root, ["vendor"] if (root / "vendor").is_dir() and not (root / "vendor").is_symlink() else []
        if self.app["type"] == "copy":
            keep = set(self.app.get("keep", []))
            return root, sorted(name for name in os.listdir(root) if name not in keep)
        return root, []

    def export_database(self, stream):
        self.owner(*self.database("dump"), limit=UPDATE_LIMIT, stdout=stream)

    def import_database(self, stream):
        self.owner(*self.database("import"), limit=UPDATE_LIMIT, stdin=stream)

    def database(self, action):
        db = self.app["database"]
        return (self.app["php"], "-n", str(DB_HELPER), action, db["format"], str(Path(self.site["path"]) / db["file"]),
                json.dumps(db.get("keys", {})), db["name"])

    def apply(self, kind, slug, source, archive=None):
        if self.app["type"] == "copy":
            folder = Path(tempfile.mkdtemp(prefix="site-update-", dir="/tmp" if os.name == "posix" else None))
            try:  # root-owned folder: the owner can read the archive but cannot swap it
                os.chmod(folder, 0o711)
                path = folder / "commit.zip"
                path.write_bytes(archive)
                os.chmod(path, 0o644)
                self.helper("install", str(path), self.site["path"], self.app.get("source", "server"),
                            ",".join(self.app.get("keep", [])), ",".join(self.app.get("clear", [])), slug)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            return
        if self.app["type"] == "laravel":
            self.artisan("down", "--retry=60")
        self.git("checkout", "--quiet", "-B", self.app["branch"], slug)
        if self.app["type"] == "laravel":
            self.owner(self.app["php"], self.app["composer"], "install", "--no-dev", "--prefer-dist", "--no-interaction",
                       "--no-progress", "--optimize-autoloader", f"--working-dir={self.site['path']}", limit=UPDATE_LIMIT)
            self.artisan("migrate", "--force")
            self.rebuild_caches()
            self.artisan("up")

    def rebuild_caches(self):
        for command in ("optimize:clear", "config:cache", "route:cache", "view:cache"):
            self.artisan(command)

    def restore(self, directory, folder, names):
        """Old commit, old files (vendor, or the copied web root) and the database, as the owner."""
        laravel = self.app["type"] == "laravel"
        if laravel:
            try:
                self.artisan("down", "--retry=60")
            except CheckFailed:
                pass
        if self.app["type"] == "copy":
            self.restore_files(directory, folder, names, also=self.added, everything=True)
            self.helper("record", self.site["path"], self.before["sha"])
        else:
            target = ["-B", self.before["branch"]] if self.before.get("branch") else ["--detach"]
            self.git("checkout", "--quiet", "--force", *target, self.before["sha"])
            self.restore_files(directory, folder, names)
        with (directory / "database.sql").open("rb") as stream:
            self.import_database(stream)
        if laravel:
            self.rebuild_caches()
            self.artisan("up")
