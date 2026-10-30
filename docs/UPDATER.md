# site-update: validated updates with backup and rollback

`site-update` is Phase 4 of the dashboard plan. It updates one WordPress component on one site, or
one of our own web apps to the newest commit of its branch ("Web apps" below), and puts everything
back if the site breaks. The dashboard runs it over SSH through a single
sudoers rule; it can also be run by hand on the server.

```sh
sudo site-update <site> plugin <slug>
sudo site-update <site> theme <slug>
sudo site-update <site> core
sudo site-update <site> translations
sudo site-update <site> all            # every plugin and theme update in the inventory, one by one
sudo site-update <site> app <commit>   # a web app: the newest commit of its branch, full 40-character SHA
sudo site-update --files-only-rollback <site> plugin <slug>   # on failure, restore files but not the database
sudo site-update <site> nightly on|off # the site's nightly switch
sudo site-update --json <site> ...     # one JSON line for the dashboard
site-update --nightly-run              # (root, from site-update-nightly.timer) the nightly updates
```

Only darius may run it (one sudoers line; user decision 2026-10-28).

## Nightly updates

Each WordPress site has a nightly switch, off by default, set from the dashboard's Nightly column or
with `site-update <site> nightly on|off`. It is stored in `/var/lib/site-watch/nightly.json`
(root:sitewatch 0640, with who changed it and when). `site-update-nightly.timer` runs every night at
about 03:30–03:50. For every site that is switched on it:

1. runs the collector, so the inventory is fresh;
2. updates every reported plugin and theme, then a WordPress release of the installed x.y branch
   (how WordPress ships security fixes; `wp core update --minor`, and a move to another branch is
   treated as a failed update), one at a time, each exactly as a manual update: backup, health check,
   automatic rollback (database included);
3. stops for that site at the first update that is refused or rolled back, and goes on with the next site.

A site that cannot be updated at all (stale inventory, owner or path problem) is logged as a skipped
item and makes the service end as failed, so the dashboard's failed-units line shows it. A run started
by hand (`sudo site-update --nightly-run`) is logged under that user, not `nightly`. On stop or
reboot, only site-update gets SIGTERM (`KillMode=mixed`); it stops the running command and finishes any
rollback within `TimeoutStopSec=45min`.

A new WordPress major version (for example 6.9 to 7.0) is never installed at night. Results are in the
update log with user `nightly`, shown under Recent updates. With `DISALLOW_FILE_MODS` on, WordPress's
own automatic background updates are off, so the switch is how a locked site stays current.

Exit codes: 0 updated or nothing to change, 1 setup problem, 2 refused (nothing changed),
3 rolled back (site healthy again), 4 rollback failed (check the site now).

## Web apps

Our own apps that are deployed from GitHub (not WordPress) are web sites with an `app` block in
`collector.json`. Only the newest commit of the configured branch is offered, and only when the
repository's security gate passed on **that exact commit**. Updates are manual only; there is no
nightly switch for apps.

```json
{"id": "app.example.org", "kind": "web", "owner": "appuser", "path": "/home/appuser/app", "health_paths": ["/", "/login"],
 "app": {"type": "laravel", "repository": "owner/repo", "branch": "main",
         "database": {"format": "env", "file": ".env", "name": "appdb"}}}
```

- `type` is how the app is deployed:
  - `laravel`: a Git checkout. Maintenance mode on, `git checkout -B <branch> <commit>`,
    `composer install --no-dev`, `artisan migrate --force`, the caches rebuilt, maintenance mode off.
  - `git`: a Git checkout and nothing else (an app that migrates itself on the next request).
  - `copy`: the commit's GitHub archive; the folder `source` (default `server`) is copied over the
    web root as the owner. Top-level names in `keep` (uploads, storage) are never touched, folders in
    `clear` (old build assets) are emptied first, nothing else is deleted, and the commit is recorded
    in `storage/last-update.json`, as the app's own updater does. Public repositories only.
