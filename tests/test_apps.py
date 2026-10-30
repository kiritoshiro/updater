import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from sitewatch import apps, appfiles, collector, model, transport
from sitewatch.apps import AppUpdater, app_inventory, gate_state
from sitewatch.collector import CheckFailed
from sitewatch.updater import Refused

OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40
REPO = "kiritoshiro/app"


def run(path, event="push", status="completed", run_id=1, sha=NEW, branch="main", number=None):
    return {"id": run_id, "run_number": run_id if number is None else number, "head_sha": sha, "head_branch": branch,
            "path": path, "event": event, "status": status}


def job(conclusion="success", status="completed", name="All security checks passed", sha=NEW):
    return {"name": name, "head_sha": sha, "status": status, "conclusion": conclusion}


class FakeGitHub:
    """GitHub API answers by path; branch head NEW, gate passed on a push run, unless changed."""

    def __init__(self, head=NEW, runs=None, jobs=None, compare="ahead", fail=False):
        self.head, self.compare, self.fail = head, compare, fail
        self.runs = [run(".github/workflows/security-gate.yml")] if runs is None else runs
        self.jobs = {1: [job()]} if jobs is None else jobs
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        if self.fail:
            raise OSError("offline")
        if path.startswith(f"/repos/{REPO}/commits/"):
            return {"sha": self.head, "commit": {"message": "Fix the thing\n\nDetails", "committer": {"date": "2026-10-29T10:00:00Z"}}}
        if "/actions/runs?" in path:
            return {"workflow_runs": self.runs}
        if "/actions/runs/" in path:
            return {"jobs": self.jobs.get(int(path.split("/actions/runs/")[1].split("/")[0]), [])}
        if "/compare/" in path:
            return {"status": self.compare}
        raise AssertionError(path)

    def fetch(self, url, accept, limit, token):
        self.token = token
        return self(url.removeprefix(apps.API))


class GateTests(unittest.TestCase):
    def test_states(self):
        gate = ".github/workflows/security-gate.yml"
        cases = [
            ([run(gate)], {1: [job()]}, "passed"),
            ([run(gate, branch="feature")], {1: [job()]}, "missing"),      # passed on another branch only
            ([run(".github/workflows/release.yml")], {1: [job()]}, "missing"),
            ([run(gate)], {1: [job("failure")]}, "failed"),
            ([run(gate)], {1: [job(None, status="in_progress")]}, "pending"),
            ([run(gate, status="in_progress")], {1: []}, "pending"),          # the gate job has not started yet
            ([run(gate, event="pull_request")], {1: [job()]}, "missing"),     # a pull request run never counts
            ([run(gate, event="pull_request_target")], {1: [job()]}, "missing"),  # nor runs of the default branch's workflow
            ([run(gate, event="workflow_run")], {1: [job()]}, "missing"),
            ([run(gate, event="schedule")], {1: [job()]}, "passed"),
            ([run(".github/workflows/other.yml")], {1: [job()]}, "missing"),  # nor does another workflow
            ([run(gate, sha=OTHER)], {1: [job()]}, "missing"),
            ([run(gate)], {1: [job(sha=OTHER)]}, "missing"),
            ([run(gate), run(gate, run_id=2)], {1: [job("failure")], 2: [job()]}, "passed"),  # a newer run passed
            ([run(gate), run(gate, run_id=2, event="schedule")], {1: [job()], 2: [job("failure")]}, "failed"),  # a newer one failed
            ([run(gate), run(gate, run_id=2, status="in_progress")], {1: [job()], 2: []}, "pending"),  # the newest is still going
            ([run(gate, run_id=9, number=1), run(gate, run_id=2, number=5)], {9: [job()], 2: [job("failure")]}, "failed"),  # by run number, not id
            ([], {}, "missing"),
        ]
        for runs, jobs, expected in cases:
            with self.subTest(expected=expected, runs=runs):
                self.assertEqual(gate_state(REPO, NEW, "main", FakeGitHub(runs=runs, jobs=jobs)), expected)

    def test_message_is_one_clean_line(self):
        self.assertEqual(apps.clean_message("Fix\x1b[31m it\x07\nmore"), "Fix[31m it")
        self.assertEqual(len(apps.clean_message("x" * 500)), 120)
        self.assertIsNone(apps.clean_message(None))

    def test_token_goes_to_the_github_api_only(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_): pass

        class Opener:
            def open(self, request, timeout):
                seen.append((request.full_url, request.get_header("Authorization")))
                return Response(b"{}")

        with mock.patch.object(collector, "build_opener", lambda *_: Opener()):
            collector.fetch_json("https://api.github.com/repos/o/r", token="t" * 30)
            collector.fetch_json("https://www.php.net/releases/", token="t" * 30)
        self.assertEqual(seen, [("https://api.github.com/repos/o/r", "Bearer " + "t" * 30), ("https://www.php.net/releases/", None)])


