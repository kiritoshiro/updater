#!/bin/bash
# Build one disposable WordPress site (site1.test) with old components, then install Site Watch's
# collector and site-update from /src the way docs/DEPLOYMENT.md and docs/UPDATER.md describe.
set -euo pipefail
SITE=site1.test OWNER=site1 ROOT=/home/site1/public_html
DBPASS=disposable-test-only

service mariadb start >/dev/null
mariadb -e "CREATE DATABASE IF NOT EXISTS site1; CREATE USER IF NOT EXISTS 'site1'@'localhost' IDENTIFIED BY '$DBPASS'; GRANT ALL ON site1.* TO 'site1'@'localhost';"

id "$OWNER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$OWNER"
id darius >/dev/null 2>&1 || useradd -m -s /bin/bash darius
getent group sitewatch >/dev/null || groupadd --system sitewatch
usermod -aG sitewatch darius
chmod 0755 /home/$OWNER

# PHP-FPM pool as the site owner, nginx with a test CA on 127.0.0.1:443.
cat > /etc/php/8.4/fpm/pool.d/site1.conf <<CONF
[site1]
user = site1
group = site1
listen = /run/php/site1.sock
listen.owner = www-data
listen.group = www-data
pm = ondemand
pm.max_children = 4
CONF
mkdir -p /etc/site-watch && chmod 0700 /etc/site-watch
if [ ! -f /etc/nginx/site1.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=Disposable Test CA" \
    -keyout /root/test-ca.key -out /root/test-ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$SITE" -keyout /etc/nginx/site1.key -out /root/site1.csr 2>/dev/null
  printf 'subjectAltName=DNS:%s\n' "$SITE" > /root/san.ext
  openssl x509 -req -in /root/site1.csr -CA /root/test-ca.pem -CAkey /root/test-ca.key -CAcreateserial \
    -days 30 -extfile /root/san.ext -out /etc/nginx/site1.crt 2>/dev/null
fi
install -o root -g root -m 0644 /root/test-ca.pem /etc/site-watch/test-ca.pem
cat > /etc/nginx/sites-enabled/default <<CONF
server {
  listen 127.0.0.1:443 ssl;
  server_name $SITE;
  ssl_certificate /etc/nginx/site1.crt;
  ssl_certificate_key /etc/nginx/site1.key;
  root $ROOT;
  index index.php;
  location / { try_files \$uri \$uri/ /index.php?\$args; }
  location ~ \.php\$ { include snippets/fastcgi-php.conf; fastcgi_pass unix:/run/php/site1.sock; }
}
CONF
service php8.4-fpm restart >/dev/null
service nginx restart >/dev/null
grep -q "$SITE" /etc/hosts || echo "127.0.0.1 $SITE" >> /etc/hosts

# WordPress with components that have updates: core 6.7.2, two wordpress.org plugins, an old
# theme, our cookie-consent plugin from an old GitHub release, and a Lithuanian core translation.
as_owner() { runuser -u "$OWNER" -- "$@"; }
wp_owner() { as_owner wp --path="$ROOT" "$@"; }
# A half-built site is not reused: recreate the container if this block fails.
if [ ! -f "$ROOT/.disposable-ready" ]; then
  [ -e "$ROOT" ] && { echo "Incomplete site from an earlier run; recreate the container." >&2; exit 1; }
  as_owner mkdir -p "$ROOT"
  wp_owner core download --version=6.7.2 --quiet
  wp_owner config create --dbname=site1 --dbuser=site1 --dbpass="$DBPASS" --dbhost=localhost --quiet
  wp_owner core install --url="https://$SITE" --title="Disposable" --admin_user=testadmin \
    --admin_password="$(openssl rand -hex 16)" --admin_email=test@example.invalid --skip-email --quiet
  wp_owner plugin install classic-editor --version=1.6.3 --activate --quiet
  wp_owner plugin install classic-widgets --version=0.2 --activate --quiet
  as_owner curl -fsSL -o /tmp/cookie.zip https://github.com/kiritoshiro/wp-cookie-consent/releases/download/v1.0.11/wp-cookie-consent.zip
  wp_owner plugin install /tmp/cookie.zip --activate --quiet
  wp_owner language core install lt_LT --quiet
  wp_owner rewrite structure "/%postname%/" --quiet
  wp_owner post create --post_title="Hello" --post_status=publish --post_category=1 --quiet
  as_owner mkdir -p "$ROOT/wp-content/mu-plugins"
  as_owner cp /src/tests/disposable/break-after-update.php "$ROOT/wp-content/mu-plugins/"
  as_owner touch "$ROOT/.disposable-ready"
fi

# Site Watch, root-owned, as in production.
install -d -o root -g root -m 0755 /opt/site-watch
rm -rf /opt/site-watch/sitewatch && cp -r /src/sitewatch /opt/site-watch/ && chown -R root:root /opt/site-watch
find /opt/site-watch -type d -exec chmod 0755 {} + && find /opt/site-watch -type f -exec chmod 0644 {} +
install -d -o root -g sitewatch -m 0750 /var/lib/site-watch
cat > /etc/site-watch/collector.json <<CONF
{"server_id": "disposable", "origin_ip": "127.0.0.1", "ca_file": "/etc/site-watch/test-ca.pem",
 "github": {"plugin:adventistai-cookie-consent": "kiritoshiro/wp-cookie-consent"},
 "sites": [{"id": "$SITE", "owner": "$OWNER", "path": "$ROOT", "health_paths": ["/", "/wp-login.php", "/category/uncategorized/"]}]}
CONF
chmod 0600 /etc/site-watch/collector.json
install -o root -g root -m 0755 /src/deploy/site-update /usr/local/sbin/site-update
install -o root -g root -m 0440 /src/deploy/site-update.sudoers /etc/sudoers.d/site-update
visudo -cf /etc/sudoers.d/site-update >/dev/null
(cd /opt/site-watch && python3 -m sitewatch.collector >/dev/null)
echo "setup done: $(wp_owner core version), status.json $(stat -c '%U:%G %a' /var/lib/site-watch/status.json)"
