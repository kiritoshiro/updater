from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from sitewatch import collector
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

    @patch("sitewatch.transport.subprocess.run")
    def test_ssh_error_does_not_disclose_remote_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, b"", b"DB_PASSWORD=secret")
        with self.assertRaises(ValueError) as error:
            fetch_ssh({"host": "example.org", "user": "darius"})
        self.assertNotIn("secret", str(error.exception))


class CollectorTests(unittest.TestCase):
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

    def test_no_remote_update_endpoint(self):
        headers = {"Origin": self.origin, "X-Site-Watch-Token": self.state.token}
        self.assertEqual(self.request("POST", "/api/update", headers)[0], 404)

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
