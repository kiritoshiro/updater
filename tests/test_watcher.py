from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sitewatch import watcher

WEBHOOK = "https://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz_0123456789"
WATCH = {"webhook_url": WEBHOOK, "mention": None, "digest_hour": 8, "ignore": []}
OK = [{"path": "/", "ok": True, "status": 200}]
DOWN = [{"path": "/", "ok": False, "status": 500, "critical_error": True}]
# Placeholder PHP only. The watcher compares names, sizes and hashes, so real payloads are never
# needed (and webshell-like strings in this file get it quarantined by antivirus).
PLACEHOLDER = "<?php // placeholder file for the watcher tests\n"


def at(hour, minute=0, day=21):
    return datetime(2026, 10, day, hour, minute).timestamp()


class SiteTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "public_html"
        content = self.root / "wp-content"
        for folder in ("plugins/akismet", "mu-plugins", "themes/twentytwentyfive", "uploads/2026/10"):
            (content / folder).mkdir(parents=True)
        (content / "plugins/akismet/akismet.php").write_text("<?php // akismet")
        (content / "mu-plugins/jetpack-helper.php").write_text("<?php // helper")
        (content / "advanced-cache.php").write_text("<?php // cache")
        (self.root / "wp-config.php").write_text("<?php define('DB_NAME', 'x');")
        (self.root / "index.php").write_text("<?php // front controller")
        self.site = {"id": "example.org", "kind": "wordpress", "path": str(self.root), "origin_ip": "203.0.113.10"}
        self.config = {"sites": [self.site]}
        self.health = OK

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, state, now=None, installs=frozenset(), watch=WATCH):
        return watcher.run(self.config, watch, state, now=now or at(12), installs=set(installs),
                           origin=lambda site: self.health)

    def baseline(self):
        return self.run_once(watcher.empty_state())


class BaselineTests(SiteTree):
    def test_first_run_is_one_info_message(self):
        messages, state = self.baseline()
        self.assertEqual(len(messages), 1)
        self.assertIn("Watching 1 sites: example.org", messages[0])
        self.assertIn("example.org", state["sites"])
        self.assertEqual(self.run_once(state)[0], [])

    def test_existing_php_in_uploads_listed_once_at_first_run(self):
        (self.root / "wp-content/uploads/2026/10/old.php").write_text(PLACEHOLDER)
        messages, state = self.baseline()
        self.assertTrue(any("WARNING" in m and "old.php" in m for m in messages))
        self.assertEqual(self.run_once(state)[0], [])


