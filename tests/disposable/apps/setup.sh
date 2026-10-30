#!/bin/bash
# Three disposable web apps for site-update's app updates, installed the way production runs them,
# from local copies of the real repositories (mounted read-only at /repos):
#   adventos.test   AdventOS, Laravel, Git checkout (/repos/AdventOS.git, branch 6.0)
#   vilnius.test    Vilnius, plain PHP, Git checkout (/repos/vilnius.git, branch main)
#   giesmynas.test  Edeno Aidai, files copied from server/ (/repos/edenoaidai.git, branch v2)
# Each starts at the real branch head. A fake GitHub API on 127.0.0.1:8099 (fakegithub.py) serves
# branch heads, gate results and archives from bare copies in /srv/origin. Never point this at production.
set -euo pipefail
DBPASS=disposable-test-only
T=/src/tests/disposable/apps
source $T/lib.sh
git config --global --add safe.directory '*'  # root reads the mounted repositories and the owners' bare copies
git config --global user.email test@example.invalid
git config --global user.name "Disposable test"

service mariadb start >/dev/null
getent group sitewatch >/dev/null || groupadd --system sitewatch
id darius >/dev/null 2>&1 || useradd -m -s /bin/bash darius
usermod -aG sitewatch darius
for u in adventos vilnius giesmynas2; do
  id $u >/dev/null 2>&1 || useradd -m -s /bin/bash $u
  chmod 0711 /home/$u
  mariadb -e "CREATE DATABASE IF NOT EXISTS $u; CREATE USER IF NOT EXISTS '$u'@'localhost' IDENTIFIED BY '$DBPASS';
    CREATE USER IF NOT EXISTS '$u'@'127.0.0.1' IDENTIFIED BY '$DBPASS';
    GRANT ALL ON $u.* TO '$u'@'localhost'; GRANT ALL ON $u.* TO '$u'@'127.0.0.1';"
done

# Bare "GitHub" copies, owned by the app owner (Git refuses repositories of another user).
origin() { # name source-git-dir branch owner
  local bare=/srv/origin/$1.git
  if [ ! -d "$bare" ]; then
    git init -q --bare "$bare"
    git -C "$bare" fetch -q "$2" "refs/remotes/origin/$3:refs/heads/$3"
    git -C "$bare" symbolic-ref HEAD "refs/heads/$3"
  fi
  chown -R "$4:$4" "$bare"
}
mkdir -p /srv/origin && chmod 0755 /srv/origin
origin AdventOS /repos/AdventOS.git 6.0 adventos
origin vilnius /repos/vilnius.git main vilnius
origin edenoaidai /repos/edenoaidai.git v2 giesmynas2

# Fake GitHub API.
mkdir -p /srv/fakegh && [ -f /srv/fakegh/gates.json ] || echo '{}' > /srv/fakegh/gates.json
if ! curl -fs http://127.0.0.1:8099/repos/x/y >/dev/null 2>&1 && ! pgrep -f fakegithub.py >/dev/null; then
  nohup python3 $T/fakegithub.py >/srv/fakegh/server.log 2>&1 &
  sleep 1
fi

