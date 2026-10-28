"""Fixed remote commands only: read the status file and update log, or run site-update with
arguments that match strict patterns. Nothing typed in the UI reaches the remote shell as text."""

import json
import os
import re
import subprocess
from pathlib import Path

from .model import DOMAIN, SLUG, decode_status

UPDATE_KINDS = ("plugin", "theme", "core", "translations")
RESULTS = ("updated", "unchanged", "refused", "rolled_back", "rollback_failed")


def ssh_executable():
    # On Windows, PATH may find Git for Windows' ssh first, which cannot use the Windows
    # OpenSSH agent. Prefer the system client so the agent-held key works from any shell.
    if os.name == "nt":
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(system):
            return system
    return "ssh"


def ssh_command(config, remote="cat /var/lib/site-watch/status.json"):
    host = config.get("host", "")
    user = config.get("user", "")
    port = config.get("port", 22)
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host):
        raise ValueError("SSH host must be an IPv4 address, hostname or SSH config alias")
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("Invalid SSH user")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    return [ssh_executable(), "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
            "-p", str(port), "--", f"{user}@{host}", remote]


def fetch_ssh(config):
    try:
        result = subprocess.run(ssh_command(config), capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("SSH unavailable or timed out. Check OpenSSH and your agent.") from exc
    if result.returncode:
        # Do not return remote stderr: even a broken PHP/site command may print secrets.
        raise ValueError("SSH read failed. Check the host key, agent, connection and status-file permissions.")
    return decode_status(result.stdout)


def fetch_file(path):
    with Path(path).open("rb") as stream:
        from .model import MAX_STATUS_BYTES
        return decode_status(stream.read(MAX_STATUS_BYTES + 1))


def update_command(config, site, kind, slug=None, restore_database=True):
    if not isinstance(site, str) or not DOMAIN.fullmatch(site) or kind not in UPDATE_KINDS:
        raise ValueError("Invalid site or update kind")
    if (kind in ("plugin", "theme")) != (slug is not None) or (slug is not None and not (isinstance(slug, str) and SLUG.fullmatch(slug))):
        raise ValueError("Invalid component")
    if type(restore_database) is not bool:
        raise ValueError("Invalid rollback choice")
    options = [] if restore_database else ["--files-only-rollback"]
    return ssh_command(config, " ".join(["sudo", "-n", "/usr/local/sbin/site-update", "--json", *options, site, kind] + ([slug] if slug else [])))


def nightly_command(config, site, enabled):
    if not isinstance(site, str) or not DOMAIN.fullmatch(site) or type(enabled) is not bool:
        raise ValueError("Invalid site or setting")
    return ssh_command(config, " ".join(["sudo", "-n", "/usr/local/sbin/site-update", "--json", site, "nightly", "on" if enabled else "off"]))


def clean_nightly(data):
    """{site: {"enabled", "changed_at", "by"}} with short strings only."""
    sites = data.get("sites", data) if isinstance(data, dict) else {}
    clean = {}
    for site, value in (sites.items() if isinstance(sites, dict) else []):
        if isinstance(site, str) and DOMAIN.fullmatch(site) and isinstance(value, dict) and type(value.get("enabled")) is bool:
            clean[site] = {"enabled": value["enabled"], **{k: value[k][:40] for k in ("changed_at", "by") if isinstance(value.get(k), str)}}
    return clean


def clean_result(item):
    """Keep only the log fields the dashboard shows, as short strings."""
    if not isinstance(item, dict) or item.get("result") not in RESULTS:
        raise ValueError("Invalid update result")
    text = lambda value, limit=300: value[:limit] if isinstance(value, str) else None
    return {key: text(item.get(key)) for key in ("time", "user", "site", "kind", "slug", "source", "from", "to", "result", "message")}


def site_update_reply(command, timeout):
    """Run site-update over SSH and return its last JSON line; never shows remote stderr."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("site-update did not report back. Check the update log before trying again.") from exc
    try:
        lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        reply = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        # Includes sudo refusing to run: no sudoers rule, or a password is required.
        raise ValueError("The server did not run site-update. Check the sudoers rule and the SSH connection.") from exc
    if isinstance(reply, dict) and isinstance(reply.get("error"), str):
        raise ValueError(reply["error"][:300])
    if not isinstance(reply, dict):
        raise ValueError("Unexpected reply from site-update")
    return reply


def run_update(config, site, kind, slug=None, restore_database=True):
    """Run one validated update over SSH; returns cleaned results or raises ValueError."""
    # The server waits at most 5 minutes for another update; backup, core update, check and rollback
    # are bounded by their own command limits (about 90 minutes in the worst case).
    reply = site_update_reply(update_command(config, site, kind, slug, restore_database), 120 * 60)
    if not isinstance(reply.get("results"), list):
        raise ValueError("Unexpected reply from site-update")
    return [clean_result(item) for item in reply["results"]]


def set_nightly(config, site, enabled):
    reply = site_update_reply(nightly_command(config, site, enabled), 60)
    setting = clean_nightly(reply.get("nightly")).get(site)
    if not setting:
        raise ValueError("Unexpected reply from site-update")
    return setting


NIGHTLY_MARK = "--- site-watch nightly ---"


def fetch_server_state(config, limit=50):
    """(recent update log items, newest first; nightly switches) in one fixed SSH read."""
    # A missing switch file means every switch is off; an unreadable one means unknown (None).
    remote = (f"tail -n {int(limit)} /var/lib/site-watch/update-log.jsonl 2>/dev/null; echo '{NIGHTLY_MARK}'; "
              "if [ -e /var/lib/site-watch/nightly.json ]; then cat /var/lib/site-watch/nightly.json 2>/dev/null; "
              "else echo '{}'; fi; true")
    try:
        result = subprocess.run(ssh_command(config, remote), capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("SSH unavailable or timed out") from exc
    if result.returncode:
        raise ValueError("Update log read failed")
    log, _, nightly = result.stdout.decode("utf-8", errors="replace").partition(NIGHTLY_MARK)
    items = []
    for line in log.splitlines():
        try:
            items.append(clean_result(json.loads(line)))
        except ValueError:
            continue
    try:
        switches = clean_nightly(json.loads(nightly))
    except ValueError:
        switches = None
    return items[::-1], switches


class SshUpdates:
    """What an updates-enabled dashboard may ask the server: fixed site-update commands only."""

    def __init__(self, config):
        self.config = config

    def run(self, site, kind, slug, restore_database=True):
        return run_update(self.config, site, kind, slug, restore_database)

    def server_state(self):
        return fetch_server_state(self.config)

    def set_nightly(self, site, enabled):
        return set_nightly(self.config, site, enabled)
