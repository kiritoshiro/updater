# Disposable WordPress server for site-update acceptance tests. Never point this at production.
# Usage: see tests/disposable/README.md.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# PHP 8.4 from the ondrej PPA, as on the production server.
RUN apt-get update && apt-get install -y --no-install-recommends software-properties-common gpg-agent \
    && add-apt-repository -y ppa:ondrej/php && apt-get update && apt-get install -y --no-install-recommends \
      nginx php8.4-fpm php8.4-cli php8.4-mysql php8.4-xml php8.4-curl php8.4-mbstring php8.4-zip php8.4-gd \
      php8.4-intl mariadb-server mariadb-client sudo curl ca-certificates openssl python3 unzip less tar openssh-server socat \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /usr/local/bin/wp https://github.com/wp-cli/wp-cli/releases/download/v2.12.0/wp-cli-2.12.0.phar \
    && chmod 0755 /usr/local/bin/wp
CMD ["sleep", "infinity"]