def git_site(root, kind="git"):
    return {"id": "app.example.org", "kind": "web", "owner": "app", "path": str(root), "origin_ip": "203.0.113.10",
            "ca_file": None, "health_paths": ["/"],
            "app": {"type": kind, "repository": REPO, "branch": "main", "php": "/usr/bin/php8.4",
                    "composer": "/usr/local/bin/composer",
                    "database": {"format": "php", "file": "config/db.php", "name": "app",
                                 "keys": {"name": "name", "user": "user", "password": "password"}}}}


class InventoryTests(unittest.TestCase):
    def inventory(self, github, installed=OLD):
        site = git_site("/srv/app")
        return app_inventory(site, "t" * 30, fetch=github.fetch, run=lambda argv: installed)

    def test_newer_commit_with_gate(self):
        result, errors = self.inventory(FakeGitHub())
        self.assertEqual(errors, [])
        self.assertEqual((result["commit"], result["relation"], result["check_state"]), (OLD, "ahead", "checked"))
        self.assertEqual(result["update"], {"sha": NEW, "date": "2026-10-29T10:00:00Z", "message": "Fix the thing", "gate": "passed"})
        model.validate_app(result)

    def test_up_to_date_behind_and_offline(self):
        result, errors = self.inventory(FakeGitHub(head=OLD))
        self.assertEqual((result["relation"], result["update"], errors), ("identical", None, []))
        result, errors = self.inventory(FakeGitHub(compare="diverged"))
        self.assertIsNone(result["update"])
        self.assertEqual(errors, ["The installed commit is not on the main branch's history"])
        result, errors = self.inventory(FakeGitHub(fail=True))
        self.assertEqual((result["check_state"], result["update"], errors), ("unknown", None, ["GitHub update check unavailable"]))

    def test_unreadable_install(self):
        result, errors = self.inventory(FakeGitHub(), installed="not a commit")
        self.assertIsNone(result["commit"])
        self.assertIn("Installed commit unavailable", errors)
        self.assertIsNone(result["update"])


def zipped(entries, symlink=None):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
    return raw.getvalue()


GOOD = {"o-app-bbbbbbb/README.md": "x", "o-app-bbbbbbb/server/index.html": "new", "o-app-bbbbbbb/server/assets/app-2.js": "js",
        "o-app-bbbbbbb/server/api/index.php": "<?php", "o-app-bbbbbbb/server/files/readme.txt": "keep out",
        "o-app-bbbbbbb/server/storage/x": "keep out"}


