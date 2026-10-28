#!/bin/bash
# Acceptance tests for site-update on the disposable site. Run as root in the container after
# setup.sh; every update is started by darius through sudo, exactly as the dashboard does.
set -uo pipefail
SITE=site1.test ROOT=/home/site1/public_html LOG=/var/lib/site-watch/update-log.jsonl
PASS=0 FAIL=0
ok() { echo "PASS  $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
wpv() { runuser -u site1 -- wp --path="$ROOT" --skip-plugins --skip-themes "$@" 2>/dev/null; }
update() { runuser -u darius -- sudo -n /usr/local/sbin/site-update "$@"; }
collect() { (cd /opt/site-watch && python3 -m sitewatch.collector >/dev/null 2>&1); }
front() { curl -s -o /dev/null -w '%{http_code}' --resolve "$SITE:443:127.0.0.1" --cacert /etc/site-watch/test-ca.pem "https://$SITE/"; }
backups() { find /var/backups/site-update -mindepth 2 -maxdepth 2 -type d 2>/dev/null | wc -l; }
last() { tail -n 1 "$LOG" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
breaks() { if [ -n "${1:-}" ]; then echo "$1" | runuser -u site1 -- tee "$ROOT/wp-content/break-after-update" >/dev/null; else rm -f "$ROOT/wp-content/break-after-update"; fi; }

echo "== refusals"
out=$(runuser -u darius -- /usr/local/sbin/site-update $SITE plugin classic-editor 2>&1); code=$?
check "without sudo: refused (exit $code)" test $code -eq 2 -a -n "$(grep -o 'must run as root' <<<"$out")"
before=$(backups)
# expected exit: 2 = argument error or refused, 1 = unknown site (setup)
for case in "2|$SITE plugin ;id" "2|$SITE plugin ../../wp-config" "2|$SITE plugin a;rm" "2|$SITE plugin unknown-slug" \
            "2|$SITE theme classic-editor" "2|$SITE plugin twentytwentyfive" "2|$SITE core extra" "2|$SITE shell" \
            "1|nosuch.test plugin classic-editor" "2|$SITE plugin -x" "1|../$SITE plugin classic-editor"; do
  expected=${case%%|*} args=${case#*|}
  # shellcheck disable=SC2086
  update $args >/dev/null 2>&1; code=$?
  check "refused: $args (exit $code, expected $expected)" test $code -eq "$expected"
done
cp -p /var/lib/site-watch/status.json /root/status.keep
python3 - <<'PY'
import json
from datetime import datetime, timedelta, timezone
path = "/var/lib/site-watch/status.json"
data = json.load(open(path))
data["collected_at"] = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(timespec="seconds")
json.dump(data, open(path, "w"))
PY
out=$(update $SITE plugin classic-editor 2>&1); code=$?
check "refused: inventory older than 3 hours (exit $code)" test $code -eq 1 -a -n "$(grep -o 'older than 3 hours' <<<"$out")"
cp -p /root/status.keep /var/lib/site-watch/status.json
check "refusals created no backup" test "$(backups)" -eq "$before"
check "sudo allows only site-update" bash -c "! runuser -u darius -- sudo -n /usr/bin/id >/dev/null 2>&1"

echo "== two updates at once are serialised"
update $SITE plugin classic-editor > /tmp/u1.out 2>&1 & p1=$!
sleep 1
update $SITE theme twentytwentyfour > /tmp/u2.out 2>&1 & p2=$!
wait $p1; c1=$?; wait $p2; c2=$?
check "plugin update succeeded (exit $c1)" test $c1 -eq 0
check "theme update succeeded (exit $c2)" test $c2 -eq 0
check "second update waited" grep -q "Waiting for another update" /tmp/u2.out
check "classic-editor is newer than 1.6.3 ($(wpv plugin get classic-editor --field=version))" \
  test "$(wpv plugin get classic-editor --field=version)" != "1.6.3"
check "twentytwentyfour is newer than 1.3 ($(wpv theme get twentytwentyfour --field=version))" \
  test "$(wpv theme get twentytwentyfour --field=version)" != "1.3"
check "update windows do not overlap" python3 - "$LOG" <<'PY'
import json, sys
from datetime import datetime, timedelta
a, b = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()[-2:]]
end = datetime.fromisoformat(a["time"]) + timedelta(seconds=a["seconds"])
sys.exit(0 if datetime.fromisoformat(b["time"]) >= end - timedelta(seconds=1) else 1)
PY
dir=$(tail -n 2 "$LOG" | head -n 1 | python3 -c "import json,sys; print(json.load(sys.stdin)['backup'])")
check "backup has database, files and metadata" test -s "$dir/database.sql" -a -s "$dir/files.tar" -a -s "$dir/backup.json"
check "backup files are root 0600, folder root 0700" test "$(stat -c '%U %a' "$dir/database.sql") $(stat -c '%U %a' /var/backups/site-update)" = "root 600 root 700"
check "darius cannot read backups" bash -c "! runuser -u darius -- ls /var/backups/site-update >/dev/null 2>&1"
check "log is root:sitewatch 0640 and darius can read it" bash -c "test \"\$(stat -c '%U:%G %a' $LOG)\" = 'root:sitewatch 640' && runuser -u darius -- cat $LOG >/dev/null"
check "log line names the user" test "$(last user)" = darius
collect

echo "== an update that breaks the site is rolled back"
breaks "plugin:classic-widgets:0.2"
update $SITE plugin classic-widgets; code=$?
check "result rolled_back (exit $code)" test $code -eq 3 -a "$(last result)" = rolled_back
check "classic-widgets back at 0.2" test "$(wpv plugin get classic-widgets --field=version)" = "0.2"
check "classic-widgets still active" test "$(wpv plugin get classic-widgets --field=status)" = active
check "front page 200 again ($(front))" test "$(front)" = 200
check "no failed copy left behind" bash -c "! ls -a $ROOT/wp-content/plugins | grep -q site-update-failed"
check "no maintenance file left" test ! -e "$ROOT/.maintenance"
update --files-only-rollback $SITE plugin classic-widgets; code=$?
check "files-only rollback: rolled back (exit $code)" test $code -eq 3 -a "$(last result)" = rolled_back
check "files-only rollback: database left as it was" bash -c "tail -n 1 $LOG | grep -q 'database was left as it was'"
check "files-only rollback: classic-widgets back at 0.2, site 200" test "$(wpv plugin get classic-widgets --field=version)" = "0.2" -a "$(front)" = 200

echo "== our GitHub plugin from its verified release"
update $SITE plugin adventistai-cookie-consent; code=$?
check "result updated (exit $code), source github" test $code -eq 0 -a "$(last source)" = github
check "cookie consent at 1.0.17 ($(wpv plugin get adventistai-cookie-consent --field=version))" \
  test "$(wpv plugin get adventistai-cookie-consent --field=version)" = "1.0.17"
check "cookie consent still active" test "$(wpv plugin get adventistai-cookie-consent --field=status)" = active
collect
update $SITE plugin adventistai-cookie-consent >/dev/null 2>&1; code=$?
check "second run refused: no update in inventory (exit $code)" test $code -eq 2

echo "== signals: SIGTERM after the backup rolls back, SIGHUP is ignored"
backed_up() {  # wait until this run's backup is complete (backup.json is written last)
  for _ in $(seq 1 240); do
    [ -n "$(find /var/backups/site-update/$SITE -maxdepth 2 -name backup.json -newer "$1" 2>/dev/null)" ] && return 0
    sleep 0.25
  done
  return 1
}
updater_pid() { pgrep -f "from sitewatch.updater import main"; }
touch /tmp/term.mark; sleep 1
update $SITE plugin akismet > /tmp/term.out 2>&1 & p=$!
sent=no; backed_up /tmp/term.mark && kill -TERM "$(updater_pid)" && sent=yes
wait $p; code=$?
check "SIGTERM sent after the backup" test $sent = yes
check "SIGTERM: rolled back (exit $code)" test $code -eq 3 -a "$(last result)" = rolled_back
check "SIGTERM: logged as interrupted" bash -c "tail -n 1 $LOG | grep -q interrupted"
check "SIGTERM: akismet back at 5.3.6" test "$(wpv plugin get akismet --field=version)" = "5.3.6"
check "SIGTERM: front page 200, no maintenance file" test "$(front)" = 200 -a ! -e "$ROOT/.maintenance"
check "SIGTERM: no WP-CLI process left running" bash -c "! pgrep -u site1 -f /usr/local/bin/wp >/dev/null"
touch /tmp/hup.mark; sleep 1
update $SITE theme twentytwentythree > /tmp/hup.out 2>&1 & p=$!
# A hangup reaches the dashboard user's sudo and site-update, not the commands site-update starts.
pid=""; backed_up /tmp/hup.mark && pid=$(updater_pid)
sent=no; [ -n "$pid" ] && kill -HUP "$(ps -o ppid= -p "$pid" | tr -d ' ')" "$pid" && sent=yes
wait $p; code=$?
check "SIGHUP sent to sudo and site-update after the backup" test $sent = yes
check "SIGHUP to sudo and site-update: update finished (exit $code)" test $code -eq 0 -a "$(last result)" = updated
check "SIGHUP: twentytwentythree updated to $(wpv theme get twentytwentythree --field=version)" \
  test "$(wpv theme get twentytwentythree --field=version)" != "1.6"

# A real terminal hangup, like a dropped SSH session: run under script(1)'s pseudo-terminal and kill
# the terminal mid-update. sudo uses its own pseudo-terminal (use_pty), as on Ubuntu 24.04.
touch /tmp/pty.mark; sleep 1
runuser -u darius -- script -qfec "sudo /usr/local/sbin/site-update $SITE plugin akismet" /dev/null >/dev/null 2>&1 &
sent=no; backed_up /tmp/pty.mark && pkill -KILL -x script && sent=yes
for _ in $(seq 1 240); do updater_pid >/dev/null || break; sleep 0.5; done
check "terminal hangup sent after the backup" test $sent = yes
check "terminal hangup: update finished and logged ($(last result))" test "$(last result)" = updated -a "$(last slug)" = akismet
check "terminal hangup: akismet at $(wpv plugin get akismet --field=version)" test "$(wpv plugin get akismet --field=version)" != "5.3.6"
check "terminal hangup: site 200, no maintenance file, no WP-CLI left" \
  bash -c "test \"\$(curl -s -o /dev/null -w '%{http_code}' --resolve $SITE:443:127.0.0.1 --cacert /etc/site-watch/test-ca.pem https://$SITE/)\" = 200 -a ! -e $ROOT/.maintenance && ! pgrep -u site1 -f /usr/local/bin/wp >/dev/null"

echo "== core: a broken update rolls back, then a good one"
breaks "core::6.7.2"
update $SITE core; code=$?
check "core result rolled_back (exit $code)" test $code -eq 3
check "core back at 6.7.2" test "$(wpv core version)" = "6.7.2"
check "database version matches 6.7.2" test "$(wpv option get db_version)" = "$(grep -oP "wp_db_version = \K[0-9]+" $ROOT/wp-includes/version.php)"
check "front page 200 after core rollback" test "$(front)" = 200
breaks ""
update $SITE core; code=$?
check "core updated (exit $code) to $(wpv core version)" test $code -eq 0 -a "$(wpv core version)" != "6.7.2"
check "database upgraded" test "$(wpv option get db_version)" = "$(grep -oP "wp_db_version = \K[0-9]+" $ROOT/wp-includes/version.php)"
collect

echo "== translations and all"
update $SITE translations; code=$?
check "translations updated (exit $code)" test $code -eq 0
update $SITE all; code=$?
check "all: remaining plugin updated (exit $code)" test $code -eq 0 -a "$(wpv plugin get classic-widgets --field=version)" != "0.2"

echo "== nightly switch and nightly run"
NIGHTLY=/var/lib/site-watch/nightly.json
/usr/local/sbin/site-update --nightly-run > /tmp/n0.out 2>&1; code=$?
check "nightly run with every switch off does nothing (exit $code)" test $code -eq 0 -a -n "$(grep -o 'No site has nightly' /tmp/n0.out)"
wpv plugin install classic-editor --version=1.6.3 --force --quiet; collect   # something for the night to do
out=$(update $SITE nightly on); code=$?
check "darius switches nightly on (exit $code)" test $code -eq 0
check "nightly.json root:sitewatch 0640, darius can read it, by darius" bash -c "test \"\$(stat -c '%U:%G %a' $NIGHTLY)\" = 'root:sitewatch 640' && runuser -u darius -- grep -q '\"by\": \"darius\"' $NIGHTLY"
check "the switch is refused for an unknown site" bash -c "! runuser -u darius -- sudo -n /usr/local/sbin/site-update nosuch.test nightly on >/dev/null 2>&1"
/usr/local/sbin/site-update --nightly-run > /tmp/n1.out 2>&1; code=$?
check "nightly run updates the site (exit $code)" test $code -eq 0 -a "$(wpv plugin get classic-editor --field=version)" != "1.6.3"
check "nightly run is logged as user nightly" test "$(last user)" = nightly -a "$(last slug)" = classic-editor
check "nightly run did not touch core (7.1.3 is current here; minor-only rule is unit-tested)" bash -c "! grep -q ' core' /tmp/n1.out"
update $SITE nightly off >/dev/null; collect
wpv plugin install classic-editor --version=1.6.3 --force --quiet; collect
/usr/local/sbin/site-update --nightly-run > /tmp/n2.out 2>&1
check "with the switch off again, nothing is updated" test "$(wpv plugin get classic-editor --field=version)" = "1.6.3"
check "front page 200 at the end" test "$(front)" = 200

echo "== log hygiene"
check "no secrets in the log" bash -c "! grep -qiE 'password|DB_PASS|AUTH_KEY|disposable-test-only' $LOG"
echo "results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
