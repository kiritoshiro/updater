import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from sitewatch import updater
from sitewatch.collector import CheckFailed
from sitewatch.updater import Refused, Updater, check_archive, github_host


def archive(entries, symlink=None):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    data = raw.getvalue()
    return data, hashlib.sha256(data).hexdigest()


PLUGIN = {"cookie/": "", "cookie/cookie.php": "<?php\n/*\n * Plugin Name: Cookie\n * Version: 1.0.17\n */\n", "cookie/js/a.js": "x"}


class ArchiveTests(unittest.TestCase):
    def test_oversized_archive_refused_before_reading(self):
        data, digest = archive(PLUGIN)
        with patch.object(updater, "MAX_UNZIPPED_BYTES", 10), self.assertRaises(Refused):
            check_archive(data, digest, "plugin", "cookie", "1.0.17")

    def test_valid_plugin_and_theme(self):
        data, digest = archive(PLUGIN)
        check_archive(data, digest, "plugin", "cookie", "1.0.17")
        data, digest = archive({"alps/style.css": "/*\nTheme Name: ALPS\nVersion: 3.38.0\n*/"})
        check_archive(data, digest, "theme", "alps", "3.38.0")

    def test_refusals(self):
        cases = {
            "digest": (archive(PLUGIN)[0], "0" * 64, "1.0.17"),
            "other folder": (*archive({"evil/evil.php": "Version: 1.0.17"}), "1.0.17"),
            "traversal": (*archive({**PLUGIN, "cookie/../../wp-config.php": "x"}), "1.0.17"),
            "absolute": (*archive({**PLUGIN, "/cookie/x.php": "x"}), "1.0.17"),
            "symlink": (*archive(PLUGIN, symlink="cookie/link"), "1.0.17"),
            "version": (*archive(PLUGIN), "1.0.18"),
            "not a zip": (b"PK nope", hashlib.sha256(b"PK nope").hexdigest(), "1.0.17"),
        }
        for name, (data, digest, version) in cases.items():
            with self.subTest(name), self.assertRaises(Refused):
                check_archive(data, digest, "plugin", "cookie", version)

    def test_download_hosts(self):
        self.assertTrue(github_host("https://github.com/o/r/releases/download/v1/a.zip"))
        self.assertTrue(github_host("https://release-assets.githubusercontent.com/x"))
        for url in ("http://github.com/x", "https://github.com.evil.example/x", "https://evil.example/githubusercontent.com"):
            self.assertFalse(github_host(url), url)

    def test_release_needs_one_zip_with_digest(self):
        base = {"tag_name": "v1.0.17", "assets": [{"name": "c.zip", "digest": "sha256:" + "a" * 64,
                                                    "browser_download_url": "https://github.com/o/r/releases/download/v1.0.17/c.zip"}]}
        self.assertEqual(updater.latest_release("o/r", lambda *_: base)["version"], "1.0.17")
        for change in ({"prerelease": True}, {"tag_name": "nightly"}, {"assets": []},
                       {"assets": [{**base["assets"][0], "digest": None}]},
                       {"assets": [{**base["assets"][0], "browser_download_url": "https://evil.example/c.zip"}]},
                       {"assets": base["assets"] * 2}):
            with self.subTest(change), self.assertRaises(Refused):
                updater.latest_release("o/r", lambda *_: {**base, **change})


class FakeSite:
    """A pretend server: WP-CLI calls change versions; tar/db calls are recorded."""

    def __init__(self, root, version="1.0", after="1.1", update_fails=False):
        self.version, self.after, self.update_fails = version, after, update_fails
        self.calls = []

    def __call__(self, argv, timeout=65, stdin=None, stdout=None):
        args = argv[argv.index("--no-color") + 1:] if "--no-color" in argv else argv[argv.index("--") + 4:]
        self.calls.append(args)
        if stdout is not None:
            stdout.write(b"backup")
        if args[:2] in (["plugin", "get"], ["theme", "get"]) or args[:2] == ["core", "version"]:
            return self.version
        if args[:2] == ["plugin", "update"] or args[:2] == ["plugin", "install"]:
            if self.update_fails:
                raise CheckFailed("Command failed")
            self.version = self.after
        if args[:1] == ["/usr/bin/tar"] and "-xf" in args:  # the old files are back
            self.version = "1.0"
        if args[:2] == ["maintenance-mode", "deactivate"]:
            raise CheckFailed("Command failed")
        return ""

    def ran(self, *prefix):
        return any(call[:len(prefix)] == list(prefix) for call in self.calls)