class FileCheckTests(SiteTree):
    def test_new_plugin_folder_is_critical_once(self):
        _, state = self.baseline()
        (self.root / "wp-content/plugins/type-hub").mkdir()
        messages, state = self.run_once(state)
        self.assertEqual(len(messages), 1)
        self.assertIn("CRITICAL · example.org", messages[0])
        self.assertIn("New plugin `type-hub`", messages[0])
        self.assertIn("ls -la", messages[0])
        self.assertEqual(self.run_once(state)[0], [], "alert once, then accept")

    def test_php_in_uploads_is_critical_and_silence_index_is_info(self):
        _, state = self.baseline()
        (self.root / "wp-content/uploads/2026/10/x.PHTML").write_text(PLACEHOLDER + "// more than a comment\n$a = 1;\n")
        (self.root / "wp-content/uploads/2026/10/index.php").write_text("<?php // Silence is golden.\n")
        messages, state = self.run_once(state)
        self.assertEqual(len(messages), 1)
        self.assertIn("x.PHTML", messages[0])
        self.assertNotIn("index.php", messages[0])
        self.assertTrue(any("index.php" in i["text"] for i in state["pending_info"]))

    def test_changed_dropin_core_file_and_mu_plugin_are_critical(self):
        _, state = self.baseline()
        (self.root / "wp-content/advanced-cache.php").write_text("<?php // cache, changed")
        (self.root / "wp-config.php").write_text("<?php define('DB_NAME', 'y'); // changed")
        (self.root / "wp-content/mu-plugins/jetpack-helper.php").write_text("<?php // helper, replaced")
        (self.root / "wp-content/mu-plugins/new-loader.php").write_text("<?php // new")
        messages, _ = self.run_once(state)
        text = "\n".join(messages)
        for expected in ("Changed drop-in `advanced-cache.php`", "Changed core file `wp-config.php`",
                         "Changed must-use plugin `jetpack-helper.php`", "New must-use plugin `new-loader.php`"):
            self.assertIn(expected, text)
        self.assertNotIn("DB_NAME", text, "never file contents")

    def test_removed_plugin_is_warning_and_new_theme_is_warning(self):
        _, state = self.baseline()
        for path in sorted((self.root / "wp-content/plugins/akismet").rglob("*"), reverse=True):
            path.unlink()
        (self.root / "wp-content/plugins/akismet").rmdir()
        (self.root / "wp-content/themes/newtheme").mkdir()
        messages, _ = self.run_once(state)
        self.assertEqual(len(messages), 1)
        self.assertIn("WARNING · example.org", messages[0])
        self.assertIn("Removed plugin `akismet`", messages[0])
        self.assertIn("New theme `newtheme`", messages[0])

    def test_quiet_window_downgrades_warning_never_critical(self):
        _, state = self.baseline()
        state["quiet"]["example.org"] = int(at(13))
        (self.root / "wp-content/themes/newtheme").mkdir()
        (self.root / "wp-content/plugins/new-plugin").mkdir()
        messages, state = self.run_once(state)
        self.assertEqual(len(messages), 1)
        self.assertIn("CRITICAL", messages[0])
        self.assertTrue(any("newtheme" in i["text"] for i in state["pending_info"]))

    def test_uploads_scan_limit_is_reported(self):
        _, state = self.baseline()
        with patch.object(watcher, "UPLOADS_MAX_ENTRIES", 1):
            messages, _ = self.run_once(state)
        self.assertTrue(any("size/time limit" in m for m in messages))

    def test_ignored_uploads_path_is_skipped(self):
        (self.root / "wp-content/uploads/cache").mkdir()
        _, state = self.baseline()
        (self.root / "wp-content/uploads/cache/gen.php").write_text(PLACEHOLDER)
        watch = {**WATCH, "ignore": ["uploads/cache/*"]}
        self.assertEqual(self.run_once(state, watch=watch)[0], [])

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "symlinks")
    def test_symlink_recorded_not_followed(self):
        secret = Path(self.tmp.name) / "secret"
        secret.write_text("root-only")
        os.symlink(secret, self.root / "wp-content/mu-plugins/link.php")
        snapshot = watcher.snapshot_site(self.site)
        self.assertEqual(snapshot["mu_plugins"]["link.php"]["type"], "link")
        self.assertNotIn("sha256", snapshot["mu_plugins"]["link.php"])

    def test_unreadable_site_warns_once(self):
        _, state = self.baseline()
        self.site["path"] = str(self.root / "missing")
        messages, state = self.run_once(state)
        self.assertTrue(any("could not be checked" in m for m in messages))
        self.assertEqual(self.run_once(state)[0], [])


class OriginTests(SiteTree):
    def test_two_failing_runs_raise_one_critical_then_recovery_info(self):
        _, state = self.baseline()
        self.health = DOWN
        first, state = self.run_once(state)
        self.assertEqual(first, [])
        second, state = self.run_once(state)
        self.assertEqual(len(second), 1)
        self.assertIn("Site down at the origin on two runs in a row", second[0])
        self.assertIn("critical-error page", second[0])
        self.assertEqual(self.run_once(state)[0], [])
        self.health = OK
        messages, state = self.run_once(state)
        self.assertEqual(messages, [])
        self.assertTrue(any("answering again" in i["text"] for i in state["pending_info"]))

    def test_already_down_at_first_run_does_not_alert(self):
        self.health = DOWN
        messages, state = self.baseline()
        self.assertEqual(len(messages), 1)
        self.assertTrue(state["origin"]["example.org"]["down"])
        self.assertEqual(self.run_once(state)[0], [])


