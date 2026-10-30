# Disposable server with three web apps for site-update's app updates. Never point this at production.
# Build tests/disposable first (it is the base image), then see tests/disposable/apps/README.md.
FROM sitewatch-disposable:latest
RUN apt-get update && apt-get install -y --no-install-recommends git php8.4-bcmath php8.4-sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/composer https://getcomposer.org/download/2.8.12/composer.phar \
    && chmod 0755 /usr/local/bin/composer
