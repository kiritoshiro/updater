"""Linux collector. Root orchestrates; WordPress runs only as its Unix owner.

This module does not install, update, deactivate or edit WordPress components.
WP-CLI update checks may refresh WordPress's update transients.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
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

from .model import DOMAIN, numeric_version, utcnow, validate_status


class CheckFailed(Exception):
    """Public, fixed text only. Never include command output or configuration."""


ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"}


def run(argv, timeout=65):
    # Own session: Ctrl+C or a hangup at site-update's terminal must not kill a check halfway.
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              check=False, env=ENV, cwd="/", start_new_session=os.name == "posix")
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
    check_root_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Collector config must have mode 0600")
    config = validate_config(json.loads(path.read_text(encoding="utf-8")))
    for ca_file in {site["ca_file"] for site in config["sites"] if site["ca_file"]}:
        check_root_file(ca_file)
    return config


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
        # Extra CA for origin TLS, e.g. Cloudflare Origin CA. A site sets null to use the system store.
        ca_file = site["ca_file"] if "ca_file" in site else config.get("ca_file")
        if ca_file is not None and (not isinstance(ca_file, str) or not ca_file.startswith("/") or ".." in ca_file):
            raise ValueError("ca_file must be an absolute path or null")
        site["ca_file"] = ca_file
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
        # Top-level mappings apply to every site that has the slug installed; a site maps a slug
        # to null when its copy comes from elsewhere (e.g. an upstream build of the same plugin).
        if not isinstance(config.get("github", {}), dict) or not isinstance(site.get("github", {}), dict):
            raise ValueError("github must map plugin:/theme: slugs to owner/repository")
        mapping = {**config.get("github", {}), **site.get("github", {})}
        for key, repository in mapping.items():
            if not re.fullmatch(r"(?:plugin|theme):[a-zA-Z0-9_.-]+", key) or not (
                    repository is None or re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", str(repository))):
                raise ValueError("Invalid GitHub slug-to-repository mapping")
        site["github"] = {key: repository for key, repository in mapping.items() if repository is not None}
    patterns = config.get("ignore_failed_units", [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) and re.fullmatch(r"[a-zA-Z0-9@_.*?-]{1,100}", p) for p in patterns):
        raise ValueError("ignore_failed_units must be a list of unit name patterns")
    return config


def check_root_file(path):
    """A file root reads on the collector's behalf must not be replaceable by a site user."""
    path = Path(path).resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Files used by the collector must be root-owned and not group/world writable")