class ServerTests(SiteTree):
    def test_new_wordpress_install_is_warning(self):
        _, state = self.run_once(watcher.empty_state(), installs={str(self.root)})
        messages, _ = self.run_once(state, installs={str(self.root), "/home/new/public_html"})
        self.assertEqual(len(messages), 1)
        self.assertIn("New WordPress install found: `/home/new/public_html`", messages[0])

    def test_daily_digest_once_after_digest_hour(self):
        _, state = self.run_once(watcher.empty_state(), now=at(7))
        state["pending_info"].append({"time": int(at(7, 30)), "site": "example.org", "text": "Site is answering again at the origin"})
        messages, state = self.run_once(state, now=at(8, 5))
        self.assertEqual(len(messages), 1)
        self.assertIn("Watcher alive", messages[0])
        self.assertIn("answering again", messages[0])
        self.assertEqual(state["pending_info"], [])
        self.assertEqual(self.run_once(state, now=at(9))[0], [])


class DeliveryTests(unittest.TestCase):
    def test_messages_split_below_discord_limit(self):
        findings = [("CRITICAL", f"New plugin `plugin-{i:03}` ({'x' * 60})", "ls -la '/x'") for i in range(80)]
        messages = watcher.format_messages("example.org", "CRITICAL", findings)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 2000 for m in messages))

    def test_names_sanitised_and_mentions_disabled(self):
        self.assertEqual(watcher.clean("a`b\nc"), "a'b?c")
        self.assertEqual(watcher.allowed_mentions(None, "@everyone x"), {"parse": []})
        self.assertEqual(watcher.allowed_mentions("<@123456789>", "<@123456789> CRITICAL"), {"parse": [], "users": ["123456789"]})

    def test_mention_only_on_critical(self):
        self.assertTrue(watcher.format_messages("s", "CRITICAL", [("CRITICAL", "x", None)], "@here")[0].startswith("@here"))
        self.assertFalse(watcher.format_messages("s", "WARNING", [("WARNING", "x", None)], "@here")[0].startswith("@here"))

    def test_failed_delivery_is_spooled_and_resent_as_delayed(self):
        with tempfile.TemporaryDirectory() as directory:
            spool, sent = watcher.Spool(Path(directory) / "spool"), []
            watcher.deliver_all(WATCH, ["one", "two"], spool, send=lambda *a: False, pause=0)
            self.assertEqual(len(spool.items()), 2)
            watcher.deliver_all(WATCH, ["three"], spool, send=lambda url, content, mention: sent.append(content) or True, pause=0)
            self.assertEqual(len(spool.items()), 0)
            self.assertTrue(sent[0].startswith("(delayed, raised ") and sent[0].endswith("one"))
            self.assertEqual(sent[-1], "three")

    def test_spool_keeps_new_messages_while_discord_is_down(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = watcher.Spool(Path(directory) / "spool")
            spool.add("old")
            watcher.deliver_all(WATCH, ["new"], spool, send=lambda *a: False, pause=0)
            self.assertEqual(len(spool.items()), 2)

    def test_send_discord_posts_json_without_mentions(self):
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        self.assertTrue(watcher.send_discord(WEBHOOK, "@everyone hi", opener=opener))
        body = json.loads(opener.request.data)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(opener.request.get_method(), "POST")


class ConfigTests(unittest.TestCase):
    def test_watch_config_validated(self):
        self.assertEqual(watcher.validate_watch_config({"webhook_url": WEBHOOK})["digest_hour"], 8)
        for bad in ({"webhook_url": "https://example.org/hook"}, {"webhook_url": WEBHOOK, "mention": "@all"},
                    {"webhook_url": WEBHOOK, "ignore": ["plugins/*"]}, {"webhook_url": WEBHOOK, "ignore": ["uploads/*"]},
                    {"webhook_url": WEBHOOK, "ignore": ["uploads/../plugins"]}, {"webhook_url": WEBHOOK, "digest_hour": 24}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                watcher.validate_watch_config(bad)

    def test_corrupt_state_starts_fresh_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "state.json").write_text("{broken")
            state, problem = watcher.load_state(directory)
            self.assertEqual(state, watcher.empty_state())
            self.assertEqual(problem[0], "WARNING")
            self.assertTrue(list(Path(directory).glob("state.json.corrupt-*")))

    def test_state_round_trip_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            state = watcher.empty_state()
            state["quiet"]["x"] = 1
            watcher.save_state(directory, state)
            self.assertEqual(watcher.load_state(directory)[0]["quiet"], {"x": 1})
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["state.json"])
