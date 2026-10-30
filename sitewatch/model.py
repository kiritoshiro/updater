"""Small, explicit public status contract shared by collector and viewer."""

from datetime import datetime, timezone
import json
import re

MAX_STATUS_BYTES = 8 * 1024 * 1024
DOMAIN = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z", re.ASCII)
SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}\Z", re.ASCII)
COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing collection time")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Collection time must include a timezone")
    return dt


def validate_status(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported status schema; expected version 1")
    timestamp(data.get("collected_at"))
    if not isinstance(data.get("server"), dict) or not isinstance(data.get("sites"), list):
        raise ValueError("Status must contain a server and sites")
    if type(data.get("demo", False)) is not bool:
        raise ValueError("Invalid demo marker")
    ids = set()
    for site in data["sites"]:
        if not isinstance(site, dict) or not DOMAIN.fullmatch(str(site.get("id", ""))):
            raise ValueError("Invalid site identifier")
        if site["id"] in ids:
            raise ValueError("Duplicate site identifier")
        ids.add(site["id"])
        if site.get("kind") not in ("wordpress", "web") or site.get("state") not in ("ok", "partial", "unhealthy", "unreachable"):
            raise ValueError("Invalid site kind or state")
        for key in ("plugins", "themes", "errors", "health"):
            if not isinstance(site.get(key, []), list):
                raise ValueError(f"Invalid {key} collection")
        for key in ("errors", "notes"):
            if not isinstance(site.get(key, []), list) or any(not isinstance(value, str) for value in site.get(key, [])):
                raise ValueError("Invalid site messages")
        if site.get("core") is not None and not isinstance(site["core"], dict):
            raise ValueError("Invalid core inventory")
        for key in ("flags", "backup"):
            if not isinstance(site.get(key, {}), dict):
                raise ValueError(f"Invalid {key}")
        for value in site.get("flags", {}).values():
            if value is not None and type(value) is not bool:
                raise ValueError("Flags must be boolean or unknown")
        for key in ("plugins", "themes"):
            for component in site.get(key, []):
                if not isinstance(component, dict) or not isinstance(component.get("name"), str):
                    raise ValueError("Invalid component inventory")
                for field in ("version", "update_version", "status", "source"):
                    if component.get(field) is not None and not isinstance(component[field], str):
                        raise ValueError("Invalid component field")
        for check in site.get("health", []):
            if not isinstance(check, dict) or type(check.get("ok")) is not bool or not isinstance(check.get("path"), str):
                raise ValueError("Invalid health check")
        validate_app(site.get("app"))
    return data


def validate_app(app):
    """A web app's installed commit and the newest commit on its branch (apps.app_inventory)."""
    if app is None:
        return
    if not isinstance(app, dict) or app.get("type") not in ("laravel", "git", "copy"):
        raise ValueError("Invalid app inventory")
    for field in ("repository", "branch", "relation", "check_state"):
        if app.get(field) is not None and not isinstance(app[field], str):
            raise ValueError("Invalid app field")
    if app.get("commit") is not None and not COMMIT.fullmatch(str(app["commit"])):
        raise ValueError("Invalid installed commit")
    update = app.get("update")
    if update is not None:
        if (not isinstance(update, dict) or not COMMIT.fullmatch(str(update.get("sha", "")))
                or update.get("gate") not in ("passed", "failed", "pending", "missing")):
            raise ValueError("Invalid app update")
        for field in ("date", "message"):
            if update.get(field) is not None and not isinstance(update[field], str):
                raise ValueError("Invalid app update field")


def decode_status(raw):
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError("Status exceeds the 8 MiB limit")
    return validate_status(json.loads(raw))


def freshness(data, max_age=7200):
    age = (datetime.now(timezone.utc) - timestamp(data["collected_at"])).total_seconds()
    return {"stale": age > max_age or age < -300, "age_seconds": max(0, int(age)),
            "clock_skew": age < -300}


def numeric_version(value):
    value = str(value).removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        return None
    parts = tuple(int(p) for p in value.split("."))
    return parts + (0,) * (4 - len(parts))