@unittest.skipUnless(hasattr(os, "fchmod"), "os.fchmod needs Unix or Python 3.13+")
class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "site/wp-content/plugins/cookie").mkdir(parents=True)
        (root / "site/wp-content/plugins/classic").mkdir(parents=True)
        (root / "backups").mkdir()
        self.site = {"id": "example.org", "owner": "example", "path": str(root / "site"), "origin_ip": "203.0.113.10",
                     "ca_file": None, "github": {"plugin:cookie": "o/cookie"}}
        self.inventory = {"id": "example.org", "core": {"version": "6.8", "update_version": None},
                          "plugins": [{"name": "classic", "version": "1.0", "update_version": "1.1"},
                                      {"name": "cookie", "version": "1.0", "update_version": "1.1"},
                                      {"name": "current", "version": "2.0", "update_version": None}], "themes": []}
        self.backups = root / "backups"
        self.health_results = []

    def tearDown(self):
        self.tmp.cleanup()

    def health(self):
        return [{"path": "/", "status": 200, "ok": self.health_results.pop(0) if self.health_results else True}]

    def updater(self, fake, **extra):
        return Updater(self.site, self.inventory, run=fake, health=self.health, sleep=lambda _: None,
                       backups=self.backups, say=lambda *_: None, **extra)

    def test_successful_update(self):
        fake = FakeSite(None)
        result = self.updater(fake).update("plugin", "classic")
        self.assertEqual((result["result"], result["from"], result["to"]), ("updated", "1.0", "1.1"))
        self.assertTrue(fake.ran("db", "export", "-"))
        self.assertTrue(any(c[0] == "/usr/bin/tar" and "-cf" in c for c in fake.calls))
        self.assertFalse(fake.ran("db", "import"))
        backup = Path(result["backup"])
        self.assertEqual(sorted(p.name for p in backup.iterdir()), ["backup.json", "database.sql", "files.tar"])

    def test_broken_site_rolls_back(self):
        fake = FakeSite(None)
        self.health_results = [True] + [False] * updater.HEALTH_ATTEMPTS
        result = self.updater(fake).update("plugin", "classic")
        self.assertEqual((result["result"], result["to"]), ("rolled_back", "1.0"))
        self.assertTrue(any(c[0] == "/usr/bin/mv" for c in fake.calls))
        self.assertTrue(any(c[0] == "/usr/bin/tar" and "-xf" in c for c in fake.calls))
        self.assertTrue(fake.ran("db", "import", "-"))

    def test_failed_command_rolls_back(self):
        result = self.updater(FakeSite(None, update_fails=True)).update("plugin", "classic")
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("update command failed", result["message"])

    def test_rollback_that_does_not_heal_is_reported(self):
        self.health_results = [True] + [False] * (2 * updater.HEALTH_ATTEMPTS)
        result = self.updater(FakeSite(None)).update("plugin", "classic")
        self.assertEqual(result["result"], "rollback_failed")

    def test_unhealthy_site_is_refused_before_backup(self):
        self.health_results = [False] * updater.HEALTH_ATTEMPTS
        fake = FakeSite(None)
        result = self.updater(fake).update("plugin", "classic")
        self.assertEqual(result["result"], "refused")
        self.assertFalse(fake.ran("db", "export", "-"))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_only_inventory_updates_are_allowed(self):
        fake = FakeSite(None)
        for kind, slug in (("plugin", "missing"), ("plugin", "current"), ("theme", "classic"), ("core", None)):
            with self.subTest(slug=slug):
                self.assertEqual(self.updater(fake).update(kind, slug)["result"], "refused")
        self.assertEqual(fake.calls, [])

    def test_unchanged_version(self):
        result = self.updater(FakeSite(None, after="1.0")).update("plugin", "classic")
        self.assertEqual(result["result"], "unchanged")

    def test_github_release_is_checked_before_backup(self):
        data, digest = archive({"cookie/cookie.php": "<?php\n/*\n * Version: 1.1\n */"})
        release = lambda repo: {"version": "1.1", "url": "https://github.com/x", "sha256": digest}
        fake = FakeSite(None)
        result = self.updater(fake, release=release, fetch=lambda url: data).update("plugin", "cookie")
        self.assertEqual((result["result"], result["source"], result["to"]), ("updated", "github", "1.1"))
        install = next(c for c in fake.calls if c[:2] == ["plugin", "install"])
        self.assertTrue(install[2].endswith("cookie.zip") and install[3] == "--force")
        self.assertFalse(Path(install[2]).exists())
        fake = FakeSite(None)
        result = self.updater(fake, release=release, fetch=lambda url: b"tampered").update("plugin", "cookie")
        self.assertEqual(result["result"], "refused")
        self.assertFalse(fake.ran("db", "export", "-"))

    def test_symlinked_plugin_refused(self):
        plugins = Path(self.site["path"]) / "wp-content/plugins"
        (plugins / "classic").rmdir()
        try:
            (plugins / "classic").symlink_to(plugins / "cookie")
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.updater(FakeSite(None)).update("plugin", "classic")["result"], "refused")

    def failing_after_backup(self, error):
        """The update command raises `error` (the backup has been taken by then)."""
        fake = FakeSite(None)
        updater_ = self.updater(fake)
        def broken(*args, **kwargs):
            raise error
        updater_.apply = broken
        return updater_, fake

    def test_interrupt_after_backup_rolls_back(self):
        updater_, fake = self.failing_after_backup(KeyboardInterrupt())
        result = updater_.update("plugin", "classic")
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("interrupted", result["message"])
        self.assertTrue(updater_.interrupted)
        self.assertTrue(fake.ran("db", "import", "-"))

    def test_unexpected_error_after_backup_rolls_back(self):
        fake = FakeSite(None)
        updater_ = self.updater(fake)
        # Reading the version works before the update and crashes after it.
        updater_.version = lambda *a: (_ for _ in ()).throw(RuntimeError("boom")) if fake.version == "1.1" else fake.version
        result = updater_.update("plugin", "classic")
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("unexpected error", result["message"])
        self.assertFalse(updater_.interrupted)

    def test_interrupt_before_backup_changes_nothing(self):
        updater_ = self.updater(FakeSite(None))
        updater_.health = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        result = updater_.update("plugin", "classic")
        self.assertEqual(result["result"], "refused")
        self.assertIn("before anything was changed", result["message"])
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_broken_output_does_not_stop_rollback(self):
        def say(*_):
            raise BrokenPipeError()
        fake = FakeSite(None)
        self.health_results = [True] + [False] * updater.HEALTH_ATTEMPTS
        updater_ = Updater(self.site, self.inventory, run=fake, health=self.health, sleep=lambda _: None,
                           backups=self.backups, say=say)
        self.assertEqual(updater_.update("plugin", "classic")["result"], "rolled_back")

    def test_unreadable_new_version_rolls_back(self):
        fake = FakeSite(None, after=None)
        result = self.updater(fake).update("plugin", "classic")
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("could not be read", result["message"])

    def test_failed_recheck_is_rollback_failed_and_kept(self):
        fake = FakeSite(None)
        calls = iter([True])
        def health():
            ok = next(calls, None)
            if ok is None:
                raise RuntimeError("check crashed")
            return [{"path": "/", "status": 200, "ok": ok}]
        updater_ = Updater(self.site, self.inventory, run=fake, health=health, sleep=lambda _: None,
                           backups=self.backups, say=lambda *_: None)
        result = updater_.update("plugin", "classic")
        self.assertEqual(result["result"], "rollback_failed")
        self.assertTrue((Path(result["backup"]) / "KEEP").exists())
        past = time.time() - 30 * 86400
        os.utime(result["backup"], (past, past))
        updater.prune_backups(self.backups)
        self.assertTrue(Path(result["backup"]).exists())

    def test_core_rollback_moves_new_core_folders_aside(self):
        root = Path(self.site["path"])
        for name in ("wp-admin", "wp-includes"):
            (root / name).mkdir()
        for name in ("index.php", "wp-config.php", "wp-load.php"):
            (root / name).write_text("<?php")
        self.inventory["core"]["update_version"] = "6.9"
        fake = FakeSite(None)
        self.health_results = [True] + [False] * updater.HEALTH_ATTEMPTS
        def run(argv, timeout=65, stdin=None, stdout=None):
            args = argv[argv.index("--no-color") + 1:] if "--no-color" in argv else []
            if args[:2] == ["core", "update"]:
                fake.version = "1.1"
            return fake(argv, timeout, stdin, stdout)
        result = Updater(self.site, self.inventory, run=run, health=self.health, sleep=lambda _: None,
                         backups=self.backups, say=lambda *_: None).update("core")
        self.assertEqual(result["result"], "rolled_back")
        tar = next(c for c in fake.calls if c[0] == "/usr/bin/tar" and "-cf" in c)
        self.assertNotIn("wp-config.php", tar)
        self.assertTrue({"wp-admin", "wp-includes", "index.php"} <= set(tar))
        moved = [Path(c[-2]).name for c in fake.calls if c[0] == "/usr/bin/mv"]
        self.assertEqual(sorted(moved), ["wp-admin", "wp-includes"])

    def test_files_only_rollback_keeps_the_database(self):
        fake = FakeSite(None)
        self.health_results = [True] + [False] * updater.HEALTH_ATTEMPTS
        result = self.updater(fake, restore_database=False).update("plugin", "classic")
        self.assertEqual((result["result"], result["to"]), ("rolled_back", "1.0"))
        self.assertFalse(fake.ran("db", "import"))
        self.assertTrue(fake.ran("db", "export", "-"))  # still backed up, for a manual restore
        self.assertIn("database was left as it was", result["message"])

    def test_nightly_takes_wordpress_minor_releases_only(self):
        for current, offered, included in (("6.8", "6.8.3", True), ("6.8.2", "6.8.3", True), ("6.8.3", "6.9", False),
                                           ("6.9", "7.0", False), ("6.8", None, False)):
            with self.subTest(current=current, offered=offered):
                self.inventory["core"] = {"version": current, "update_version": offered}
                items = self.updater(FakeSite(None)).pending(core_minor=True)
                self.assertEqual(("core", None) in items, included)
                if included:
                    self.assertEqual(items[-1], ("core", None))  # after plugins and themes
        self.assertNotIn(("core", None), self.updater(FakeSite(None)).pending())

    def test_pending_lists_plugins_and_themes_only(self):
        self.inventory["core"]["update_version"] = "6.9"
        self.assertEqual(self.updater(FakeSite(None)).pending(), [("plugin", "classic"), ("plugin", "cookie")])


