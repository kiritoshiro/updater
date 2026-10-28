# site-update: validated updates with backup and rollback

`site-update` is Phase 4 of the dashboard plan. It updates one WordPress component on one site,
and puts everything back if the site breaks. The dashboard runs it over SSH through a single
sudoers rule; it can also be run by hand on the server.

```sh
sudo site-update <site> plugin <slug>
sudo site-update <site> theme <slug>
sudo site-update <site> core
sudo site-update <site> translations
sudo site-update <site> all            # every plugin and theme update in the inventory, one by one
sudo site-update --json <site> ...     # one JSON line for the dashboard
```

Exit codes: 0 updated or nothing to change, 1 setup problem, 2 refused (nothing changed),
3 rolled back (site healthy again), 4 rollback failed (check the site now).

## What one update does

1. **Checks the request.** The site must be a WordPress site in `/etc/site-watch/collector.json`,
   and the last inventory (`status.json`, at most 3 hours old) must report an update for exactly
   that plugin, theme or core. Slugs must match `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. Anything else is
   refused before anything runs. Translations need no inventory entry.
2. **Waits its turn.** One update at a time across all sites (`/var/lib/site-watch/.update.lock`);
   a second request waits up to 20 minutes (5 minutes when the dashboard asks; the dashboard waits
   up to 2 hours for a reply, more than the slowest possible core update with rollback).
3. **Checks the site first.** The origin health check (the collector's `health_paths`, through
   `curl --resolve` past Cloudflare) must pass. An already-broken site is refused, because a
   later failure could not be blamed on the update.
4. **For our GitHub plugins and themes** (the collector's `github` mapping), downloads the latest
   stable release before backing anything up. The release must have exactly one ZIP asset with a
   GitHub SHA-256 digest; the download must match it, contain only the folder named like the
   installed slug (no `..`, absolute paths or symlinks), and carry the release's version in its
   plugin header or `style.css`. Redirects may only lead to `github.com` or `*.githubusercontent.com`.
5. **Backs up** to `/var/backups/site-update/<site>/<time>-<kind>-<slug>/` (root, 0700/0600): the
   database (`wp db export`) and a tar of the plugin or theme folder, the core files
   (`wp-admin`, `wp-includes`, root `*.php` except `wp-config.php`), or `wp-content/languages`.
   Needs 2 GB free. Backups older than 7 days are deleted, except those of a failed rollback
   (marked with a `KEEP` file), which stay until deleted by hand.
6. **Asks the alert watcher for a quiet window** (`site-watch quiet <site> 30`) if it is installed.
7. **Updates** with WP-CLI as the site owner: `plugin update`, `theme update`, `core update` plus
   `core update-db`, `language … update`, or `plugin|theme install <verified zip> --force`.
8. **Checks the site again** after a 3-second pause (PHP's opcache rechecks changed files every
   2 seconds by default), up to 3 times 5 seconds apart. Any status outside 200–399, a connection
   failure or a WordPress critical-error page counts as broken.
9. **Rolls back** once the backup exists if anything goes wrong: the update command fails, the new
   version cannot be read, the check fails, an unexpected error occurs, or the run is stopped with
   Ctrl+C or SIGTERM. It moves every backed-up folder aside (the plugin or theme folder; for core
   `wp-admin` and `wp-includes`; `languages`), unpacks the backup, imports the database backup,
   leaves maintenance mode, then checks health and the version again. The result is `rolled_back`
   only if the site is healthy and back at its old version; otherwise `rollback_failed` with the
   backup path. Ctrl+C and SIGTERM only set a flag: the running command is stopped (SIGTERM through
   sudo and `timeout` to WP-CLI, then waited for) and the rollback follows; a signal during a
   restore waits until it is done. SIGHUP is ignored, so a dropped SSH session does not stop an
   update halfway. For long manual runs, a `tmux` session is still the safer habit.
10. **Logs** one JSON line per item as soon as it is done to `/var/lib/site-watch/update-log.jsonl`
    (root:sitewatch 0640, last 1,000): time, sudo user, site, item, source, versions, result,
    message and backup path. No command output is logged or shown: a broken site may print secrets.
11. **Starts the collector** (`systemctl start --no-block site-collect.service`) so the dashboard
    shows the new versions within a minute.

## Safety rules

- Site files and the database are only touched as the site owner (`sudo -u <owner>`): tar,
  `mv`, `rm`, WP-CLI. Root only lists the site folder, writes into its own backup folder and
  hands the backup in and out through pipes. A symlink planted in a site cannot redirect a
  root write. A plugin or theme folder that is itself a symlink is refused.
- The wrapper `/usr/local/sbin/site-update` runs Python with `-I` and imports only root-owned code
  from `/opt/site-watch`; sudo's environment is not used.
- Arguments are matched against strict patterns and the collector's configuration and inventory;
  they never reach a shell. The dashboard sends the same validated values in a fixed SSH command.

## Known limits

- **The database is restored on rollback.** Anything written to the site between the backup and
  the rollback (normally a minute or two: comments, form entries, orders) is lost. Restoring was
  chosen over keeping a database that a failed plugin version may have migrated.
- A core rollback restores `wp-admin`, `wp-includes` and the root PHP files. Anything a core update
  adds under `wp-content` (for example a new bundled theme) stays.
- The health check cannot see a page cache, and relies on PHP noticing changed files. Server 561363
  has no nginx cache, no page-cache plugin, and opcache with `validate_timestamps` on and
  `revalidate_freq` 2 (checked 2026-10-28); re-check this before enabling updates elsewhere.
- Only the latest GitHub release is installed, and only public repositories are supported.
  Vendor plugins with their own updaters are not updated (their updaters are skipped).
- The health check sees fatal errors and error statuses on the checked paths only. Add an archive page to a
  site's `health_paths`, for example `/category/<slug>/`. Paths cannot contain `?`.
- There is no undo after a successful update other than restoring the backup by hand:
  `tar -xf files.tar` and `wp db import database.sql`, both as the site owner.

## Install on the server

See "Updater deployment" in `docs/DEPLOYMENT.md`.

## Tests

- Unit tests: `python -m unittest discover -s tests` (`tests/test_updater.py`).
- Acceptance on a disposable WordPress in Docker: `tests/disposable/README.md`. It covers
  refusals with their exit codes, serialisation, wordpress.org and GitHub updates, plugin and core
  forced-failure rollback, SIGTERM (rolls back) and SIGHUP (ignored) during an update,
  translations, `all` and file modes. `viewer.sh` starts the dashboard over SSH for a manual check
  in the browser; that part is not automated.