def validate_owner(site):
    import pwd
    try:
        account = pwd.getpwnam(site["owner"])
        path = Path(site["path"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise CheckFailed("Site owner or path unavailable") from exc
    if account.pw_uid == 0 or path.stat().st_uid != account.pw_uid:
        raise CheckFailed("Site path must belong to its configured non-root owner")


def as_owner(site, *argv, limit=60):
    return ["/usr/bin/sudo", "-n", "-H", "-u", site["owner"], "--",
            "/usr/bin/timeout", "--kill-after=5", str(limit), *argv]


def wp_argv(site, *args, limit=60):
    return as_owner(site, "/usr/bin/env", "WP_CLI_CONFIG_PATH=/dev/null", "WP_CLI_PACKAGES_DIR=/nonexistent",
                    "/usr/local/bin/wp", f"--path={site['path']}", "--skip-plugins",
                    "--skip-themes", "--skip-packages", "--no-color", *args, limit=limit)


def wp(site, *args):
    validate_owner(site)
    return run(wp_argv(site, *args))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_json(url, accept="application/json", limit=256 * 1024):
    """Small HTTPS JSON read: no redirects, bounded size and time."""
    request = Request(url, headers={"Accept": accept, "User-Agent": "wp-site-watch/0.1"})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Oversized response")
    return json.loads(raw)


def github_version(repository):
    # Public releases only in v0.1. No token is needed or accepted in viewer config.
    try:
        release = fetch_json(f"https://api.github.com/repos/{repository}/releases/latest", "application/vnd.github+json")
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("Not a stable release")
        tag = release["tag_name"]
        if numeric_version(tag) is None:
            raise ValueError("Non-numeric release")
        return str(tag).removeprefix("v")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("GitHub release check unavailable") from exc


# Installed folder names may contain spaces (e.g. a renamed "old theme"); still no control or
# path characters. Update actions must validate their own arguments against model.SLUG.
COMPONENT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_. -]{0,199}\Z", re.ASCII)


def inventory(rows, kind, mapping, releases, errors):
    if not isinstance(rows, list):
        raise CheckFailed("Invalid component inventory")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not COMPONENT_NAME.fullmatch(str(row.get("name", ""))):
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


def origin_health(domain, origin, path, ca_file=None):
    address = f"[{origin}]" if ":" in origin else origin
    trust = ["--cacert", ca_file] if ca_file else []
    try:
        with tempfile.TemporaryDirectory(prefix="site-watch-health-") as directory:
            body = Path(directory) / "body"
            output = run(["/usr/bin/curl", "--silent", "--show-error", "--noproxy", "*",
                          "--connect-timeout", "5", "--max-time", "12", "--max-filesize", "2097152",
                          "--proto", "=https", "--resolve", f"{domain}:443:{address}", *trust,
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


SECURITY_FLAGS = ("DISALLOW_FILE_MODS", "DISALLOW_FILE_EDIT", "WP_DEBUG")


def security_flags(site):
    """True/False per constant; a constant missing from wp-config.php is WordPress's default, False.
    None means wp-config.php could not be read. Only names are listed, never values of other keys."""
    try:
        rows = json.loads(wp(site, "config", "list", "--fields=name,type", "--format=json"))
        defined = {r["name"] for r in rows if isinstance(r, dict) and r.get("type") == "constant"}
    except (CheckFailed, ValueError, TypeError, KeyError):
        return {flag: None for flag in SECURITY_FLAGS}
    flags = {}
    for flag in SECURITY_FLAGS:
        if flag not in defined:
            flags[flag] = False
            continue
        try:
            value = wp(site, "config", "get", flag, "--type=constant").lower()
            flags[flag] = {"true": True, "1": True, "false": False, "0": False, "": False}.get(value)
        except CheckFailed:
            flags[flag] = None
    return flags


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
                # The same x.y branch gets security releases even after a new major version exists.
                branch = [v for v in newer if numeric_version(v)[:2] == current[:2]]
                result["core"].update(check_state="checked", update_version=max(newer, key=numeric_version) if newer else None,
                                      minor_update_version=max(branch, key=numeric_version) if branch else None)
        except (CheckFailed, ValueError, TypeError, KeyError):
            errors.append("Core update check unavailable")
        for kind, key in (("plugin", "plugins"), ("theme", "themes")):
            try:
                rows = json.loads(wp(site, kind, "list", "--format=json", "--fields=name,status,version,update_version"))
                result[key] = inventory(rows, kind, site.get("github", {}), releases, errors)
            except (CheckFailed, ValueError, TypeError):
                errors.append(f"{kind.capitalize()} inventory unavailable")
        result["flags"] = security_flags(site)
        result["notes"].append("WP-CLI skips regular plugins and themes; must-use plugins and drop-ins can still load, as the site owner.")
        result["notes"].append("No reported update does not verify currency for vendor plugins without a configured GitHub mapping.")
    paths = site.get("health_paths", ["/", "/wp-login.php"] if result["kind"] == "wordpress" else ["/"])
    for path in paths:
        result["health"].append(origin_health(site["id"], site["origin_ip"], path, site.get("ca_file")))
    if any(not h["ok"] for h in result["health"]):
        result["state"] = "unhealthy"
    elif errors:
        result["state"] = "partial"
    return result


def php_release(version):
    """Compare the installed PHP with php.net's latest release of the same branch."""
    current = numeric_version(version)
    if current is None:
        raise CheckFailed("PHP version unknown")
    branch = f"{current[0]}.{current[1]}"
    try:
        data = fetch_json(f"https://www.php.net/releases/index.php?json&version={branch}")
        latest = str(data["version"])
        if numeric_version(latest) is None or not latest.startswith(branch + "."):
            raise ValueError("Unexpected release data")
        supported = data.get("supported_versions")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("PHP release check unavailable") from exc
    return {"branch": branch, "latest": latest, "update_available": numeric_version(latest) > current,
            "branch_supported": branch in supported if isinstance(supported, list) else None,
            "security_release": "security" in data.get("tags", []) if isinstance(data.get("tags"), list) else None}


# `apt list --upgradable` line: name/suite[,suite] version arch [upgradable from: version]
APT_UPGRADABLE = re.compile(r"([a-z0-9][a-z0-9+.-]*)/(\S+) (\S+) \S+ \[upgradable from: ([^\]]+)\]\Z")
DEB_VERSION = re.compile(r"[a-zA-Z0-9.+:~-]{1,100}\Z")


def apt_status():
    """Pending package upgrades from apt's cached lists. Read-only: never runs apt update."""
    output = run(["/usr/bin/apt", "list", "--upgradable"], timeout=60)
    packages = []
    for line in output.splitlines():
        match = APT_UPGRADABLE.match(line.strip())
        if match and DEB_VERSION.fullmatch(match[3]) and DEB_VERSION.fullmatch(match[4]):
            packages.append({"name": match[1], "version": match[3], "from": match[4],
                             "security": "-security" in match[2]})
    # Ubuntu touches this stamp after every fully successful `apt update` (manual or apt-daily).
    stamp = Path("/var/lib/apt/periodic/update-success-stamp")
    try:
        refreshed = datetime.fromtimestamp(stamp.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        refreshed = None
    return {"upgradable": len(packages), "security": sum(p["security"] for p in packages),
            "packages": packages[:200], "lists_refreshed_at": refreshed}


def server_info(config):
    disk = shutil.disk_usage("/")
    result = {"id": safe_text(config.get("server_id", "server")), "hostname": safe_text(socket.gethostname()),
              "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
              "memory": None, "load": list(os.getloadavg()), "php_version": None, "php_release": None,
              "apt": None, "failed_units": None, "errors": []}
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
    if result["php_version"]:
        try:
            result["php_release"] = php_release(result["php_version"])
        except CheckFailed:
            result["errors"].append("PHP release check unavailable")
    try:
        result["apt"] = apt_status()
    except CheckFailed:
        result["errors"].append("Package update list unavailable")
    try:
        units = run(["/usr/bin/systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
        # Container hosts (e.g. OpenVZ) report harmless failures such as user@<uid>.service.
        ignored = config.get("ignore_failed_units", [])
        names = [line.split()[0] for line in units.splitlines() if line.strip()]
        result["failed_units"] = [safe_text(name) for name in names if not any(fnmatch.fnmatchcase(name, p) for p in ignored)]
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
