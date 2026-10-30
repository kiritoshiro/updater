# Architecture and status contract

The root Linux collector invokes WP-CLI through `sudo -n -H -u <owner> -- timeout … wp --skip-plugins --skip-themes --skip-packages`. A failed WordPress call becomes a fixed public error message; stderr is never copied to status. Each site is isolated so one fatal installation does not abort fleet collection.

The collector writes schema version 1 to `/var/lib/site-watch/status.json`. The app reads exactly this path with a fixed SSH command. Connection configuration is local and never supplied by the browser. The browser has status and refresh endpoints and, when the viewer config enables updates, one update endpoint. It accepts only a site, kind and slug that the current snapshot reports as updatable; the viewer turns them into a fixed `sudo -n /usr/local/sbin/site-update --json <site> <kind> [<slug>]` SSH command after strict pattern checks. There is no arbitrary command endpoint.

## Contract

- `schema_version`: integer `1`.
- `collected_at`: timezone-aware ISO 8601 timestamp in UTC.
- `demo`: boolean. Demo records are never live checks.
- `server`: id, hostname, PHP version, disk byte counts, memory byte counts, 1/5/15-minute load, failed systemd unit names and fixed error messages.
- `sites`: unique domain IDs. Each has `kind` (`wordpress`/`web`), `state` (`ok`/`partial`/`unhealthy`/`unreachable`), core, plugins, themes, named boolean flags, origin health, backup metadata, notes and fixed errors.
- Component: name, installed version, reported update version or null, activation status, source and check state. GitHub items also have a public repository identifier.
- Health: request path, HTTP status or null, response milliseconds or null, `ok` boolean; optional critical-error detection (WordPress's `wp_die()` fatal-error page with its English or Lithuanian message, not the words anywhere on a page)/error message.
- Backup: `not_configured`, `unknown`, or `observed` with modification time and byte size. `verified` is always false in this milestone.
- App (web sites with an `app` block only): `type`, `repository`, `branch`, installed `commit` (40 hex or null), `relation` of the branch head to it (`identical`, `ahead`, `behind`, `diverged` or null), `check_state`, and `update`: null, or the newest commit's `sha`, `date`, first message line (at most 120 printable characters) and `gate` (`passed`/`failed`/`pending`/`missing`).

The viewer rejects unsupported schemas, invalid timestamps, duplicate site IDs and oversized input. Older-than-two-hours and far-future snapshots are visibly stale. A failed refresh preserves the last successful snapshot and displays the failure alongside it. The 60-second browser poll rereads cached state for freshness; the explicit refresh button fetches the file/SSH source again.

## Trust and rollout boundaries

Root-owned config is validated independently of inventory. No credential or full WP option/config output is a contract field. A site's own Unix user remains a trust boundary: malicious must-use code can still run as that user, and an installed component can lie about its version. Planned file-integrity monitoring is separate from inventory.

The updater (`sitewatch/updater.py`, [UPDATER.md](UPDATER.md)) takes site roots and owners from the root-owned collector configuration, never from the snapshot. Web app updates (`sitewatch/apps.py`) take the commit from the inventory and re-check it, and its security gate, on GitHub at update time. It accepts only updates the inventory reports (at most 3 hours old), serialises jobs with a lock, refuses sites that fail their health check beforehand, backs up outside the site root, verifies GitHub release ZIPs against GitHub's SHA-256 digest and rolls back automatically. Rollback policy for the database: it is restored, which discards content written during the update window (normally a minute or two). These properties are covered by unit tests and a disposable-WordPress acceptance suite; production installation is a separate step.

Original requirements live in the Obsidian project notes “WP Site Watch — Dashboard and Updater Plan” and “WP Site Watch — Spec”. This repository implements phases 1–2 and 4 of the dashboard plan; alerting parts A–C and the extended statistics of phase 3 are separate milestones.

WP-CLI behavior references: [plugin list](https://developer.wordpress.org/cli/commands/plugin/list/) and [core check-update](https://developer.wordpress.org/cli/commands/core/check-update/). The latter documents both `--force-check` and the fact that must-use plugins still load with `--skip-plugins`.
