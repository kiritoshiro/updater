# Collector deployment (manual, not performed)

Target: Ubuntu 24.04, Python 3.11+, `/usr/local/bin/wp` (WP-CLI 2.10+ for `--force-check`), sudo, timeout, curl, PHP and systemd. Test on a disposable server before production. Commands below are installation instructions, not an automatic installer.

## Files and access

Copy the reviewed `sitewatch/` package to `/opt/site-watch/sitewatch/`. The code and every parent must be root-owned and not writable by site users. Do not install from a site-owned checkout or use an editable package controlled by a site owner.

```sh
sudo groupadd --system sitewatch  # only if it does not already exist
sudo install -d -o root -g root -m 0755 /opt/site-watch
sudo install -d -o root -g root -m 0700 /etc/site-watch
sudo install -d -o root -g sitewatch -m 0750 /var/lib/site-watch
sudo install -o root -g root -m 0600 config/collector.example.json /etc/site-watch/collector.json
```

Edit `/etc/site-watch/collector.json` as root. Set all five WordPress domains with their verified Unix owners and absolute document roots. Add non-WordPress sites with `kind: web`. Set `origin_ip` (top level, or per site to override) to the address nginx listens on for HTTPS; check with `ss -ltn 'sport = :443'`. It is required and has no default: `127.0.0.1` works only if nginx listens there.