class ArchiveTests(unittest.TestCase):
    def test_names_and_refusals(self):
        self.assertEqual(appfiles.check_archive(zipped(GOOD), "server", ["files", "storage"]), ["api", "assets", "index.html"])
        for bad in ({"o-app/server/../../etc/x": "x"}, {"/abs/server/x": "x"}, {"a/server/x": "x", "b/server/y": "y"},
                    {"o-app/README.md": "no server folder"}) + (({"o-app\\server\\x": "x"},) if os.sep == "/" else ()):
            with self.subTest(bad), self.assertRaises(ValueError):
                appfiles.check_archive(zipped(bad), "server")
        with self.assertRaises(ValueError):
            appfiles.check_archive(zipped(GOOD, symlink="o-app-bbbbbbb/server/link"), "server")
        with self.assertRaises(ValueError):
            appfiles.check_archive(b"not a zip", "server")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "Unix only")
    def test_install_writes_over_and_keeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "www"
            (root / "assets").mkdir(parents=True)
            (root / "assets/app-1.js").write_text("old build")
            (root / "files").mkdir()
            (root / "files/song.mp3").write_text("upload")
            (root / "storage").mkdir()
            (root / "index.html").write_text("old")
            (root / "local.txt").write_text("not in the commit")
            archive = Path(tmp) / "c.zip"
            archive.write_bytes(zipped(GOOD))
            self.assertEqual(appfiles.main(["install", str(archive), str(root), "server", "files,storage", "assets", NEW]), 0)
            self.assertEqual((root / "index.html").read_text(), "new")
            self.assertEqual(sorted(os.listdir(root / "assets")), ["app-2.js"])
            self.assertEqual((root / "files/song.mp3").read_text(), "upload")
            self.assertFalse((root / "files/readme.txt").exists())
            self.assertEqual((root / "local.txt").read_text(), "not in the commit")
            self.assertEqual(json.loads((root / "storage/last-update.json").read_text())["sha"], NEW)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "Unix only")
    def test_install_never_writes_through_a_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / "www", Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "api").symlink_to(outside)
            (root / "index.html").symlink_to(outside / "planted")
            archive = Path(tmp) / "c.zip"
            archive.write_bytes(zipped(GOOD))
            self.assertEqual(appfiles.main(["install", str(archive), str(root), "server", "files,storage", "", NEW]), 1)
            self.assertEqual(os.listdir(outside), [])


class FakeApp:
    """A pretend app owner: git, artisan, composer and the database helper, by argv."""

    def __init__(self, site, fail_on=None):
        self.site, self.fail_on = site, fail_on
        self.head, self.branch, self.dirty = OLD, "main", ""
        self.dump = b"/*M!999999\\- enable the sandbox mode */ \n-- MariaDB dump 10.19\nCREATE TABLE t (id int);\n-- Dump completed on 2026-10-30\n"
        self.calls, self.imported = [], None

    def __call__(self, argv, timeout=65, stdin=None, stdout=None):
        args = argv[argv.index("--") + 4:]
        if args[0] == "/usr/bin/git":
            args = ["git"] + args[args.index("-C") + 2:]
        elif args[1:2] == ["-n"]:
            args = ["db", args[3]]
        elif args[1:2] == [str(Path(self.site["path"]) / "artisan")]:
            args = ["artisan"] + args[2:-1]
        self.calls.append(args)
        if self.fail_on and args[:len(self.fail_on)] == self.fail_on:
            raise CheckFailed("Command failed")
        if args == ["db", "dump"]:
            stdout.write(self.dump)
        if args == ["db", "import"]:
            self.imported = stdin.read()
        if args[:2] == ["git", "for-each-ref"]:
            return f"refs/heads/main/x {OTHER}\nrefs/heads/main {OLD}"
        if args[:2] == ["git", "rev-parse"]:
            return self.head if args[-1] == "HEAD" else NEW
        if args[:2] == ["git", "status"]:
            return self.dirty
        if args[:2] == ["git", "symbolic-ref"]:
            return self.branch
        if args[:2] == ["git", "checkout"]:
            self.head = args[-1]
        return ""

    def ran(self, *prefix):
        return any(call[:len(prefix)] == list(prefix) for call in self.calls)


