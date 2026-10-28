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


def update_command(config, site, kind, slug=None):
    if not isinstance(site, str) or not DOMAIN.fullmatch(site) or kind not in UPDATE_KINDS:
        raise ValueError("Invalid site or update kind")
    if (kind in ("plugin", "theme")) != (slug is not None) or (slug is not None and not (isinstance(slug, str) and SLUG.fullmatch(slug))):
        raise ValueError("Invalid component")
    return ssh_command(config, " ".join(["sudo", "-n", "/usr/local/sbin/site-update", "--json", site, kind] + ([slug] if slug else [])))


def clean_result(item):
    """Keep only the log fields the dashboard shows, as short strings."""
    if not isinstance(item, dict) or item.get("result") not in RESULTS:
        raise ValueError("Invalid update result")
    text = lambda value, limit=300: value[:limit] if isinstance(value, str) else None
    return {key: text(item.get(key)) for key in ("time", "user", "site", "kind", "slug", "source", "from", "to", "result", "message")}


def run_update(config, site, kind, slug=None):
    """Run one validated update over SSH; returns cleaned results or raises ValueError."""
    try:
        # The server waits at most 5 minutes for another update, then backup, update, check and rollback.
        result = subprocess.run(update_command(config, site, kind, slug), capture_output=True, timeout=75 * 60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("The update did not report back. Check the update log before trying again.") from exc
    try:
        lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        reply = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        # Includes sudo refusing to run: no sudoers rule, or a password is required.
        raise ValueError("The server did not run site-update. Check the sudoers rule and the SSH connection.") from exc
    if isinstance(reply, dict) and isinstance(reply.get("error"), str):
        raise ValueError(reply["error"][:300])
    if not isinstance(reply, dict) or not isinstance(reply.get("results"), list):
        raise ValueError("Unexpected reply from site-update")
    return [clean_result(item) for item in reply["results"]]


def fetch_update_log(config, limit=50):
    try:
        result = subprocess.run(ssh_command(config, f"tail -n {int(limit)} /var/lib/site-watch/update-log.jsonl 2>/dev/null || true"),
                                capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("SSH unavailable or timed out") from exc
    if result.returncode:
        raise ValueError("Update log read failed")
    items = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        try:
            items.append(clean_result(json.loads(line)))
        except ValueError:
            continue
    return items[::-1]