class SignalTests(unittest.TestCase):
    def tearDown(self):
        updater.STOP.update(held=0, requested=False)

    def test_signal_only_sets_a_flag(self):
        updater.interrupt()
        updater.interrupt()  # a second Ctrl+C must not raise either
        self.assertTrue(updater.STOP["requested"])
        with updater.signals_held():
            updater.stop_requested()  # held: a restore keeps running
        with self.assertRaises(KeyboardInterrupt):
            updater.stop_requested()

    @unittest.skipUnless(os.name == "posix", "real child process")
    def test_running_command_is_stopped_and_waited_for(self):
        import threading
        threading.Timer(0.5, updater.interrupt).start()
        started = time.monotonic()
        with patch.object(updater.collector, "ENV", {"PATH": "/usr/bin:/bin"}), patch.object(updater, "stop", wraps=updater.stop) as stop:
            with self.assertRaises(KeyboardInterrupt):
                updater.execute(["/bin/sleep", "30"], timeout=60)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsNotNone(stop.call_args[0][0].returncode)  # waited: the child has exited

    @unittest.skipUnless(os.name == "posix", "real child process")
    def test_held_command_finishes(self):
        updater.interrupt()
        with updater.signals_held():
            self.assertEqual(updater.execute(["/bin/echo", "done"], timeout=10), "done")

    def test_stop_during_health_check_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site/wp-content/plugins/classic").mkdir(parents=True)
            (root / "backups").mkdir()
            site = {"id": "example.org", "owner": "example", "path": str(root / "site"), "origin_ip": "203.0.113.10", "ca_file": None, "github": {}}
            inventory = {"plugins": [{"name": "classic", "version": "1.0", "update_version": "1.1"}], "themes": []}
            fake, checks = FakeSite(None), []
            def health():
                checks.append(fake.version)
                if fake.version == "1.1" and len(checks) == 2:
                    updater.interrupt()  # Ctrl+C while the site is checked after the update
                return [{"path": "/", "status": 200, "ok": True}]
            if not hasattr(os, "fchmod"):
                self.skipTest("os.fchmod needs Unix or Python 3.13+")
            result = Updater(site, inventory, run=fake, health=health, sleep=lambda _: None,
                             backups=root / "backups", say=lambda *_: None).update("plugin", "classic")
            self.assertEqual((result["result"], result["to"]), ("rolled_back", "1.0"))
            self.assertIn("interrupted", result["message"])

    def test_second_signal_during_rollback_does_not_stop_it(self):
        # A second signal during the rollback must not stop it: only the flag is set.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site/wp-content/plugins/classic").mkdir(parents=True)
            (root / "backups").mkdir()
            site = {"id": "example.org", "owner": "example", "path": str(root / "site"), "origin_ip": "203.0.113.10", "ca_file": None, "github": {}}
            inventory = {"plugins": [{"name": "classic", "version": "1.0", "update_version": "1.1"}], "themes": []}
            fake = FakeSite(None)
            def run(argv, timeout=65, stdin=None, stdout=None):
                args = argv[argv.index("--no-color") + 1:] if "--no-color" in argv else []
                if args[:2] == ["plugin", "update"]:
                    fake.version = "1.1"
                    raise KeyboardInterrupt
                if args[:2] == ["db", "import"]:
                    updater.interrupt()  # another Ctrl+C while restoring
                return fake(argv, timeout, stdin, stdout)
            if not hasattr(os, "fchmod"):
                self.skipTest("os.fchmod needs Unix or Python 3.13+")
            result = Updater(site, inventory, run=run, health=lambda: [{"path": "/", "status": 200, "ok": True}],
                             sleep=lambda _: None, backups=root / "backups", say=lambda *_: None).update("plugin", "classic")
            self.assertEqual((result["result"], result["to"]), ("rolled_back", "1.0"))
            self.assertIn("interrupted", result["message"])


