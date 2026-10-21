# WP Site Watch

A local dashboard for your WordPress sites and Linux server. A server collector writes a small JSON snapshot; the Windows/Linux viewer reads it over SSH. No remote-control plugin is installed on your sites.

**v0.1 is the first read-only milestone.** It includes a working demo, collector, SSH transport and dashboard. Production deployment and automatic updates have not been performed. The updater, rollback engine and alerting watcher remain on the roadmap.

## Try it

Requires Python 3.11+; no third-party runtime dependencies or frontend build.

```powershell
git clone https://github.com/kiritoshiro/updater.git
cd updater
git switch feat/initial-dashboard
python -m sitewatch --demo
```

The app opens `http://127.0.0.1:8765`. Use `--no-browser` to start without opening a browser, or `--port 8766` if the port is occupied. Ctrl+C stops it. The dashboard binds only to IPv4 localhost.

For an existing snapshot:

```powershell
python -m sitewatch --file status.local.json
```

For SSH, copy `config/viewer.example.json` to `config.local.json`, set host/user/port, then run:

```powershell
python -m sitewatch --config config.local.json
```

Use your existing OpenSSH agent and verified host key. Password prompts are disabled; the app does not read private keys, accept new host keys, or change SSH settings. Refresh retrieves the existing status file; it does not start a privileged scan. The page rechecks freshness every minute.

## Included

- Site search, filters, origin status and response times, core/plugin/theme versions and reported updates.
- Public GitHub stable release comparisons through an explicit component-to-repository mapping.
- Security flag visibility: `DISALLOW_FILE_MODS`, `DISALLOW_FILE_EDIT`, `WP_DEBUG` (unknown stays unknown).
- Disk, memory, load, PHP version, failed systemd services; optional local backup archive metadata.
- Non-WordPress site health checks.
- Stale/missing/failed snapshots, including retention of the last successful snapshot after a connection failure.
- Per-site failure isolation, owner-only WP-CLI execution, atomic status replacement and collector locking.
- Manual GitHub Actions checks for Windows/Linux and Python 3.11/3.13.

All demo readings are fictional and labelled. No demo number is evidence about the live server.

## Server setup

See [the deployment guide](docs/DEPLOYMENT.md). Configuration must use verified site owners, roots, origin IPs and GitHub slugs. Examples deliberately contain placeholders and no production credentials or connection details. Do not enable the hourly timer until the dry-run output matches manual inventory checks.

## Verification

```text
python -m unittest discover -s tests -v
python -m compileall -q sitewatch
node --check sitewatch/web/app.js
```

Tests cover the data contract, stale clocks, SSH argument rejection, fixed remote command, owner-switch command construction, secret-field exclusion, broken-site isolation, GitHub failures, origin failures, local HTTP assets, host/origin/token checks and snapshot retention. Unix ownership tests execute on Linux and skip on Windows. These are unit and local HTTP integration tests, not live server acceptance tests.

## Update roadmap

1. Validate this collector against every production site's manual inventory, without deploying changes to WordPress.
2. Add timestamp-filtered PHP/5xx/FPM statistics, certificates, mail, fail2ban, firewall, Drive backups and watcher findings.
3. Build the validated `site-update` wrapper: lock → backup → update → origin checks → rollback → audit log. Prove success, failure rollback and argument refusal on a disposable Linux WordPress first.
4. Add update controls after that acceptance gate. Only then consider `DISALLOW_FILE_MODS` changes or nightly policies.

No sudoers rule, mutation endpoint, automatic update timer or production security-flag change is included in v0.1.

## Limitations and security

WP-CLI runs as each site owner with regular plugins, themes and packages skipped. Must-use plugins and drop-ins may still load. A compromised owner can falsify its own inventory; this collector is not a forensic integrity guarantee. Root never invokes PHP from a site root directly. Config gets are restricted to three named boolean constants.

WP-CLI checks can refresh database update transients. “Read only” means no installs, updates or content/settings edits, not zero database writes. An empty update field means **none reported**, not a verified security status. Vendor updaters are skipped; configure GitHub mappings where applicable and review remaining commercial plugins separately. A GitHub comparison is version visibility only, not ZIP authenticity verification.

Status output explicitly projects permitted fields and excludes raw command output, package URLs, config contents, option values and credentials. Local HTTP endpoints require an exact loopback Host; refresh also requires matching Origin and a random session token. There is no CORS access. Other processes running as your desktop user can still read localhost, so this is not a multi-user security boundary.

See [architecture and status contract](docs/ARCHITECTURE.md).
