"""Linux collector. Root orchestrates; WordPress runs only as its Unix owner.

This module does not install, update, deactivate or edit WordPress components.
WP-CLI update checks may refresh WordPress's update transients.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .model import DOMAIN, SLUG, numeric_version, utcnow, validate_status


class CheckFailed(Exception):
    """Public, fixed text only. Never include command output or configuration."""


def run(argv, timeout=65):
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              check=False, env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"}, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckFailed("Command unavailable or timed out") from exc
    if proc.returncode or len(proc.stdout) > 4 * 1024 * 1024:
        raise CheckFailed("Command failed or returned oversized output")
    return proc.stdout.strip()


def safe_text(value, version=False):
    value = str(value or "")
    pattern = r"[a-zA-Z0-9_.+ -]{1,80}" if version else r"[a-zA-Z0-9_.@-]{1,200}"
    return value if re.fullmatch(pattern, value) else None


def load_config(path):
    path = Path(path).resolve(strict=True)
    # Privileged configuration and its parents must not be replaceable by a site user.
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Collector config and parent directories must be root-owned and not group/world writable")
    if path.stat().st_mode & 0o077:
        raise ValueError("Collector config must have mode 0600")
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


def validate_config(config):
    if not isinstance(config.get("sites"), list) or not config["sites"]:
        raise ValueError("Configure at least one site")
    seen = set()
    for site in config["sites"]:
        domain = site.get("id", "")
        if not isinstance(domain, str) or not DOMAIN.fullmatch(domain) or ".." in domain or domain in seen:
            raise ValueError("Site IDs must be unique hostnames")
        seen.add(domain)
        # No default: nginx may not listen on loopback, and a wrong guess reports every site down.
        origin = site.get("origin_ip", config.get("origin_ip"))
        if not isinstance(origin, str):
            raise ValueError("Set origin_ip to the address nginx listens on for HTTPS")
        ipaddress.ip_address(origin)
        site["origin_ip"] = origin
        if site.get("kind", "wordpress") not in ("wordpress", "web"):
            raise ValueError("Site kind must be wordpress or web")
        for check_path in site.get("health_paths", ["/", "/wp-login.php"]):
            if not isinstance(check_path, str) or not check_path.startswith("/") or any(c in check_path for c in "\r\n?#"):
                raise ValueError("Invalid origin health path")
        if site.get("kind", "wordpress") == "wordpress":
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", site.get("owner", "")):
                raise ValueError("Set a valid non-root site owner")
            if not Path(site.get("path", "")).is_absolute():
                raise ValueError("Site paths must be absolute")
        for key, repository in site.get("github", {}).items():
            if not re.fullmatch(r"(?:plugin|theme):[a-zA-Z0-9_.-]+", key) or not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repository):
                raise ValueError("Invalid GitHub slug-to-repository mapping")
    return config


def validate_owner(site):
    import pwd
    try:
        account = pwd.getpwnam(site["owner"])
        path = Path(site["path"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise CheckFailed("Site owner or path unavailable") from exc
    if account.pw_uid == 0 or path.stat().st_uid != account.pw_uid:
        raise CheckFailed("Site path must belong to its configured non-root owner")


def wp(site, *args):
    validate_owner(site)
    return run(["/usr/bin/sudo", "-n", "-H", "-u", site["owner"], "--",
                "/usr/bin/timeout", "--kill-after=5", "60", "/usr/bin/env",
                "WP_CLI_CONFIG_PATH=/dev/null", "WP_CLI_PACKAGES_DIR=/nonexistent",
                "/usr/local/bin/wp", f"--path={site['path']}", "--skip-plugins",
                "--skip-themes", "--skip-packages", "--no-color", *args])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def github_version(repository):
    # Public releases only in v0.1. No token is needed or accepted in viewer config.
    request = Request(f"https://api.github.com/repos/{repository}/releases/latest",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "wp-site-watch/0.1"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("Oversized response")
        release = json.loads(raw)
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("Not a stable release")
        tag = release["tag_name"]
        if numeric_version(tag) is None:
            raise ValueError("Non-numeric release")
        return str(tag).removeprefix("v")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("GitHub release check unavailable") from exc


def inventory(rows, kind, mapping, releases, errors):
    if not isinstance(rows, list):
        raise CheckFailed("Invalid component inventory")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not SLUG.fullmatch(str(row.get("name", ""))):
            errors.append(f"Invalid {kind} row omitted")
            continue
        name = row["name"]
        item = {"name": name, "version": safe_text(row.get("version"), version=True),
                "status": safe_text(row.get("status")) or "unknown",
                "update_version": safe_text(row.get("update_version"), version=True),
                "source": "wordpress.org"}
        repository = mapping.get(f"{kind}:{name}")
        if repository:
            item.update(source="github", repository=repository, update_version=None)
            if repository not in releases:
                try:
                    releases[repository] = github_version(repository)
                except CheckFailed:
                    releases[repository] = None
            latest = releases[repository]
            current = numeric_version(item["version"])
            if latest is None or current is None:
                item["check_state"] = "unknown"
                errors.append(f"Release comparison unavailable: {name}")
            else:
                item["check_state"] = "checked"
                if numeric_version(latest) > current:
                    item["update_version"] = latest
        else:
            # Skipped third-party updater code cannot report private/vendor updates.
            item["check_state"] = "reported" if item["update_version"] else "not_reported"
        result.append(item)
    return result


# WordPress's fatal-error screen in the languages our sites use. Pages that merely mention the
# words (posts, search results) are not errors, so the wp_die() page marker is also required.
CRITICAL_ERROR_MESSAGES = ("there has been a critical error on this website", "tinklalapyje įvyko kritinė klaida")


def critical_error_page(content):
    content = content.lower()
    return 'id="error-page"' in content and any(text in content for text in CRITICAL_ERROR_MESSAGES)


def origin_health(domain, origin, path):
    address = f"[{origin}]" if ":" in origin else origin
    try:
        with tempfile.TemporaryDirectory(prefix="site-watch-health-") as directory:
            body = Path(directory) / "body"
            output = run(["/usr/bin/curl", "--silent", "--show-error", "--noproxy", "*",
                          "--connect-timeout", "5", "--max-time", "12", "--max-filesize", "2097152",
                          "--proto", "=https", "--resolve", f"{domain}:443:{address}",
                          "--header", "Cache-Control: no-cache", "--output", str(body),
                          "--write-out", "%{http_code} %{time_total}", f"https://{domain}{path}"], timeout=15)
            code, elapsed = output.split()
            content = body.read_bytes()[:2097152].decode("utf-8", errors="replace").lower()
        critical = critical_error_page(content)
        return {"path": path, "status": int(code), "response_ms": round(float(elapsed) * 1000),
                "ok": 200 <= int(code) < 400 and not critical, "critical_error": critical}
    except (CheckFailed, OSError, ValueError):
        return {"path": path, "status": None, "response_ms": None, "ok": False, "error": "Origin check failed (connection, TLS or response limit)"}


def backup_info(site):
    directory = site.get("backup_directory")
    if not directory:
        return {"state": "not_configured"}
    try:
        files = [p for p in Path(directory).iterdir() if p.is_file() and not p.is_symlink()]
        newest = max(files, key=lambda p: p.stat().st_mtime)
        info = newest.stat()
        return {"state": "observed", "last_modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                "size_bytes": info.st_size, "verified": False}
    except (OSError, ValueError):
        return {"state": "unknown"}


def collect_site(site, config, releases):
    result = {"id": site["id"], "kind": site.get("kind", "wordpress"), "state": "ok", "core": None,
              "plugins": [], "themes": [], "flags": {}, "errors": [], "notes": [], "health": [],
              "backup": backup_info(site)}
    errors = result["errors"]
    if result["kind"] == "wordpress":
        try:
            version = wp(site, "core", "version")
            result["core"] = {"version": safe_text(version, version=True), "update_version": None, "check_state": "unknown"}
        except CheckFailed:
            errors.append("WordPress version unavailable")
        try:
            rows = json.loads(wp(site, "core", "check-update", "--force-check", "--format=json", "--fields=version,update_type"))
            if not isinstance(rows, list):
                raise ValueError("Invalid core update response")
            versions = [r["version"] for r in rows if isinstance(r, dict) and numeric_version(r.get("version"))]
            if result["core"]:
                current = numeric_version(result["core"]["version"])
                newer = [v for v in versions if current and numeric_version(v) > current]
                result["core"].update(check_state="checked", update_version=max(newer, key=numeric_version) if newer else None)
        except (CheckFailed, ValueError, TypeError, KeyError):
            errors.append("Core update check unavailable")
        for kind, key in (("plugin", "plugins"), ("theme", "themes")):
            try:
                rows = json.loads(wp(site, kind, "list", "--format=json", "--fields=name,status,version,update_version"))
                result[key] = inventory(rows, kind, site.get("github", {}), releases, errors)
            except (CheckFailed, ValueError, TypeError):
                errors.append(f"{kind.capitalize()} inventory unavailable")
        for flag in ("DISALLOW_FILE_MODS", "DISALLOW_FILE_EDIT", "WP_DEBUG"):
            try:
                value = wp(site, "config", "get", flag, "--type=constant").lower()
                result["flags"][flag] = {"true": True, "1": True, "false": False, "0": False, "": False}.get(value)
            except CheckFailed:
                result["flags"][flag] = None
        result["notes"].append("WP-CLI skips regular plugins and themes; must-use plugins and drop-ins can still load, as the site owner.")
        result["notes"].append("No reported update does not verify currency for vendor plugins without a configured GitHub mapping.")
    paths = site.get("health_paths", ["/", "/wp-login.php"] if result["kind"] == "wordpress" else ["/"])
    for path in paths:
        result["health"].append(origin_health(site["id"], site["origin_ip"], path))
    if any(not h["ok"] for h in result["health"]):
        result["state"] = "unhealthy"
    elif errors:
        result["state"] = "partial"
    return result


def server_info(config):
    disk = shutil.disk_usage("/")
    result = {"id": safe_text(config.get("server_id", "server")), "hostname": safe_text(socket.gethostname()),
              "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
              "memory": None, "load": list(os.getloadavg()), "php_version": None, "failed_units": None, "errors": []}
    try:
        info = dict((key, int(value.strip().split()[0]) * 1024) for key, value in
                    (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()))
        result["memory"] = {"total_bytes": info["MemTotal"], "available_bytes": info["MemAvailable"]}
    except (OSError, ValueError, KeyError):
        result["errors"].append("Memory data unavailable")
    try:
        # -n avoids loading site-controlled PHP configuration and extensions.
        result["php_version"] = safe_text(run(["/usr/bin/php", "-n", "-r", "echo PHP_VERSION;"], timeout=10), version=True)
    except CheckFailed:
        result["errors"].append("PHP version unavailable")
    try:
        units = run(["/usr/bin/systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
        result["failed_units"] = [safe_text(line.split()[0]) for line in units.splitlines() if line.strip()]
    except CheckFailed:
        result["errors"].append("Service status unavailable")
    return result


def collect(config):
    releases = {}
    sites = []
    for site in config["sites"]:
        try:
            sites.append(collect_site(site, config, releases))
        except Exception:
            # Isolate unexpected per-site failures; no raw output in the public snapshot.
            sites.append({"id": site["id"], "kind": site.get("kind", "wordpress"), "state": "unreachable",
                          "core": None, "plugins": [], "themes": [], "health": [], "flags": {},
                          "backup": {"state": "unknown"}, "errors": ["Collection failed for this site"], "notes": []})
    return validate_status({"schema_version": 1, "collected_at": utcnow(), "demo": False,
                            "server": server_info(config), "sites": sites})


@contextmanager
def collector_lock(directory):
    import fcntl
    with (directory / ".collector.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another collector is running") from exc
        yield


def write_status(data, directory):
    import grp
    group = grp.getgrnam("sitewatch").gr_gid
    fd, name = tempfile.mkstemp(prefix=".status-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, group)
            json.dump(data, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / "status.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description="Read-only Linux WordPress fleet collector")
    parser.add_argument("--config", type=Path, default=Path("/etc/site-watch/collector.json"))
    parser.add_argument("--dry-run", action="store_true", help="Print JSON; do not write state")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("The collector must run as root on Linux; WordPress runs as each site owner")
    try:
        config = load_config(args.config)
        directory = Path("/var/lib/site-watch")
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("State directory must be a real root-owned directory without group/world write access")
        with collector_lock(directory):
            data = collect(config)
            if args.dry_run:
                print(json.dumps(data, indent=2))
            else:
                write_status(data, directory)
                print(f"Collected {len(data['sites'])} sites at {data['collected_at']}")
                for site in data["sites"]:
                    print(f"  {site['id']}: {site['state']}")
    except (ValueError, OSError, KeyError):
        parser.exit(1, "Collector setup failed. Check root-owned config, sitewatch group and state directory.\n")


if __name__ == "__main__":
    main()
