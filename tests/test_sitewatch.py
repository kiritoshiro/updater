from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from sitewatch import collector, transport
from sitewatch.demo import sample
from sitewatch.model import decode_status, freshness, MAX_STATUS_BYTES, numeric_version
from sitewatch.transport import ssh_command, fetch_ssh, fetch_file
from sitewatch.viewer import State, make_server


class ContractTests(unittest.TestCase):
    def test_demo_round_trip(self):
        self.assertTrue(decode_status(json.dumps(sample()).encode())["demo"])

    def test_stale_and_future_snapshots(self):
        data = sample()
        self.assertFalse(freshness(data)["stale"])
        data["collected_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.assertTrue(freshness(data)["stale"])
        data["collected_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertTrue(freshness(data)["clock_skew"])

    def test_malformed_status_rejected(self):
        for data in ({}, {"schema_version": 9}, {"schema_version": 1, "collected_at": "not a date"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_status(json.dumps(data).encode())

    def test_duplicate_sites_rejected(self):
        data = sample(); data["sites"].append(data["sites"][0])
        with self.assertRaises(ValueError):
            decode_status(json.dumps(data).encode())

    def test_invalid_nested_data_rejected(self):
        for key, value in (("plugins", ["broken"]), ("core", "broken"), ("flags", {"WP_DEBUG": "true"}), ("health", [{"ok": "yes", "path": "/"}])):
            data = sample(); data["sites"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                decode_status(json.dumps(data).encode())

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            decode_status(b" " * (MAX_STATUS_BYTES + 1))

    def test_file_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(sample()))
            self.assertEqual(len(fetch_file(path)["sites"]), 7)

    def test_versions_are_numeric_not_lexical(self):
        self.assertGreater(numeric_version("v2.10.0"), numeric_version("2.9"))
        self.assertEqual(numeric_version("2.4"), numeric_version("2.4.0"))
        self.assertIsNone(numeric_version("2.4-beta"))


class TransportTests(unittest.TestCase):
    def test_only_fixed_remote_command(self):
        command = ssh_command({"host": "example.org", "user": "darius", "port": 5567})
        self.assertEqual(command[-1], "cat /var/lib/site-watch/status.json")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)

    def test_injection_and_invalid_port_rejected(self):
        valid = {"host": "example.org", "user": "darius", "port": 22}
        for key, value in [("host", "-oProxyCommand=bad"), ("host", "x;id"), ("user", "root;id"), ("user", "../root"), ("port", "22;id"), ("port", True), ("port", 65536)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                ssh_command({**valid, key: value})

    def test_windows_prefers_system_openssh(self):
        with tempfile.TemporaryDirectory() as root:
            os_ = __import__("os")
            client = os_.path.join(root, "System32", "OpenSSH", "ssh.exe")
            with patch("sitewatch.transport.os.name", "nt"), patch.dict("os.environ", {"SystemRoot": root}):
                self.assertEqual(transport.ssh_executable(), "ssh")
                os_.makedirs(os_.path.dirname(client))
                open(client, "wb").close()
                self.assertEqual(transport.ssh_executable(), client)
                self.assertEqual(ssh_command({"host": "example.org", "user": "darius"})[0], client)

    @patch("sitewatch.transport.os.name", "posix")
    def test_other_systems_use_ssh_from_path(self):
        self.assertEqual(transport.ssh_executable(), "ssh")

    @patch("sitewatch.transport.subprocess.run")
    def test_ssh_error_does_not_disclose_remote_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, b"", b"DB_PASSWORD=secret")
        with self.assertRaises(ValueError) as error:
            fetch_ssh({"host": "example.org", "user": "darius"})
        self.assertNotIn("secret", str(error.exception))

    def test_update_command_is_fixed_and_validated(self):
        config = {"host": "example.org", "user": "darius", "port": 5567}
        self.assertEqual(transport.update_command(config, "example.org", "plugin", "classic-editor")[-1],
                         "sudo -n /usr/local/sbin/site-update --json example.org plugin classic-editor")
        self.assertEqual(transport.update_command(config, "example.org", "core")[-1],
                         "sudo -n /usr/local/sbin/site-update --json example.org core")
        for args in (("example.org", "plugin", "x;id"), ("example.org", "plugin", "$(id)"), ("example.org", "plugin", "-x"),
                     ("example.org", "plugin", None), ("example.org", "core", "x"), ("example.org", "all", None),
                     ("a b", "core", None), ("example.org;id", "core", None)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                transport.update_command(config, *args)

    @patch("sitewatch.transport.subprocess.run")
    def test_update_reply_is_cleaned(self, run):
        config = {"host": "example.org", "user": "darius"}
        reply = {"results": [{"site": "example.org", "kind": "plugin", "slug": "x", "result": "rolled_back",
                              "message": "m" * 1000, "backup": "/var/backups/x", "extra": "<script>"}]}
        run.return_value = subprocess.CompletedProcess([], 3, ("Waiting…\n" + json.dumps(reply)).encode(), b"")
        [result] = transport.run_update(config, "example.org", "plugin", "x")
        self.assertEqual((result["result"], len(result["message"])), ("rolled_back", 300))
        self.assertNotIn("extra", result)
        run.return_value = subprocess.CompletedProcess([], 1, b"", b"sudo: a password is required")
        with self.assertRaises(ValueError) as error:
            transport.run_update(config, "example.org", "plugin", "x")
        self.assertIn("sudoers", str(error.exception))
        run.return_value = subprocess.CompletedProcess([], 1, b'{"error": "Another update is still running; try again later"}', b"")
        with self.assertRaises(ValueError):
            transport.run_update(config, "example.org", "plugin", "x")
        state = (b'{"result": "updated"}\nnot json\n{"result": "bad"}\n' + transport.NIGHTLY_MARK.encode()
                 + b'\n{"sites": {"example.org": {"enabled": true, "by": "darius"}, "bad site": {"enabled": true},'
                 + b' "other.org": {"enabled": "yes"}}}\n')
        run.return_value = subprocess.CompletedProcess([], 0, state, b"")
        history, nightly = transport.fetch_server_state(config)
        self.assertEqual(len(history), 1)
        self.assertEqual(nightly, {"example.org": {"enabled": True, "by": "darius"}})
        run.return_value = subprocess.CompletedProcess([], 0, transport.NIGHTLY_MARK.encode() + b"\n", b"")
        self.assertIsNone(transport.fetch_server_state(config)[1])  # unreadable switch file: unknown, not off
        run.return_value = subprocess.CompletedProcess([], 0, transport.NIGHTLY_MARK.encode() + b"\n{}\n", b"")
        self.assertEqual(transport.fetch_server_state(config)[1], {})

    def test_rollback_choice_and_nightly_commands(self):
        config = {"host": "example.org", "user": "darius", "port": 5567}
        self.assertEqual(transport.update_command(config, "example.org", "plugin", "x", restore_database=False)[-1],
                         "sudo -n /usr/local/sbin/site-update --json --files-only-rollback example.org plugin x")
        self.assertEqual(transport.nightly_command(config, "example.org", True)[-1],
                         "sudo -n /usr/local/sbin/site-update --json example.org nightly on")
        for args in (("example.org", "yes"), ("example.org;id", True), ("example.org", 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                transport.nightly_command(config, *args)
        with self.assertRaises(ValueError):
            transport.update_command(config, "example.org", "core", None, "false")


class CollectorTests(unittest.TestCase):
    @patch("sitewatch.collector.origin_health", return_value={"path": "/", "ok": True, "status": 200})
    @patch("sitewatch.collector.security_flags", return_value={})
    @patch("sitewatch.collector.wp")
    def test_core_reports_the_installed_branch_release(self, wp, *_):
        def answer(site, *args):
            if args[:2] == ("core", "version"):
                return "6.8.2"
            if args[:2] == ("core", "check-update"):
                return json.dumps([{"version": "7.0.1", "update_type": "major"}, {"version": "6.8.3", "update_type": "minor"}])
            return "[]"
        wp.side_effect = answer
        core = collector.collect_site({"id": "example.org", "kind": "wordpress", "origin_ip": "203.0.113.10"}, {}, {})["core"]
        self.assertEqual((core["update_version"], core["minor_update_version"]), ("7.0.1", "6.8.3"))

    @patch("sitewatch.collector.run", return_value="6.8.2")
    @patch("sitewatch.collector.validate_owner")
    def test_wp_always_drops_privileges(self, validate, run):
        collector.wp({"owner": "example", "path": "/home/example/public_html"}, "core", "version")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:7], ["/usr/bin/sudo", "-n", "-H", "-u", "example", "--", "/usr/bin/timeout"])
        self.assertIn("--skip-plugins", cmd)
        self.assertIn("--skip-themes", cmd)
        self.assertIn("--skip-packages", cmd)
        self.assertNotIn("--allow-root", cmd)

    @unittest.skipUnless(__import__('os').name == 'posix', "Unix ownership semantics")
    def test_root_site_owner_refused(self):
        with self.assertRaises(collector.CheckFailed):
            collector.validate_owner({"owner": "root", "path": "/"})

    @patch("sitewatch.collector.github_version", return_value="2.10.0")
    def test_github_projection_and_cache(self, release):
        rows = [{"name": "my-plugin", "version": "2.9.0", "status": "active", "DB_PASSWORD": "secret", "update_package": "https://example.org?token=secret"}]
        releases, errors = {}, []
        first = collector.inventory(rows, "plugin", {"plugin:my-plugin": "example/repo"}, releases, errors)
        collector.inventory(rows, "plugin", {"plugin:my-plugin": "example/repo"}, releases, errors)
        self.assertEqual(first[0]["update_version"], "2.10.0")
        release.assert_called_once()
        self.assertNotIn("secret", json.dumps(first))
        self.assertNotIn("DB_PASSWORD", json.dumps(first))

    @patch("sitewatch.collector.github_version", side_effect=collector.CheckFailed("unavailable"))
    def test_github_failure_is_unknown_not_current(self, _):
        errors = []
        rows = collector.inventory([{"name": "custom", "version": "1.0"}], "plugin", {"plugin:custom": "example/repo"}, {}, errors)
        self.assertEqual(rows[0]["check_state"], "unknown")
        self.assertTrue(errors)

    @patch("sitewatch.collector.server_info", return_value={"id": "test"})
    @patch("sitewatch.collector.collect_site")
    def test_broken_site_does_not_abort_fleet(self, collect_site, _):
        good = sample()["sites"][0]
        collect_site.side_effect = [RuntimeError("DB_PASSWORD=secret"), good]
        data = collector.collect({"sites": [{"id": "broken.example.org"}, {"id": good["id"]}]})
        self.assertEqual(data["sites"][0]["state"], "unreachable")
        self.assertEqual(data["sites"][1]["id"], good["id"])
        self.assertNotIn("secret", json.dumps(data))

    @patch("sitewatch.collector.wp", side_effect=collector.CheckFailed("DB_PASSWORD=secret"))
    @patch("sitewatch.collector.origin_health", return_value={"path": "/", "ok": True, "status": 200})
    def test_fatal_wordpress_is_partial_and_keeps_origin(self, *_):
        data = collector.collect_site({"id": "example.org", "owner": "example", "path": "/home/example/public_html", "origin_ip": "203.0.113.10"}, {}, {})
        self.assertEqual(data["state"], "partial")
        self.assertIsNone(data["core"])
        self.assertTrue(data["health"])
        self.assertNotIn("secret", json.dumps(data))

    @patch("sitewatch.collector.run", side_effect=collector.CheckFailed("TLS failure"))
    def test_failed_origin_not_healthy(self, _):
        self.assertFalse(collector.origin_health("example.org", "127.0.0.1", "/")["ok"])

    def test_critical_error_needs_wordpress_error_page(self):
        english = '<body id="error-page"><div class="wp-die-message"><p>There has been a critical error on this website.</p>'
        lithuanian = '<body id="error-page"><div class="wp-die-message"><p>Tinklalapyje įvyko kritinė klaida.</p>'
        self.assertTrue(collector.critical_error_page(english))
        self.assertTrue(collector.critical_error_page(lithuanian))
        for page in ('<body class="post"><p>How we fixed a critical error on this website.</p>',
                     '<body><p>There has been a critical error on this website.</p>',
                     '<body id="error-page"><p>Database connection failed.</p>'):
            with self.subTest(page=page):
                self.assertFalse(collector.critical_error_page(page))

    def test_origin_ip_required(self):
        site = {"id": "example.org", "kind": "web"}
        with self.assertRaises(ValueError):
            collector.validate_config({"sites": [dict(site)]})
        with self.assertRaises(ValueError):
            collector.validate_config({"origin_ip": "not-an-ip", "sites": [dict(site)]})
        config = collector.validate_config({"origin_ip": "203.0.113.10", "sites": [dict(site), {**site, "id": "b.example.org", "origin_ip": "198.51.100.7"}]})
        self.assertEqual([s["origin_ip"] for s in config["sites"]], ["203.0.113.10", "198.51.100.7"])

    def test_ca_file_inherited_overridable_and_validated(self):
        site = {"id": "example.org", "kind": "web"}
        config = collector.validate_config({"origin_ip": "203.0.113.10", "ca_file": "/etc/site-watch/ca.pem",
                                            "sites": [dict(site), {**site, "id": "le.example.org", "ca_file": None}]})
        self.assertEqual([s["ca_file"] for s in config["sites"]], ["/etc/site-watch/ca.pem", None])
        for bad in ("relative.pem", "/etc/../tmp/ca.pem", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                collector.validate_config({"origin_ip": "203.0.113.10", "ca_file": bad, "sites": [dict(site)]})

    @patch("sitewatch.collector.run", return_value="200 0.05")
    def test_origin_check_passes_ca_file_and_keeps_verification(self, run):
        collector.origin_health("example.org", "203.0.113.10", "/", "/etc/site-watch/ca.pem")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--cacert") + 1], "/etc/site-watch/ca.pem")
        self.assertNotIn("--insecure", cmd)
        self.assertNotIn("-k", cmd)
        collector.origin_health("example.org", "203.0.113.10", "/")
        self.assertNotIn("--cacert", run.call_args.args[0])

    def test_component_names_with_spaces_kept(self):
        errors = []
        rows = collector.inventory([{"name": "old alps-wordpress-v3 v2", "version": "3.14.3.0", "status": "inactive"},
                                    {"name": "../etc", "version": "1"}, {"name": "bad\nname"}], "theme", {}, {}, errors)
        self.assertEqual([r["name"] for r in rows], ["old alps-wordpress-v3 v2"])
        self.assertEqual(len(errors), 2)

    @patch("sitewatch.collector.wp")
    def test_undefined_constants_are_false_not_unknown(self, wp):
        def fake(site, *args):
            if args[:2] == ("config", "list"):
                return json.dumps([{"name": "DB_NAME", "type": "constant"}, {"name": "WP_DEBUG", "type": "constant"},
                                   {"name": "table_prefix", "type": "variable"}])
            return "1"
        wp.side_effect = fake
        self.assertEqual(collector.security_flags({}), {"DISALLOW_FILE_MODS": False, "DISALLOW_FILE_EDIT": False, "WP_DEBUG": True})
        self.assertNotIn("--fields=name,value,type", json.dumps([c.args for c in wp.call_args_list]))

    @patch("sitewatch.collector.wp", side_effect=collector.CheckFailed("unreadable"))
    def test_unreadable_config_flags_unknown(self, _):
        self.assertEqual(set(collector.security_flags({}).values()), {None})

    def test_github_mappings_inherit_and_exclude(self):
        config = collector.validate_config({
            "origin_ip": "203.0.113.10", "github": {"theme:adventistai": "kiritoshiro/Adventistai-ALPS", "plugin:alps-gutenberg-blocks": "kiritoshiro/blocks"},
            "sites": [{"id": "a.example.org", "kind": "web"},
                      {"id": "b.example.org", "kind": "web", "github": {"plugin:alps-gutenberg-blocks": None, "plugin:extra": "kiritoshiro/extra"}}]})
        self.assertEqual(config["sites"][0]["github"], {"theme:adventistai": "kiritoshiro/Adventistai-ALPS", "plugin:alps-gutenberg-blocks": "kiritoshiro/blocks"})
        self.assertEqual(config["sites"][1]["github"], {"theme:adventistai": "kiritoshiro/Adventistai-ALPS", "plugin:extra": "kiritoshiro/extra"})
        for bad in ({"plugin:x": "no-slash"}, {"bad key": "a/b"}, ["a/b"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                collector.validate_config({"origin_ip": "203.0.113.10", "github": bad, "sites": [{"id": "a.example.org", "kind": "web"}]})

    @patch("sitewatch.collector.fetch_json", return_value={"version": "8.4.27", "tags": ["security"], "supported_versions": ["8.3", "8.4", "8.5"]})
    def test_php_release_compares_same_branch(self, fetch):
        self.assertEqual(collector.php_release("8.4.26"), {"branch": "8.4", "latest": "8.4.27", "update_available": True,
                                                           "branch_supported": True, "security_release": True})
        self.assertIn("version=8.4", fetch.call_args.args[0])
        self.assertFalse(collector.php_release("8.4.27")["update_available"])

    @patch("sitewatch.collector.fetch_json", return_value={"version": "8.5.12"})
    def test_php_release_rejects_other_branch(self, _):
        with self.assertRaises(collector.CheckFailed):
            collector.php_release("8.4.26")

    @patch("sitewatch.collector.fetch_json", side_effect=OSError("offline"))
    def test_php_release_unavailable(self, _):
        with self.assertRaises(collector.CheckFailed):
            collector.php_release("8.4.26")

    @patch("sitewatch.collector.run", return_value="Listing...\n"
           "php8.4-fpm/noble 8.4.27-1+ubuntu24.04.1+deb.sury.org+1 amd64 [upgradable from: 8.4.26-1+ubuntu24.04.1+deb.sury.org+1]\n"
           "libssl3t64/noble-updates,noble-security 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13-0ubuntu3.5]\n"
           "evil;rm/noble 1 amd64 [upgradable from: 0]\n")
    def test_apt_status_parses_upgradable(self, run):
        status = collector.apt_status()
        self.assertEqual(run.call_args.args[0], ["/usr/bin/apt", "list", "--upgradable"])
        self.assertEqual(status["upgradable"], 2)
        self.assertEqual(status["security"], 1)
        self.assertEqual(status["packages"][0], {"name": "php8.4-fpm", "version": "8.4.27-1+ubuntu24.04.1+deb.sury.org+1",
                                                 "from": "8.4.26-1+ubuntu24.04.1+deb.sury.org+1", "security": False})
        self.assertIn("lists_refreshed_at", status)

    def test_ignore_failed_units_validated(self):
        base = {"origin_ip": "203.0.113.10", "sites": [{"id": "example.org", "kind": "web"}]}
        collector.validate_config({**base, "ignore_failed_units": ["user@*.service"]})
        for bad in ("user@*.service", ["a b"], [5]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                collector.validate_config({**base, "ignore_failed_units": bad})

    @unittest.skipUnless(hasattr(__import__('os'), "getloadavg"), "Unix load average")
    @patch("sitewatch.collector.php_release", return_value={"branch": "8.4", "latest": "8.4.26", "update_available": False})
    @patch("sitewatch.collector.run")
    def test_ignored_failed_units_filtered(self, run, _):
        run.side_effect = lambda argv, timeout=65: ("8.4.26" if "php" in argv[0] else
                                                   "user@1001.service loaded failed failed User Manager\nnginx.service loaded failed failed nginx")
        info = collector.server_info({"ignore_failed_units": ["user@*.service"]})
        self.assertEqual(info["failed_units"], ["nginx.service"])


class FakeUpdates:
    def __init__(self, calls):
        self.calls = calls

    def run(self, site, kind, slug, restore_database=True):
        self.calls.append((site, kind, slug, restore_database))
        return [{"result": "updated"}]

    def server_state(self):
        return [], {}

    def set_nightly(self, site, enabled):
        self.calls.append(("nightly", site, enabled))
        return {"enabled": enabled, "by": "darius"}


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.state = State(sample, "Demo")
        self.state.refresh()
        self.server = make_server(self.state, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        code, body, hdrs = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return code, body, hdrs

    def test_dashboard_and_assets(self):
        for path in ("/", "/app.js", "/style.css", "/api/status"):
            code, body, headers = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request("GET", "/api/status", {"Host": "attacker.example"})[0], 403)

    def test_cross_origin_refresh_rejected(self):
        headers = {"Origin": "https://attacker.example", "X-Site-Watch-Token": self.state.token}
        self.assertEqual(self.request("POST", "/api/refresh", headers)[0], 403)

    def test_missing_token_rejected(self):
        self.assertEqual(self.request("POST", "/api/refresh", {"Origin": self.origin})[0], 403)

    def test_same_origin_refresh(self):
        headers = {"Origin": self.origin, "X-Site-Watch-Token": self.state.token}
        self.assertEqual(self.request("POST", "/api/refresh", headers)[0], 200)

    def post_update(self, body, origin=None, path="/api/update"):
        raw = json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("POST", path, raw, {"Origin": origin or self.origin, "X-Site-Watch-Token": self.state.token,
                                                         "Content-Type": "application/json", "Content-Length": str(len(raw))})
        response = connection.getresponse()
        code, reply = response.status, json.loads(response.read())
        connection.close()
        return code, reply

    def enable_updates(self):
        calls = []
        live = sample(); live["demo"] = False
        self.state.loader = lambda: live
        self.state.updates = FakeUpdates(calls)
        self.state.refresh()
        return calls

    def test_read_only_dashboard_refuses_updates(self):
        code, reply = self.post_update({"site": "adventistai.lt", "kind": "plugin", "slug": "wordfence"})
        self.assertEqual(code, 400)
        self.assertFalse(reply["capabilities"]["updates"])

    def test_demo_data_never_offers_updates(self):
        self.state.updates = FakeUpdates([])
        self.assertFalse(self.state.payload()["capabilities"]["updates"])

    def test_update_runs_only_reported_updates(self):
        calls = self.enable_updates()
        self.assertTrue(self.state.payload()["capabilities"]["updates"])
        self.assertEqual(self.post_update({"site": "adventistai.lt", "kind": "plugin", "slug": "wordfence"})[0], 200)
        self.assertEqual(self.post_update({"site": "dievozodis.lt", "kind": "core"})[0], 200)
        for body in ({"site": "sveikas.info", "kind": "plugin", "slug": "wordfence"}, {"site": "adventistai.lt", "kind": "core"},
                     {"site": "adventistai.lt", "kind": "plugin", "slug": "wordfence; id"}, {"site": "giesmynas.adventistai.lt", "kind": "core"},
                     {"site": "adventistai.lt", "kind": "all"}, {"site": "adventistai.lt", "kind": "plugin", "slug": "wordfence", "extra": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.post_update(body)[0], 400)
        self.assertEqual(self.post_update({"site": "malonetau.lt", "kind": "plugin", "slug": "wordfence", "restore_database": False})[0], 200)
        self.assertEqual(self.post_update({"site": "malonetau.lt", "kind": "plugin", "slug": "wordfence", "restore_database": "no"})[0], 400)
        self.assertEqual(calls, [("adventistai.lt", "plugin", "wordfence", True), ("dievozodis.lt", "core", None, True),
                                 ("malonetau.lt", "plugin", "wordfence", False)])

    def test_nightly_switch(self):
        calls = self.enable_updates()
        code, reply = self.post_update({"site": "sveikas.info", "enabled": True}, path="/api/nightly")
        self.assertEqual((code, reply["nightly"]["sveikas.info"]["enabled"]), (200, True))
        for body in ({"site": "giesmynas.adventistai.lt", "enabled": True}, {"site": "nosuch.example", "enabled": True},
                     {"site": "sveikas.info", "enabled": "on"}, {"site": "sveikas.info"}):
            with self.subTest(body=body):
                self.assertEqual(self.post_update(body, path="/api/nightly")[0], 400)
        self.assertEqual(calls, [("nightly", "sveikas.info", True)])
        self.assertEqual(self.post_update({"site": "sveikas.info", "enabled": False}, path="/api/nightly",
                                          origin="https://attacker.example")[0], 403)

    def test_update_without_snapshot_refused(self):
        self.enable_updates()
        self.state.data = None
        self.assertEqual(self.post_update({"site": "dievozodis.lt", "kind": "core"})[0], 400)

    def test_update_needs_same_origin_and_one_at_a_time(self):
        calls = self.enable_updates()
        self.assertEqual(self.post_update({"site": "dievozodis.lt", "kind": "core"}, origin="https://attacker.example")[0], 403)
        self.state.update_lock.acquire()
        try:
            self.assertEqual(self.post_update({"site": "dievozodis.lt", "kind": "core"})[0], 409)
        finally:
            self.state.update_lock.release()
        self.assertEqual(calls, [])

    def test_missing_and_stale_preserved_on_failure(self):
        previous = self.state.data
        self.state.loader = lambda: (_ for _ in ()).throw(ValueError("private secret"))
        self.state.refresh()
        self.assertIs(self.state.data, previous)
        self.assertNotIn("secret", json.dumps(self.state.payload()))
        self.assertTrue(self.state.payload()["error"])
        self.state.data = None
        self.assertIsNone(self.state.payload()["freshness"])

    def test_static_path_traversal_refused(self):
        self.assertEqual(self.request("GET", "/../config/collector.example.json")[0], 404)


if __name__ == "__main__":
    unittest.main()