@unittest.skipUnless(hasattr(os, "fchmod"), "os.fchmod needs Unix or Python 3.13+")
class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "app/vendor").mkdir(parents=True)
        (self.root / "backups").mkdir()
        self.inventory = {"id": "app.example.org", "app": {"commit": OLD, "update": {"sha": NEW, "gate": "passed"}}}
        self.health_results = []

    def tearDown(self):
        self.tmp.cleanup()

    def health(self):
        return [{"path": "/", "status": 200, "ok": self.health_results.pop(0) if self.health_results else True}]

    def update(self, kind="git", github=None, fail_on=None, prepare=None):
        site = git_site(self.root / "app", kind)
        fake = FakeApp(site, fail_on)
        if prepare:
            prepare(fake)
        github = github or FakeGitHub()
        updater = AppUpdater(site, self.inventory, run=fake, health=self.health, sleep=lambda _: None, fetch_json=github.fetch,
                             backups=self.root / "backups", say=lambda *_: None)
        return updater.update("app", NEW), fake

    def test_git_update(self):
        result, fake = self.update()
        self.assertEqual((result["result"], result["from"], result["to"]), ("updated", OLD, NEW))
        self.assertTrue(fake.ran("git", "fetch", "--no-tags", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"))
        self.assertTrue(fake.ran("git", "checkout", "--quiet", "-B", "main", NEW))
        self.assertTrue(fake.ran("db", "dump"))
        self.assertFalse(fake.ran("db", "import"))

    def test_failed_health_check_rolls_back_commit_and_database(self):
        self.health_results = [True] + [False] * 3
        result, fake = self.update()
        self.assertEqual(result["result"], "rolled_back")
        self.assertTrue(fake.ran("git", "checkout", "--quiet", "--force", "-B", "main", OLD))
        self.assertEqual(fake.imported, fake.dump)
        self.assertEqual(fake.head, OLD)

    def test_detached_install_is_put_back_detached(self):
        self.health_results = [True] + [False] * 3
        def detached(fake):
            fake.branch = None
            fake.fail_on = ["git", "symbolic-ref"]
        result, fake = self.update(prepare=detached)
        self.assertEqual(result["result"], "rolled_back")
        self.assertTrue(fake.ran("git", "checkout", "--quiet", "--force", "--detach", OLD))
        self.assertTrue(fake.ran("git", "update-ref", "refs/heads/main", OLD))  # the branch apply moved is put back

    def test_laravel_rollback_deletes_generated_caches_first(self):
        cache = self.root / "app/bootstrap/cache"
        cache.mkdir(parents=True)
        for name in ("packages.php", "services.php", "routes-v7.php", ".gitignore"):
            (cache / name).write_text("x")
        result, fake = self.update("laravel", fail_on=["artisan", "migrate"])
        self.assertEqual(result["result"], "rolled_back")
        removal = next(i for i, c in enumerate(fake.calls) if c[:2] == ["/usr/bin/rm", "-f"])
        self.assertEqual(sorted(Path(p).name for p in fake.calls[removal][3:]), ["packages.php", "routes-v7.php", "services.php"])
        restored_vendor = max(i for i, c in enumerate(fake.calls) if c[0] == "/usr/bin/tar" and "-xf" in c)
        artisan_after_restore = [i for i, c in enumerate(fake.calls) if c[0] == "artisan" and i > restored_vendor]
        self.assertTrue(restored_vendor < removal < min(artisan_after_restore))
        self.assertLess(removal, next(i for i, c in enumerate(fake.calls) if c == ["db", "import"]))

    def test_laravel_steps_and_failed_migration(self):
        result, fake = self.update("laravel")
        self.assertEqual(result["result"], "updated")
        steps = [c[:2] for c in fake.calls if c[0] in ("artisan",) or c[:2] == ["git", "checkout"]]
        self.assertEqual(steps, [["artisan", "down"], ["git", "checkout"], ["artisan", "migrate"], ["artisan", "optimize:clear"],
                                 ["artisan", "config:cache"], ["artisan", "route:cache"], ["artisan", "view:cache"], ["artisan", "up"]])
        self.assertTrue(any(c[0] == "/usr/bin/tar" and "-cf" in c and c[-1] == "vendor" for c in fake.calls))
        result, fake = self.update("laravel", fail_on=["artisan", "migrate"])
        self.assertEqual(result["result"], "rolled_back")
        self.assertEqual(fake.head, OLD)
        self.assertTrue(fake.ran("db", "import"))
        self.assertEqual([c for c in fake.calls if c[0] == "artisan"][-1], ["artisan", "up"])

    def test_refusals_change_nothing(self):
        cases = [
            ({"github": FakeGitHub(head=OTHER)}, "has moved on"),
            ({"github": FakeGitHub(jobs={1: [job("failure")]})}, "has not passed on this commit (failed)"),
            ({"github": FakeGitHub(runs=[run(".github/workflows/security-gate.yml", event="pull_request")])}, "(missing)"),
            ({"github": FakeGitHub(fail=True)}, "GitHub could not be reached"),
            ({"prepare": lambda fake: setattr(fake, "dirty", " M app.php")}, "local changes"),
            ({"fail_on": ["git", "fetch"]}, "deploy key"),
            ({"fail_on": ["git", "merge-base"]}, "does not build on"),
            ({"fail_on": ["git", "for-each-ref"]}, "Reading the checkout"),
            ({"prepare": lambda fake: setattr(fake, "dump", b"-- MariaDB dump\nCREATE TABLE t (id int);\n")}, "backup failed"),
            ({"prepare": lambda fake: setattr(fake, "dump", b"")}, "backup failed"),
        ]
        for options, message in cases:
            with self.subTest(message):
                result, fake = self.update(**options)
                self.assertEqual(result["result"], "refused")
                self.assertIn(message, result["message"])
                self.assertFalse(fake.ran("git", "checkout"))
                if message != "backup failed":  # an incomplete dump is refused after it was taken
                    self.assertFalse(fake.ran("db", "dump"))
        self.inventory["app"]["update"]["sha"] = OTHER
        result, _ = self.update()
        self.assertIn("does not offer this commit", result["message"])


    def test_copy_app_rechecks_the_branch_after_download(self):
        github = FakeGitHub()
        heads = iter([NEW, OTHER])  # the branch moves while the archive downloads
        original = github.__call__

        def moving(path):
            data = original(path)
            return {**data, "sha": next(heads)} if path.startswith(f"/repos/{REPO}/commits/") else data
        site = git_site(self.root / "app", "copy")
        fake = FakeApp(site)
        fake_record = lambda argv, timeout=65, stdin=None, stdout=None: (
            json.dumps({"sha": OLD}) if "/usr/bin/head" in argv else fake(argv, timeout, stdin, stdout))
        updater = AppUpdater(site, self.inventory, run=fake_record, health=self.health, sleep=lambda _: None,
                             fetch_json=lambda url, accept, limit, token: moving(url.removeprefix(apps.API)),
                             backups=self.root / "backups", say=lambda *_: None)
        with mock.patch.object(apps, "download", lambda url, limit: zipped(GOOD)):
            result = updater.update("app", NEW)
        self.assertEqual(result["result"], "refused")
        self.assertIn("moved on", result["message"])
        self.assertFalse(fake.ran("db", "dump"))


class ConfigTests(unittest.TestCase):
    def config(self, **app):
        site = git_site(Path(tempfile.gettempdir()) / "app")  # absolute on this platform
        site["app"].update(app)
        return {"origin_ip": "203.0.113.10", "sites": [site]}

    def test_valid_and_invalid(self):
        collector.validate_config(self.config())
        collector.validate_config(self.config(type="copy", source="server", keep=["files", "storage"], clear=["assets"]))
        for change in ({"type": "svn"}, {"repository": "no-slash"}, {"branch": "../main"}, {"php": "php"},
                       {"database": {"format": "env", "file": "../.env", "name": "app"}},
                       {"database": {"format": "php", "file": "c.php", "name": "app", "keys": {"name": "n"}}},
                       {"type": "copy", "keep": ["../x"]}):
            with self.subTest(change), self.assertRaises(ValueError):
                collector.validate_config(self.config(**change))
        config = self.config(branch="-main")
        with self.assertRaises(ValueError):
            collector.validate_config(config)
        config = self.config()
        config["sites"][0]["kind"] = "wordpress"  # an app block on a WordPress site is a mistake
        with self.assertRaises(ValueError):
            collector.validate_config(config)
        config = self.config()
        del config["sites"][0]["owner"]
        with self.assertRaises(ValueError):
            collector.validate_config(config)


class TransportTests(unittest.TestCase):
    def test_app_command(self):
        config = {"host": "example.org", "user": "darius", "port": 22}
        self.assertTrue(transport.update_command(config, "app.example.org", "app", NEW)[-1].endswith("--json app.example.org app " + NEW))
        for slug, restore in ((None, True), ("abc", True), (NEW.upper(), True), (NEW, False)):
            with self.subTest(slug=slug, restore=restore), self.assertRaises(ValueError):
                transport.update_command(config, "app.example.org", "app", slug, restore)


if __name__ == "__main__":
    unittest.main()
