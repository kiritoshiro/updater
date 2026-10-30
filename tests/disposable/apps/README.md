# Disposable web apps for site-update

A throwaway server (the `tests/disposable` image plus Git, Composer and two PHP extensions) with
real copies of our three apps, installed the way production runs them:

| Host | App | Type |
|---|---|---|
| `adventos.test` | AdventOS (Laravel), branch `6.0` | `laravel` |
| `vilnius.test` | Vilnius, branch `main` | `git` |
| `giesmynas.test` | Edeno Aidai, branch `v2`, files from `server/` | `copy` |

The code comes from your local clones, mounted read-only, so private repositories need no
credentials in the container. `setup.sh` copies their deployed branch (taken from each clone's
`refs/remotes/origin/<branch>`, so fetch first) into bare repositories under `/srv/origin`, which play
GitHub. `fakegithub.py` answers the GitHub API on `127.0.0.1:8099` from those bare repositories: branch
heads, compare, archives and security gate results, which the tests set per commit in
`/srv/fakegh/gates.json`. `sitewatch_test_shim.py` points site-update and the collector at it; that
shim exists only in this container. Never point any of this at production.

From the repository root, after building `tests/disposable` (Git Bash; the source must have LF line
endings, so export it with `git -c core.autocrlf=false archive` into a folder and mount that):

```sh
docker build -t sitewatch-apps tests/disposable/apps
MSYS_NO_PATHCONV=1 docker run -d --name sitewatch-apps -v "<exported source>:/src:ro" \
  -v "<AdventOS clone>/.git:/repos/AdventOS.git:ro" -v "<vilnius-adventistai clone>/.git:/repos/vilnius.git:ro" \
  -v "<edenoaidai clone>/.git:/repos/edenoaidai.git:ro" sitewatch-apps
MSYS_NO_PATHCONV=1 docker exec sitewatch-apps bash /src/tests/disposable/apps/setup.sh
MSYS_NO_PATHCONV=1 docker exec sitewatch-apps bash /src/tests/disposable/apps/accept.sh
```

`setup.sh` runs `composer install` for AdventOS, so the container needs internet access once.
`accept.sh` pushes test commits to the bare repositories (a migration, a broken front controller, a
new build) and runs site-update through sudo as `darius`, as the dashboard does. It checks the
collector's view, refusals (pending, failed or missing gate, moved branch, local changes, an older
commit, bad arguments), updates, migrations, rollbacks of commit, files and database, kept upload
folders, owner-only writes, a detached checkout and the update log. It changes the apps, so recreate
the container before running it again.
