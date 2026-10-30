#!/bin/bash
# Acceptance checks for app updates on the disposable apps server (run setup.sh first). It changes
# the apps and their "GitHub" branches: recreate the container before running it again.
set -uo pipefail
source /src/tests/disposable/apps/lib.sh
pass=0 fail=0
check() { if eval "$2"; then echo "PASS $1"; pass=$((pass + 1)); else echo "FAIL $1"; fail=$((fail + 1)); fi; }

A=/home/adventos/app V=/home/vilnius/vilnius-adventistai G=/home/giesmynas2/public_html
# update <site> <sha> [options]: as the dashboard does, darius through sudo; sets $out and $rc.
update() { out=$(runuser -u darius -- sudo -n /usr/local/sbin/site-update --json "${@:3}" "$1" app "$2" 2>/dev/null); rc=$?; }
field() { python3 -c 'import json, sys; d = json.loads(sys.argv[1]); r = d["results"][-1] if "results" in d else d; print(r.get(sys.argv[2], ""))' "$out" "$1" 2>/dev/null; }
# app <site> <field>...: fields of the site's app inventory (update.<field> for the offered commit,
# errors for the number of site errors), space-separated.
app() { python3 -c 'import json, sys
s = next(s for s in json.load(open("/var/lib/site-watch/status.json"))["sites"] if s["id"] == sys.argv[1])
a, out = s.get("app") or {}, []
for f in sys.argv[2:]:
    out.append(str(len(s["errors"])) if f == "errors" else str((a.get("update") or {}).get(f[7:])) if f.startswith("update.") else str(a.get(f)))
print(" ".join(out))' "$@"; }
status() { curl -s -o /dev/null -w '%{http_code}' --resolve "$1:443:127.0.0.1" --cacert /etc/site-watch/apps-ca.pem "https://$1${2:-/}"; }
head_of() { runuser -u "$1" -- git -C "$2" rev-parse HEAD; }
table() { mariadb -N "$1" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '$1' AND table_name = '$2'"; }

curl -s -o /dev/null --resolve vilnius.test:443:127.0.0.1 --cacert /etc/site-watch/apps-ca.pem https://vilnius.test/  # first request migrates
A0=$(head_of adventos $A) V0=$(head_of vilnius $V)
G0=$(runuser -u giesmynas2 -- python3 -c 'import json; print(json.load(open("/home/giesmynas2/public_html/storage/last-update.json"))["sha"])')

echo "== starting point"
check "all three apps answer" '[ "$(status adventos.test)" = 302 ] && [ "$(status adventos.test /login)" = 200 ] && [ "$(status vilnius.test)" = 200 ] && [ "$(status giesmynas.test)" = 200 ] && [ "$(status giesmynas.test /probe.php)" = 200 ]'
collect
check "collector: installed commits, nothing newer" '[ "$(app adventos.test commit relation update)" = "$A0 identical None" ] && [ "$(app vilnius.test commit relation)" = "$V0 identical" ]'
check "collector: no errors for the apps" '[ "$(app adventos.test errors) $(app vilnius.test errors) $(app giesmynas.test errors)" = "0 0 0" ]'
check "collector: copy app reads its recorded commit" '[ "$(app giesmynas.test commit)" = "$G0" ]'

echo "== AdventOS (Laravel): gate, migration, rollback"
A1=$(testcommit AdventOS 6.0 "Test: add a table" "cat > database/migrations/2099_01_01_000001_site_update_probe.php <<'PHP'
<?php
use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;
return new class extends Migration {
    public function up(): void { Schema::create('site_update_probe', fn (Blueprint \$t) => \$t->id()); }
    public function down(): void { Schema::dropIfExists('site_update_probe'); }
};
PHP")
gate "$A1" pending
collect
check "collector: newer commit, gate pending" '[ "$(app adventos.test relation update.sha update.gate)" = "ahead $A1 pending" ] && [ "$(app adventos.test update.message)" = "Test: add a table" ]'
update adventos.test "$A1"
check "pending gate: refused, exit 2" '[ $rc = 2 ] && [ "$(field result)" = refused ] && field message | grep -q "has not passed on this commit (pending)"'
gate "$A1" failed; collect
update adventos.test "$A1"
check "failed gate: refused" '[ "$(field result)" = refused ] && field message | grep -q "(failed)"'
check "refusals left the app alone" '[ "$(head_of adventos $A)" = "$A0" ] && [ "$(table adventos site_update_probe)" = 0 ]'
gate "$A1" passed; collect
update adventos.test "$A1"
check "passed gate: updated, exit 0" '[ $rc = 0 ] && [ "$(field result)" = updated ] && [ "$(field from)" = "$A0" ] && [ "$(field to)" = "$A1" ]'
check "on the new commit, still on branch 6.0" '[ "$(head_of adventos $A)" = "$A1" ] && [ "$(runuser -u adventos -- git -C $A symbolic-ref --short HEAD)" = 6.0 ]'
check "migration ran" '[ "$(table adventos site_update_probe)" = 1 ]'
check "out of maintenance mode and healthy" '[ ! -e $A/storage/framework/down ] && [ "$(status adventos.test /login)" = 200 ]'
check "checkout clean (composer and caches left no tracked changes)" '[ -z "$(runuser -u adventos -- git -C $A status --porcelain --untracked-files=no)" ]'
backup=$(field backup)
check "backup: database dump and vendor tar, root-only" '[ -s $backup/database.sql ] && grep -q "CREATE TABLE" $backup/database.sql && tar -tf $backup/files.tar | grep -c "^vendor/autoload.php$" >/dev/null && [ "$(stat -c %a $backup/database.sql)" = 600 ]'
check "no database password in the backup notes or the log" '! grep -rq disposable-test-only $backup/backup.json /var/lib/site-watch/update-log.jsonl'

A2=$(testcommit AdventOS 6.0 "Test: another table and a broken front controller" "cat > database/migrations/2099_01_01_000002_site_update_probe2.php <<'PHP'
<?php
use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;
return new class extends Migration {
    public function up(): void { Schema::create('site_update_probe2', fn (Blueprint \$t) => \$t->id()); }
    public function down(): void { Schema::dropIfExists('site_update_probe2'); }
};
PHP
sed -i '1a http_response_code(500); exit;' public/index.php")
gate "$A2" passed; collect
update adventos.test "$A2"
check "broken commit: rolled back, exit 3" '[ $rc = 3 ] && [ "$(field result)" = rolled_back ] && [ "$(field to)" = "$A1" ]'
check "previous commit and branch are back" '[ "$(head_of adventos $A)" = "$A1" ] && [ "$(runuser -u adventos -- git -C $A symbolic-ref --short HEAD)" = 6.0 ]'
check "database restored: the new table is gone, the old one kept" '[ "$(table adventos site_update_probe2)" = 0 ] && [ "$(table adventos site_update_probe)" = 1 ]'
check "healthy and out of maintenance mode after the rollback" '[ ! -e $A/storage/framework/down ] && [ "$(status adventos.test /login)" = 200 ]'

# A failed commit that adds an auto-discovered package: its name lands in bootstrap/cache/packages.php,
# which must not stop artisan from starting the restored code.
A2b=$(testcommit AdventOS 6.0 "Test: add a package (still broken)" "COMPOSER_ALLOW_SUPERUSER=1 composer require spatie/laravel-honeypot --no-install --no-scripts --no-interaction -q")
gate "$A2b" passed; collect
update adventos.test "$A2b"
check "broken commit with a new package: rolled back, not stuck in maintenance" '[ "$(field result)" = rolled_back ] && [ "$(head_of adventos $A)" = "$A1" ] && [ ! -e $A/storage/framework/down ] && [ "$(status adventos.test /login)" = 200 ]'
check "the package is gone from vendor and Laravel's package cache" '[ ! -d $A/vendor/spatie/laravel-honeypot ] && ! grep -qs honeypot $A/bootstrap/cache/packages.php'

A3=$(testcommit AdventOS 6.0 "Test: harmless" "echo >> README.md")
gate "$A3" passed; collect
runuser -u adventos -- bash -c "echo local >> $A/composer.json"
update adventos.test "$A3"
check "local changes in the checkout: refused" '[ "$(field result)" = refused ] && field message | grep -q "local changes"'
runuser -u adventos -- git -C $A checkout -q -- composer.json
A4=$(testcommit AdventOS 6.0 "Test: newer still" "echo >> README.md")
update adventos.test "$A3"
check "branch moved since the check: refused" '[ "$(field result)" = refused ] && field message | grep -q "moved on"'
check "nothing changed by those refusals" '[ "$(head_of adventos $A)" = "$A1" ]'

echo "== Vilnius (Git): update, rollback, detached checkout"
mariadb vilnius -e "CREATE TABLE IF NOT EXISTS sentinel (id INT); INSERT INTO sentinel VALUES (7)"
V1=$(testcommit vilnius main "Test: harmless" "echo >> README.md")
gate "$V1" passed; collect
update vilnius.test "$V1"
check "updated" '[ "$(field result)" = updated ] && [ "$(head_of vilnius $V)" = "$V1" ] && [ "$(status vilnius.test)" = 200 ]'
# A database config with a guard that ends any script it was not meant for: the backup must fail
# rather than come back empty.
runuser -u vilnius -- cp $V/config/database.local.php $V/config/database.local.php.orig
runuser -u vilnius -- sed -i "1a if (!defined('ROOT_PATH')) { exit(0); }" $V/config/database.local.php
V1b=$(testcommit vilnius main "Test: harmless too" "echo >> README.md")
gate "$V1b" passed; collect
update vilnius.test "$V1b"
check "config that exits early: backup refused, nothing changed" '[ "$(field result)" = refused ] && field message | grep -q "backup failed" && [ "$(head_of vilnius $V)" = "$V1" ] && [ "$(status vilnius.test)" = 200 ]'
runuser -u vilnius -- mv $V/config/database.local.php.orig $V/config/database.local.php
V2=$(testcommit vilnius main "Test: broken" "sed -i '1a http_response_code(500); exit;' public/index.php")
gate "$V2" passed; collect
mariadb vilnius -e "INSERT INTO sentinel VALUES (8)"
update vilnius.test "$V2"
check "broken commit: rolled back to the previous commit" '[ "$(field result)" = rolled_back ] && [ "$(head_of vilnius $V)" = "$V1" ] && [ "$(status vilnius.test)" = 200 ]'
check "database restored from the backup taken just before" '[ "$(mariadb -N vilnius -e "SELECT GROUP_CONCAT(id ORDER BY id) FROM sentinel")" = "7,8" ]'
runuser -u vilnius -- git -C $V checkout -q --detach
V3=$(testcommit vilnius main "Test: fixed" "git checkout HEAD~1 -- public/index.php")
gate "$V3" passed; collect
update vilnius.test "$V3"
check "a detached checkout is updated onto the branch" '[ "$(field result)" = updated ] && [ "$(runuser -u vilnius -- git -C $V symbolic-ref --short HEAD)" = main ] && [ "$(head_of vilnius $V)" = "$V3" ]'

echo "== Giesmynas (copy): update, keep folders, rollback"
old_asset=$(ls $G/assets | head -1)
G1=$(testcommit edenoaidai v2 "Test: new build" "echo '<!-- G1 -->' >> server/index.html; echo js > server/assets/site-update-test.js; mkdir -p server/newdir; echo x > server/newdir/x.txt; echo nope > server/files/from-commit.txt")
gate "$G1" passed; collect
update giesmynas.test "$G1"
check "updated" '[ "$(field result)" = updated ] && [ "$(field to)" = "$G1" ]'
check "new files written, commit recorded" 'grep -q "G1" $G/index.html && [ -f $G/newdir/x.txt ] && grep -q "$G1" $G/storage/last-update.json'
check "assets folder replaced by the commit's own" '[ -f $G/assets/site-update-test.js ] && { [ -z "$old_asset" ] || [ -e "$G/assets/$old_asset" ] || git -C /srv/origin/edenoaidai.git cat-file -e "$G1:server/assets/$old_asset" 2>/dev/null || true; }'
check "kept folders untouched: uploads, config" '[ "$(cat $G/files/audio/keep.txt)" = "an upload that must survive" ] && [ ! -e $G/files/from-commit.txt ] && grep -q disposable-test-only $G/api/config.php'
check "written as the owner" '[ "$(stat -c %U $G/newdir/x.txt)" = giesmynas2 ] && [ -z "$(find $G -user root | head -1)" ]'
mariadb giesmynas2 -e "INSERT INTO probe VALUES (2, 'during')"
G2=$(testcommit edenoaidai v2 "Test: broken probe" "echo '<?php http_response_code(500);' > server/probe.php; echo '<!-- G2 -->' >> server/index.html; echo y > server/added-by-g2.txt")
gate "$G2" passed; collect
update giesmynas.test "$G2"
check "broken commit: rolled back" '[ "$(field result)" = rolled_back ] && [ "$(field to)" = "$G1" ] && [ "$(status giesmynas.test /probe.php)" = 200 ]'
check "files from the broken commit are gone, the previous ones back" '[ ! -e $G/added-by-g2.txt ] && ! grep -q G2 $G/index.html && grep -q G1 $G/index.html && [ -f $G/newdir/x.txt ]'
check "recorded commit, uploads and config are back" 'grep -q "$G1" $G/storage/last-update.json && [ -f $G/files/audio/keep.txt ] && grep -q disposable-test-only $G/api/config.php'
check "database restored" '[ "$(mariadb -N giesmynas2 -e "SELECT GROUP_CONCAT(id) FROM probe")" = "1,2" ]'
check "no leftovers from the rollback" '[ -z "$(ls -A $G | grep site-update-failed)" ]'

echo "== refusals and the dashboard's view"
update giesmynas.test "$G1"
check "an older or current commit is not offered" '[ "$(field result)" = refused ] && field message | grep -q "does not offer"'
out=$(runuser -u darius -- sudo -n /usr/local/sbin/site-update --json adventos.test app abc 2>&1); rc=$?
check "short commit: usage error" '[ $rc = 2 ]'
out=$(runuser -u darius -- sudo -n /usr/local/sbin/site-update --json --files-only-rollback adventos.test app "$A3" 2>&1); rc=$?
check "files-only rollback refused for apps" '[ $rc = 2 ] && echo "$out" | grep -q "always restores the database"'
out=$(runuser -u darius -- sudo -n /usr/local/sbin/site-update --json adventos.test nightly on 2>&1); rc=$?
check "no nightly switch for apps" '[ $rc = 1 ] && echo "$out" | grep -q "Unknown WordPress site"'
check "update log: one line per attempt, apps included" '[ "$(grep -c "\"kind\": \"app\"" /var/lib/site-watch/update-log.jsonl)" -ge 10 ]'
check "status.json still valid for the viewer" '(cd /opt/site-watch && python3 -c "from sitewatch.model import decode_status; decode_status(open(\"/var/lib/site-watch/status.json\", \"rb\").read())")'
check "token sent to no host but the GitHub API (fake API is plain http)" '! grep -q "auth=yes" /srv/fakegh/requests.log'

echo
echo "$pass passed, $fail failed"
[ $fail = 0 ]
