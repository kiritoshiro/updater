# Disposable WordPress for site-update

A throwaway Ubuntu 24.04 container with nginx, PHP 8.4 (ondrej PPA, as in production), MariaDB,
WP-CLI 2.12 and one site, `site1.test`, built with outdated components: WordPress 6.7.2,
`classic-editor` 1.6.3, `classic-widgets` 0.2, our Cookie Consent 1.0.11 from its GitHub release
and the bundled themes. A test-only must-use plugin (`break-after-update.php`) makes the front end
fail after a chosen component is updated, to prove rollback. Never point any of this at production.

From the repository root (Git Bash shown; in PowerShell drop `MSYS_NO_PATHCONV=1` and use `${PWD}`):

```sh
docker build -t sitewatch-disposable tests/disposable
MSYS_NO_PATHCONV=1 docker run -d --name sitewatch-disposable -p 127.0.0.1:8765:8766 -v "$(pwd -W):/src:ro" sitewatch-disposable
MSYS_NO_PATHCONV=1 docker exec sitewatch-disposable bash /src/tests/disposable/setup.sh
MSYS_NO_PATHCONV=1 docker exec sitewatch-disposable bash /src/tests/disposable/accept.sh
```

`accept.sh` prints PASS/FAIL per check and exits non-zero on any failure. It changes the site, so
recreate the container (`docker rm -f sitewatch-disposable`, then run and setup again) before
running it a second time.

To try the dashboard against the container, run `viewer.sh` after `setup.sh` and open
http://127.0.0.1:8765. It runs the real viewer as a separate user that reaches `darius` over SSH
and updates through sudo, exactly as from a PC. To see a rollback there, first run
`echo plugin:classic-widgets:0.2 | runuser -u site1 -- tee /home/site1/public_html/wp-content/break-after-update`
in the container, then press Update on classic-widgets. The container has no systemd, so refresh
the inventory by hand: `cd /opt/site-watch && python3 -m sitewatch.collector`.