Origins behind Cloudflare often serve a **Cloudflare Origin CA** certificate, which the system trust store does not include, so every check would fail TLS verification. Set `ca_file` (top level, or per site) to Cloudflare's Origin CA root, installed root-owned, e.g. `sudo install -o root -g root -m 0644 origin_ca_rsa_root.pem /etc/site-watch/cloudflare-origin-ca.pem`, from https://developers.cloudflare.com/ssl/static/origin_ca_rsa_root.pem (compare its fingerprint with Cloudflare's documentation). Sites with publicly trusted certificates (e.g. Let's Encrypt) set `"ca_file": null`. Find which issuer a site uses with `openssl s_client -connect <origin_ip>:443 -servername <site> </dev/null | openssl x509 -noout -issuer`. Verification is never disabled.

`ignore_failed_units` (optional) lists `fnmatch` patterns of systemd units whose failures are expected on this host, such as `user@*.service` on OpenVZ containers. Origin checks run **on the server** with SNI and certificate verification, using `curl --resolve`, no proxy, no redirect following, and no Cloudflare cache.

GitHub mapping keys are `plugin:<installed-slug>` and `theme:<installed-slug>`; values are public `owner/repository` identifiers. A top-level `github` map applies to every site where that slug is installed; a site's own `github` adds entries or maps a slug to `null` when that site's copy comes from elsewhere (e.g. an upstream build with the same folder name). Private repositories/tokens are not supported yet.

The server panel also reports PHP against php.net's latest release of the installed branch (an HTTPS request to php.net per run) and pending package upgrades from `apt list --upgradable`. The collector never runs `apt update`; pending upgrades are only as current as the last successful update, shown as "Package lists refreshed" (from `/var/lib/apt/periodic/update-success-stamp`). Keep `apt-daily.timer` enabled so the lists refresh daily. Optional `backup_directory` reports only the newest local file's time and size; it does not verify the archive or inspect remote Drive backups.

Grant the viewer's existing SSH user membership in `sitewatch`, then reconnect SSH. Replace the placeholder:

```sh
sudo usermod -aG sitewatch YOUR_SSH_USER
```

Status is root:sitewatch `0640`, replaced atomically in a root-owned `0750` directory. Group members cannot alter the inventory. If the alert watcher later shares this directory, keep its private baselines/spool/secrets in a separate root-only `0700` subdirectory. Do not expose those files to the dashboard group.

## Acceptance before enabling the timer

```sh
cd /opt/site-watch
sudo /usr/bin/python3 -m sitewatch.collector --config /etc/site-watch/collector.json --dry-run
```

Compare core, plugin and theme counts and versions with manual WP-CLI checks **as each site owner**, never root. Check every configured site. Verify origin login/archive checks against nginx, a broken site is partial/unreachable, unavailable checks remain unknown, and no passwords, keys or raw PHP error text appear in JSON. Verify owner is non-root and owns the document root. Existing custom WP-CLI configuration may affect commands, but runs as that owner.

After reviewing the dry run, run once without `--dry-run` to write `/var/lib/site-watch/status.json`. Check owner/mode and fetch through the intended read-only SSH account. Then install the provided units and enable the timer only when authorized for deployment:

```sh
sudo install -o root -g root -m 0644 deploy/site-collect.service deploy/site-collect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now site-collect.timer
sudo systemctl status site-collect.timer
sudo journalctl -u site-collect.service
```

The collector is sequential with bounded individual commands. Unlike the future 5-minute security watcher, it is not designed for a 60-second fleet-wide budget. The service has a 30-minute upper limit and locks prevent overlap. A stale snapshot warning appears after two hours; it is not an external outage alert.

To stop collection, disable `site-collect.timer`; the existing status file remains. The collector installs no WordPress plugin and needs no sudoers change.

# Updater deployment (Phase 4)

`site-update` is described in `docs/UPDATER.md`. It reuses the collector's code, configuration and `status.json`; install and run the collector first. Prove it on a disposable WordPress (`tests/disposable/README.md`) with the exact code you install.

Server needs: `mysqldump`/`mysql` clients for `wp db export`/`import` (installed with MariaDB/MySQL), `tar`, and outbound HTTPS to `api.github.com`, `github.com` and `*.githubusercontent.com` for GitHub-mapped plugins and themes. Backups need free space for one database dump plus the component; the wrapper refuses below 2 GB free.

1. Copy the reviewed code to `/opt/site-watch/sitewatch/` as for the collector (root-owned, not writable by others).
2. Install the wrapper, the one sudoers rule (replace `darius` with the dashboard's SSH user) and the backup folder:

```sh
sudo install -o root -g root -m 0755 deploy/site-update /usr/local/sbin/site-update
sudo install -o root -g root -m 0440 deploy/site-update.sudoers /etc/sudoers.d/site-update
sudo visudo -cf /etc/sudoers.d/site-update
sudo install -d -o root -g root -m 0700 /var/backups/site-update
sudo install -o root -g root -m 0644 deploy/site-update-nightly.service deploy/site-update-nightly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now site-update-nightly.timer   # does nothing until a site's nightly switch is on
```

3. Optional: add an archive page to each site's `health_paths` in `/etc/site-watch/collector.json`, e.g. `"health_paths": ["/", "/wp-login.php", "/category/naujienos/"]`.
4. Check from the dashboard user's SSH session, without changing anything:

```sh
sudo -n /usr/local/sbin/site-update example.org plugin no-such-plugin   # refused, exit 2
sudo -n /usr/bin/id                                                   # must fail: only site-update is allowed
```

5. Run a first real update by hand on one site, on a plugin you can live without for a minute, and read the result and `/var/lib/site-watch/update-log.jsonl`. Then set `"updates": true` in the viewer's `config.local.json`.

### Web app updates

1. The repository's security gate must run on pushes to the deployed branch (see "Web apps" in
   `docs/UPDATER.md`), or no new commit will ever have a gate result.
2. For private repositories, create a fine-grained GitHub token limited to those repositories with
   **Contents: read** and **Actions: read** (Metadata is added automatically), and store it root-only:

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/site-watch/github-token
sudo nano /etc/site-watch/github-token        # paste the token, one line
```

   and add `"github_token_file": "/etc/site-watch/github-token"` to `collector.json`. Public
   repositories work without it (at a lower GitHub rate limit). The token is never written to
   `status.json`, the log or a command line.
3. Add the `app` block to each app's site in `collector.json` (owner, path, health paths, type,
   repository, branch, database), run the collector and check the app's inventory in `status.json`.
4. Check that each owner can fetch with its deploy key (`sudo -u <owner> git -C <path> ls-remote origin`)
   and that its checkout is clean, then run one update by hand when the dashboard shows a commit with a
   passed gate.

To remove it: `sudo systemctl disable --now site-update-nightly.timer`, then `sudo rm /etc/sudoers.d/site-update /usr/local/sbin/site-update /etc/systemd/system/site-update-nightly.*`. The update log and backups stay until deleted; backups are pruned only by `site-update` itself.
