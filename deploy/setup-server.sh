#!/usr/bin/env bash
# Idempotent server setup, called by remote.sh: secrets file, directories, Apache vhosts, certificate.
set -euo pipefail
DOMAIN=integer.ozzy1986.com
ROOT=/var/www/$DOMAIN
HERE=$(cd "$(dirname "$0")" && pwd)

mkdir -p "$ROOT/acme" "$ROOT/releases" "$ROOT/shared"
chmod 755 "$ROOT" "$ROOT/acme" "$ROOT/releases"
chmod 700 "$ROOT/shared"

# Secrets are generated once on the server and never leave it. A setting introduced by a later release is
# appended to the existing file, so redeploying an old server just adds what is missing.
ENV_FILE=$ROOT/shared/.env
(umask 077 && touch "$ENV_FILE")
chmod 600 "$ENV_FILE"
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
ensure() { grep -q "^$1=" "$ENV_FILE" || { echo "$1=$2" >> "$ENV_FILE"; echo "added $1 to $ENV_FILE"; }; }
ensure POSTGRES_PASSWORD "$(secret)"
ensure APP_DB_PASSWORD "$(secret)"
ensure ALPHA_SECRET "$(secret)"
ensure BETA_SECRET "$(secret)"
ensure PAYMENTS_SECRET "$(secret)"
ensure FAKE_CONTROL_TOKEN "$(secret)"
ensure APP_BIND 127.0.0.1
ensure APP_PORT 8121
ensure PROVIDER_TIMEOUT 60
ensure RATE_LIMIT_PER_MINUTE 60

install -m 644 "$HERE/apache/$DOMAIN.conf" "/etc/apache2/sites-available/$DOMAIN.conf"
a2ensite -q "$DOMAIN"
if ! certbot certificates 2>/dev/null | grep -q "Certificate Name: $DOMAIN"; then
  apache2ctl configtest && systemctl reload apache2
  certbot certonly --webroot -w "$ROOT/acme" -d "$DOMAIN" --non-interactive --agree-tos
fi
install -m 644 "$HERE/apache/$DOMAIN-le-ssl.conf" "/etc/apache2/sites-available/$DOMAIN-le-ssl.conf"
a2ensite -q "$DOMAIN-le-ssl"
apache2ctl configtest
systemctl reload apache2