- `database` says where the app keeps its own database login: `format` `env` (Laravel's
  `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`) or `php` (a file that returns an
  array; `keys` names its entries for `host`, `port`, `name`, `user`, `password`), `file` inside the app
  and the expected database `name`. The dump and restore run as the owner with that login
  (`sitewatch/appdb.php`), so root needs no database password; the password goes to the client in
  `MYSQL_PWD`, never on a command line. If the file names another database, the backup is refused.
- `php` (default `/usr/bin/php8.4`) and `composer` (default `/usr/local/bin/composer`) are optional.

**The collector** reads the installed commit as the owner (`git rev-parse HEAD`, or the recorded
commit for `copy`), asks GitHub for the branch head, compares the two and, when the branch is ahead,
reads the gate: the job "All security checks passed" (or "security-gate / All security checks
passed" from a release run) of `security-gate.yml` or `release.yml`, on exactly that commit, from
any event except `pull_request` (a pull request run scanned the merge preview, with fewer checks).
The result is `passed`, `failed`, `pending` or `missing`. The dashboard lists the commit under "Web
apps" with its message and gate; Update is offered only when it passed.

**One app update**, besides the steps under "What one update does":

1. The commit must be the one the inventory offers, still the branch head on GitHub, and the gate must
   have passed on it, checked again at that moment.
2. Git apps: the checkout must have no tracked local changes. The branch is fetched with the owner's
   deploy key, and the fetched head must be the commit GitHub reported and must build on the installed
   commit (no downgrades or side branches). A checkout that was left detached (for example by the
   app's own updater) is put on the branch.
   Copy apps: GitHub must report the commit as ahead of the recorded one, and the archive is checked
   (one top folder, no absolute paths, `..` or links, at most 128 MB, 512 MB unpacked) before anything changes.
3. Backup: the database, plus `vendor` (Laravel) or the web root without `keep` (copy). Git keeps the
   old commit itself.
4. Rollback: the previous commit (and branch, or detached state) is checked out again, the backed-up
   files are put back (for copy, also every top-level name the new commit wrote), and the database is
   restored: every table is dropped first, so tables a failed migration created do not survive.
   A rollback always restores the database (`--files-only-rollback` is refused for apps).

Deploy keys stay where they are (in the owners' `~/.ssh`); site-update never reads them. GitHub
API calls use the read-only token from `github_token_file` (see "Updater deployment" in
`docs/DEPLOYMENT.md`), sent to `api.github.com` only, never to the archive host or anywhere else.

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

- **By default the database is restored on rollback.** Anything written to the site between the
  backup and the rollback (normally a minute or two: comments, form entries, orders) is lost.
  Restoring was chosen over keeping a database that a failed plugin version may have migrated.
  Per update, the dashboard's checkbox (or `--files-only-rollback`) restores only the files and
  leaves the database as it is; the database backup is still taken. Not offered for WordPress core:
  `core update-db` upgrades the database, so a core rollback always restores it.
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
- Apps: a commit that only a pull request run checked has no gate result, so the repository's gate
  must also run on pushes to the deployed branch. Only the branch head can be installed; to go back
  to an older commit, revert it on the branch. Cron jobs or workers that write to the database during
  the update are not paused, and their writes are lost if it is rolled back.

## Install on the server

See "Updater deployment" in `docs/DEPLOYMENT.md`.

## Tests

- Unit tests: `python -m unittest discover -s tests` (`tests/test_updater.py`, `tests/test_apps.py`).
- App updates on a disposable server with real copies of the three apps and a fake GitHub API:
  `tests/disposable/apps/README.md`.
- Acceptance on a disposable WordPress in Docker: `tests/disposable/README.md`. It covers
  refusals with their exit codes, serialisation, wordpress.org and GitHub updates, plugin and core
  forced-failure rollback, SIGTERM (rolls back) and SIGHUP (ignored) during an update,
  translations, `all` and file modes. `viewer.sh` starts the dashboard over SSH for a manual check
  in the browser; that part is not automated.
