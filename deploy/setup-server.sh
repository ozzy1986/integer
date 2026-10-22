#!/usr/bin/env bash
# Idempotent server setup, called by remote.sh: secrets file, directories, Apache vhosts, certificate.
set -euo pipefail
DOMAIN=integer.ozzy1986.com
ROOT=/var/www/$DOMAIN
HERE=$(cd "$(dirname "$0")" && pwd)

mkdir -p "$ROOT/acme" "$ROOT/releases" "$ROOT/shared"
chmod 755 "$ROOT" "$ROOT/acme" "$ROOT/releases"
chmod 700 "$ROOT/shared"

# Secrets are generated once on the server and never leave it.
if [ ! -f "$ROOT/shared/.env" ]; then
  secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
  umask 077
  cat > "$ROOT/shared/.env" <<EOF
POSTGRES_PASSWORD=$(secret)
ALPHA_SECRET=$(secret)
BETA_SECRET=$(secret)
PAYMENTS_SECRET=$(secret)
FAKE_CONTROL_TOKEN=$(secret)
APP_BIND=127.0.0.1
APP_PORT=8121
PROVIDER_TIMEOUT=60
EOF
  echo "generated $ROOT/shared/.env"
fi
chmod 600 "$ROOT/shared/.env"

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
