# Alert watcher — Part A, step 1

Status: **in development** on `feat/watcher-part-a`. Nothing here is deployed.

The watcher sends a Discord message within about five minutes when a WordPress site on the server
changes in a way that matches the October 2026 break-ins, or when a site goes down at the origin.
It is the push side of WP Site Watch; the collector/dashboard is the pull side.

## Scope of step 1

Every run (systemd timer, every 5 minutes, as root, **no WordPress or PHP code executed**):

| # | Check | New | Changed | Removed |
| --- | --- | --- | --- | --- |
| 1 | Top-level entries of `wp-content/plugins/` | CRITICAL | — | WARNING |
| 1b | Top-level files in `wp-content/mu-plugins/` (with sha256) | CRITICAL | CRITICAL | WARNING |
| 2 | Drop-ins: `wp-content/*.php` (with sha256) | CRITICAL | CRITICAL | WARNING |
| 3 | `wp-config.php`, `index.php`, `.htaccess`, `.user.ini` in the site root (sha256) | CRITICAL | CRITICAL | WARNING |
| 4 | `*.php`, `*.phtml`, `*.phar` (any case) under `wp-content/uploads/` | CRITICAL ¹ | CRITICAL | INFO |
| 5 | Top-level entries of `wp-content/themes/` | WARNING | — | INFO |
| 6 | Origin health (`/`, `/wp-login.php` or the site's `health_paths`) via `curl --resolve`, Cloudflare bypassed | failing on **two runs in a row** → CRITICAL; recovery → INFO | | |
| 7 | A `wp-config.php` under `/home/*/public_html` or `/home/*/domains/*/public_html` that is not a configured site | WARNING | | |

¹ A tiny `index.php` that only says "silence is golden" is INFO, not CRITICAL.

Not in step 1 (later steps): web-log signals, database checks (active plugins, admins, application
passwords, UpdraftCentral keys, key options), the hourly checksum manifest, mail bounces, Part C
heartbeat.

## Alerts

- **Discord webhook** (decided 2026-10-21). One message per site per run, CRITICAL and WARNING
  findings together; long messages are split at Discord's 2,000-character limit.
- CRITICAL and WARNING are sent immediately. INFO goes into a **daily digest at 08:00** server
  time, which also confirms the watcher is alive and lists sites it could not check.
- **Alert once, then accept:** after each run the current state becomes the baseline, so a finding
  is reported once.
- Failed delivery is spooled and resent oldest-first on the next run, marked "delayed".
- Messages contain names, sizes and times only, never file contents, option values or secrets.
  File names are attacker-controlled, so they are sanitised and Discord mentions are disabled
  except an optional configured mention for CRITICAL.
- Never via the server's own mail.

## Baseline and maintenance

- The first run per site records the baseline and sends one INFO ("watching N sites"). PHP files
  already present in `uploads/` at that point are listed once as WARNING, because they may be old
  backdoors.
- `rebaseline <site>` accepts the current state by hand.
- `quiet <site> <minutes>`: WARNING becomes INFO during the window. **CRITICAL is never silenced.**
- A site that is already failing at the first run is recorded as down; it alerts again only after
  it has recovered and fails again.

## Commands

```
python3 -m sitewatch.watcher run [--dry-run]     # what the timer runs; --dry-run prints instead of sending/saving
python3 -m sitewatch.watcher baseline            # record the baseline for all sites without alerting
python3 -m sitewatch.watcher rebaseline <site>
python3 -m sitewatch.watcher quiet <site> <minutes>
python3 -m sitewatch.watcher status
python3 -m sitewatch.watcher test-alert
```

## Files

| Path | Owner / mode | Content |
| --- | --- | --- |
| `/etc/site-watch/collector.json` | root 0600 | sites, owners, paths, `origin_ip`, `ca_file` (shared with the collector) |
| `/etc/site-watch/watch.json` | root 0600 | `webhook_url`, optional `mention`, `digest_hour`, `ignore` |
| `/var/lib/site-watch/watch/` | root 0700 | `state.json` (baseline, origin state, quiet windows, pending INFO), `spool/` |

The watcher's state is root-only. The dashboard group `sitewatch` cannot read it.

## Safety rules

- Runs as root but never executes site code: it only lists, `lstat`s and hashes files.
- Symlinks are recorded, never followed. Files are opened with `O_NOFOLLOW` and only regular files
  up to 16 MB are hashed.
- The `uploads/` walk is capped (entries and time); hitting the cap is reported as WARNING.
- A run is locked with `flock`; an interrupted run leaves the previous state intact (atomic writes).
- An `ignore` list (glob, relative to `wp-content/`) can exclude noisy paths from the uploads walk,
  e.g. `uploads/wflogs/*`; it can never cover `plugins/`, `mu-plugins/` or drop-ins.

## Deviations from the original spec (WP Site Watch — Spec, Part A)

- **Python, not Bash.** It shares the site configuration, origin check (with Cloudflare Origin CA
  trust) and safety rules with the collector, and it is unit-tested on Windows and Linux.
- **systemd timer, not `/etc/cron.d`**, like the collector.
- **mu-plugins are hashed in step 1** (the spec only listed them), so a changed must-use plugin is
  CRITICAL. Lesson of 2026-10-21: identify must-use plugins by content, not by name.

## Tests before production (step 1 part of spec A8)

On a disposable WordPress or in the unit tests:

1. First run: baseline only, one INFO.
2. Copy a plugin folder in by file → CRITICAL.
3. Add `x.php` to `uploads/2026/10/` → CRITICAL.
4. Change a drop-in, `wp-config.php` or a must-use plugin → CRITICAL.
5. Make the origin fail twice → CRITICAL once; recovery → INFO.
6. Break the webhook URL → spooled; fix it → delivered, marked "delayed".
7. `quiet` window: a removed plugin → INFO; a new plugin → still CRITICAL.
8. Kill a run mid-way → the next run works (no stale lock, no corrupt state).
