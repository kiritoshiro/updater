# Architecture and status contract

The root Linux collector invokes WP-CLI through `sudo -n -H -u <owner> -- timeout … wp --skip-plugins --skip-themes --skip-packages`. A failed WordPress call becomes a fixed public error message; stderr is never copied to status. Each site is isolated so one fatal installation does not abort fleet collection.

The collector writes schema version 1 to `/var/lib/site-watch/status.json`. The app reads exactly this path with a fixed SSH command. Connection configuration is local and never supplied by the browser. The browser has only status and refresh endpoints. It has no arbitrary command or update endpoint.

## Contract

- `schema_version`: integer `1`.
- `collected_at`: timezone-aware ISO 8601 timestamp in UTC.
- `demo`: boolean. Demo records are never live checks.
- `server`: id, hostname, PHP version, disk byte counts, memory byte counts, 1/5/15-minute load, failed systemd unit names and fixed error messages.
- `sites`: unique domain IDs. Each has `kind` (`wordpress`/`web`), `state` (`ok`/`partial`/`unhealthy`/`unreachable`), core, plugins, themes, named boolean flags, origin health, backup metadata, notes and fixed errors.
- Component: name, installed version, reported update version or null, activation status, source and check state. GitHub items also have a public repository identifier.
- Health: request path, HTTP status or null, response milliseconds or null, `ok` boolean; optional critical-error detection/error message.
- Backup: `not_configured`, `unknown`, or `observed` with modification time and byte size. `verified` is always false in this milestone.

The viewer rejects unsupported schemas, invalid timestamps, duplicate site IDs and oversized input. Older-than-two-hours and far-future snapshots are visibly stale. A failed refresh preserves the last successful snapshot and displays the failure alongside it. The 60-second browser poll rereads cached state for freshness; the explicit refresh button fetches the file/SSH source again.

## Trust and rollout boundaries

Root-owned config is validated independently of inventory. No credential or full WP option/config output is a contract field. A site's own Unix user remains a trust boundary: malicious must-use code can still run as that user, and an installed component can lie about its version. Planned file-integrity monitoring is separate from inventory.

The future updater must derive permitted site roots and owners from root-owned configuration, never just from a potentially forged snapshot. It must validate requested slugs against a fresh inventory, serialize jobs, fail closed on stale/unknown checks, back up outside the site root, verify artifacts against a trusted digest and safely roll back before update controls are enabled. Database migrations need explicit rollback policy; restoring a database automatically can discard new content. None of these promises is presented as implemented by this release.

Original requirements live in the Obsidian project notes “WP Site Watch — Dashboard and Updater Plan” and “WP Site Watch — Spec”. This repository starts with phases 1–2 of the dashboard plan; alerting parts A–C and the destructive updater are separate remaining milestones.

WP-CLI behavior references: [plugin list](https://developer.wordpress.org/cli/commands/plugin/list/) and [core check-update](https://developer.wordpress.org/cli/commands/core/check-update/). The latter documents both `--force-check` and the fact that must-use plugins still load with `--skip-plugins`.
