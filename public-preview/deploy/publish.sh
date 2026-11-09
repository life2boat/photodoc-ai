#!/usr/bin/env bash
# Publish the informational preview on the VPS.
# NOT executed by the agent — requires the owner's explicit approval.
#
# Scope: copies static files and switches ONE nginx site file. It does not touch the
# backend, the database, compose files, DNS, TLS or DEPLOYMENT_BLOCKED.
set -euo pipefail

SITE_DIR=/srv/photodoc/public-preview
SITE_CONF=/etc/nginx/sites-available/photodoc-preview
LINK=/etc/nginx/sites-enabled/photodoc
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/root/preview-publish-$STAMP

echo "== 1. safety checks =="
test -f /srv/photodoc/DEPLOYMENT_BLOCKED \
  && echo "DEPLOYMENT_BLOCKED present (left untouched by this script)" \
  || echo "WARNING: marker absent"

echo "== 2. back up the current site file =="
mkdir -p "$BACKUP"
if [ -L "$LINK" ] || [ -f "$LINK" ]; then
  cp -a "$LINK" "$BACKUP/photodoc.link" 2>/dev/null || true
  readlink -f "$LINK" > "$BACKUP/previous-target.txt" 2>/dev/null || true
  echo "previous target: $(cat "$BACKUP/previous-target.txt" 2>/dev/null || echo unknown)"
fi
echo "backup dir: $BACKUP"

echo "== 3. copy the static site =="
mkdir -p "$SITE_DIR"
cp -a public-preview/index.html public-preview/services.html \
      public-preview/contacts.html public-preview/offer.html \
      public-preview/privacy.html public-preview/refund.html "$SITE_DIR/"
mkdir -p "$SITE_DIR/assets"
cp -a public-preview/assets/styles.css "$SITE_DIR/assets/"
chown -R root:root "$SITE_DIR"
chmod -R a+rX "$SITE_DIR"
ls -la "$SITE_DIR"

echo "== 4. install and enable the site config =="
install -m 644 public-preview/deploy/preview.conf "$SITE_CONF"
ln -sfn "$SITE_CONF" "$LINK"
nginx -t
systemctl reload nginx

echo "== 5. verify =="
for path in / /services /contacts /offer /privacy /refund; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --resolve "photo1storage.ru:443:127.0.0.1" "https://photo1storage.ru$path")
  echo "  $path -> $code"
done
for path in /api/health /api/payment/create /order-status /admin; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --resolve "photo1storage.ru:443:127.0.0.1" "https://photo1storage.ru$path")
  echo "  $path -> $code (expected 404)"
done

echo "done. rollback: deploy/rollback.sh"
