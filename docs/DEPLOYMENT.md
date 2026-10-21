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

Edit `/etc/site-watch/collector.json` as root. Set all five WordPress domains with their verified Unix owners and absolute document roots. Add non-WordPress sites with `kind: web`. Set `origin_ip` (top level, or per site to override) to the address nginx listens on for HTTPS; check with `ss -ltn 'sport = :443'`. It is required and has no default: `127.0.0.1` works only if nginx listens there. Origin checks run **on the server** with SNI and certificate verification, using `curl --resolve`, no proxy, no redirect following, and no Cloudflare cache.

GitHub mapping keys are `plugin:<installed-slug>` and `theme:<installed-slug>`; values are public `owner/repository` identifiers. Private repositories/tokens are not supported yet. Optional `backup_directory` reports only the newest local file's time and size; it does not verify the archive or inspect remote Drive backups.

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

To stop collection, disable `site-collect.timer`; the existing status file remains. No WordPress plugin or sudoers change is installed by this milestone.