# TLS for the three hosts from one test CA; PHP-FPM pools as each owner.
mkdir -p /etc/site-watch && chmod 0700 /etc/site-watch
if [ ! -f /root/apps-ca.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=Disposable Apps CA" -keyout /root/apps-ca.key -out /root/apps-ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=apps.test" -keyout /etc/nginx/apps.key -out /root/apps.csr 2>/dev/null
  printf 'subjectAltName=DNS:adventos.test,DNS:vilnius.test,DNS:giesmynas.test\n' > /root/apps-san.ext
  openssl x509 -req -in /root/apps.csr -CA /root/apps-ca.pem -CAkey /root/apps-ca.key -CAcreateserial -days 30 \
    -extfile /root/apps-san.ext -out /etc/nginx/apps.crt 2>/dev/null
fi
install -o root -g root -m 0644 /root/apps-ca.pem /etc/site-watch/apps-ca.pem
for u in adventos vilnius giesmynas2; do
  cat > /etc/php/8.4/fpm/pool.d/$u.conf <<CONF
[$u]
user = $u
group = $u
listen = /run/php/$u.sock
listen.owner = www-data
listen.group = www-data
pm = ondemand
pm.max_children = 4
CONF
done
# Vilnius creates its first admin on the first request from these (its README's installation step).
grep -q ORGSITE_ADMIN_EMAIL /etc/php/8.4/fpm/pool.d/vilnius.conf || {
  echo "env[ORGSITE_ADMIN_EMAIL] = admin@example.invalid"
  echo "env[ORGSITE_ADMIN_PASSWORD] = $(openssl rand -hex 16)"
} >> /etc/php/8.4/fpm/pool.d/vilnius.conf
vhost() { # host root socket front-controller
  cat <<CONF
server {
  listen 127.0.0.1:443 ssl;
  server_name $1;
  ssl_certificate /etc/nginx/apps.crt;
  ssl_certificate_key /etc/nginx/apps.key;
  root $2;
  index index.php index.html;
  location / { try_files \$uri \$uri/ $4; }
  location ~ \.php\$ { include snippets/fastcgi-php.conf; fastcgi_pass unix:/run/php/$3.sock; }
}
CONF
}
{
  vhost adventos.test /home/adventos/app/public adventos '/index.php?$query_string'
  vhost vilnius.test /home/vilnius/vilnius-adventistai/public vilnius '/index.php?$query_string'
  vhost giesmynas.test /home/giesmynas2/public_html giesmynas2 '/index.html'
} > /etc/nginx/sites-enabled/apps
grep -q adventos.test /etc/hosts || echo "127.0.0.1 adventos.test vilnius.test giesmynas.test" >> /etc/hosts

as() { local u=$1; shift; runuser -u "$u" -- "$@"; }

# AdventOS (Laravel) at the real 6.0 head.
A=/home/adventos/app
if [ ! -f $A/.disposable-ready ]; then
  [ -e $A ] && { echo "Incomplete AdventOS from an earlier run; recreate the container." >&2; exit 1; }
  as adventos git clone -q -b 6.0 /srv/origin/AdventOS.git $A
  as adventos cp $A/.env.example $A/.env
  as adventos sed -i -e "s/^DB_PASSWORD=.*/DB_PASSWORD=$DBPASS/" -e "s#^APP_URL=.*#APP_URL=https://adventos.test#" $A/.env
  as adventos bash -c "cd $A && composer install --no-dev --prefer-dist --no-interaction --no-progress --optimize-autoloader -q"
  as adventos php8.4 $A/artisan key:generate --force --no-interaction >/dev/null
  as adventos php8.4 $A/artisan migrate --force --no-interaction >/dev/null
  for c in optimize:clear config:cache route:cache view:cache; do as adventos php8.4 $A/artisan $c --no-interaction >/dev/null; done
  as adventos ln -sfn $A/public /home/adventos/public_html
  # .disposable-ready is in .git/info/exclude so the checkout stays clean
  echo .disposable-ready | as adventos tee -a $A/.git/info/exclude >/dev/null
  as adventos touch $A/.disposable-ready
fi

# Vilnius (plain PHP, migrates itself on the next request) at the real main head.
V=/home/vilnius/vilnius-adventistai
if [ ! -f $V/.disposable-ready ]; then
  [ -e $V ] && { echo "Incomplete Vilnius from an earlier run; recreate the container." >&2; exit 1; }
  as vilnius git clone -q -b main /srv/origin/vilnius.git $V
  as vilnius tee $V/config/database.local.php >/dev/null <<PHP
<?php
return ['host' => '127.0.0.1', 'port' => 3306, 'name' => 'vilnius', 'user' => 'vilnius', 'password' => '$DBPASS'];
PHP
  echo .disposable-ready | as vilnius tee -a $V/.git/info/exclude >/dev/null
  as vilnius touch $V/.disposable-ready
fi

# Giesmynas (files copied from server/) at the real v2 head plus one test commit that adds
# probe.php, a PHP health check path; recorded as its own updater does.
G=/home/giesmynas2/public_html
if [ ! -f $G/.disposable-ready ]; then
  [ -e $G ] && { echo "Incomplete Giesmynas from an earlier run; recreate the container." >&2; exit 1; }
  head=$(testcommit edenoaidai v2 "Test: PHP health probe" "echo '<?php echo \"ok\";' > server/probe.php")
  as giesmynas2 mkdir -p $G
  git -C /srv/origin/edenoaidai.git archive "$head" server | as giesmynas2 tar -x --strip-components=1 -C $G
  as giesmynas2 tee $G/api/config.php >/dev/null <<PHP
<?php
return ['db_host' => 'localhost', 'db_name' => 'giesmynas2', 'db_user' => 'giesmynas2', 'db_pass' => '$DBPASS',
        'admin_password_hash' => '', 'github_token' => '', 'allowed_origins' => [],
        'files_dir' => dirname(__DIR__) . '/files', 'storage_dir' => dirname(__DIR__) . '/storage'];
PHP
  as giesmynas2 mkdir -p $G/files/audio $G/storage
  echo "an upload that must survive" | as giesmynas2 tee $G/files/audio/keep.txt >/dev/null
  printf '{"source": "commit", "ref": "%s", "sha": "%s", "appliedAt": "2026-10-20T19:46:53+00:00"}\n' "$head" "$head" \
    | as giesmynas2 tee $G/storage/last-update.json >/dev/null
  as giesmynas2 touch $G/.disposable-ready
fi
mariadb giesmynas2 -e "CREATE TABLE IF NOT EXISTS probe (id INT PRIMARY KEY, note VARCHAR(40)); INSERT IGNORE INTO probe VALUES (1, 'before')"

service php8.4-fpm restart >/dev/null
service nginx restart >/dev/null

# Site Watch, root-owned, as in production, plus the test shim that points it at the fake API.
install -d -o root -g root -m 0755 /opt/site-watch
rm -rf /opt/site-watch/sitewatch && cp -r /src/sitewatch /opt/site-watch/
install -o root -g root -m 0644 $T/sitewatch_test_shim.py /opt/site-watch/sitewatch_test_shim.py
chown -R root:root /opt/site-watch
find /opt/site-watch -type d -exec chmod 0755 {} + && find /opt/site-watch -type f -exec chmod 0644 {} +
install -d -o root -g sitewatch -m 0750 /var/lib/site-watch
install -d -o root -g root -m 0700 /var/backups/site-update
printf 'ghp_%s\n' "$(openssl rand -hex 18)" > /etc/site-watch/github-token && chmod 0600 /etc/site-watch/github-token
cat > /etc/site-watch/collector.json <<'CONF'
{"server_id": "apps", "origin_ip": "127.0.0.1", "ca_file": "/etc/site-watch/apps-ca.pem",
 "github_token_file": "/etc/site-watch/github-token",
 "sites": [
  {"id": "adventos.test", "kind": "web", "owner": "adventos", "path": "/home/adventos/app", "health_paths": ["/", "/login"],
   "app": {"type": "laravel", "repository": "kiritoshiro/AdventOS", "branch": "6.0",
           "database": {"format": "env", "file": ".env", "name": "adventos"}}},
  {"id": "vilnius.test", "kind": "web", "owner": "vilnius", "path": "/home/vilnius/vilnius-adventistai", "health_paths": ["/"],
   "app": {"type": "git", "repository": "kiritoshiro/vilnius-adventistai", "branch": "main",
           "database": {"format": "php", "file": "config/database.local.php", "name": "vilnius",
                        "keys": {"host": "host", "port": "port", "name": "name", "user": "user", "password": "password"}}}},
  {"id": "giesmynas.test", "kind": "web", "owner": "giesmynas2", "path": "/home/giesmynas2/public_html", "health_paths": ["/", "/probe.php"],
   "app": {"type": "copy", "repository": "kiritoshiro/edenoaidai", "branch": "v2", "source": "server",
           "keep": ["files", "storage", "awstats"], "clear": ["assets"],
           "database": {"format": "php", "file": "api/config.php", "name": "giesmynas2",
                        "keys": {"host": "db_host", "name": "db_name", "user": "db_user", "password": "db_pass"}}}}]}
CONF
chmod 0600 /etc/site-watch/collector.json
cat > /usr/local/sbin/site-update <<'WRAP'
#!/bin/sh
# Disposable tests only: deploy/site-update plus the fake-GitHub shim.
cd / && exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, "/opt/site-watch"); import sitewatch_test_shim; from sitewatch.updater import main; sys.exit(main(sys.argv[1:]))' "$@"
WRAP
chmod 0755 /usr/local/sbin/site-update
install -o root -g root -m 0440 /src/deploy/site-update.sudoers /etc/sudoers.d/site-update
visudo -cf /etc/sudoers.d/site-update >/dev/null
echo "setup done"
