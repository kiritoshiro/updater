"""Only fetch a fixed status file. Never accept a remote command from the UI."""

import os
import re
import subprocess
from pathlib import Path

from .model import decode_status


def ssh_executable():
    # On Windows, PATH may find Git for Windows' ssh first, which cannot use the Windows
    # OpenSSH agent. Prefer the system client so the agent-held key works from any shell.
    if os.name == "nt":
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(system):
            return system
    return "ssh"


def ssh_command(config):
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
            "-p", str(port), "--", f"{user}@{host}",
            "cat /var/lib/site-watch/status.json"]


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