class CommandLineTests(unittest.TestCase):
    def test_nightly_arguments_are_validated(self):
        for argv in (["example.org", "nightly"], ["example.org", "nightly", "maybe"], ["--nightly-run", "example.org"],
                     ["--files-only-rollback", "example.org", "nightly", "on"], ["--files-only-rollback", "--nightly-run"], []):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised, patch("sys.stderr"):
                updater.main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_nightly_policy_ignores_bad_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(updater.read_nightly(root), {})
            (root / "nightly.json").write_text('{"sites": {"a.org": {"enabled": true}, "b.org": {"enabled": "yes"}, "c.org": 1}}')
            self.assertEqual(updater.read_nightly(root), {"a.org": {"enabled": True}})
            (root / "nightly.json").write_text("not json")
            self.assertEqual(updater.read_nightly(root), {})

    def test_arguments_are_validated(self):
        for argv in (["example.org", "plugin"], ["example.org", "plugin", "../x"], ["example.org", "core", "x"],
                     ["example.org", "plugin", "a;rm"], ["example.org", "shell"], ["example.org", "plugin", "-x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised, patch("sys.stderr"):
                updater.main(argv)
            self.assertEqual(raised.exception.code, 2)

    @unittest.skipIf(os.name == "posix" and os.geteuid() == 0, "running as root")
    def test_non_root_refused(self):
        with self.assertRaises(SystemExit) as raised, patch("sys.stderr"):
            updater.main(["example.org", "plugin", "classic"])
        self.assertEqual(raised.exception.code, 2)

    def test_prune_keeps_recent_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "example.org/old", root / "example.org/new"
            old.mkdir(parents=True); new.mkdir()
            past = time.time() - 8 * 86400
            os.utime(old, (past, past))
            updater.prune_backups(root)
            self.assertEqual([p.name for p in (root / "example.org").iterdir()], ["new"])


if __name__ == "__main__":
    unittest.main()
