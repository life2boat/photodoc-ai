#!/usr/bin/env bash
# Roll the public site back to the previous state (the maintenance 503 page).
# NOT executed by the agent — requires the owner's explicit approval.
set -euo pipefail

LINK=/etc/nginx/sites-enabled/photodoc
BACKUP=$(ls -d /root/preview-publish-* 2>/dev/null | tail -1 || true)

echo "== 1. restore the previous site file from the most recent publish backup =="
if [ -n "${BACKUP}" ] && [ -f "$BACKUP/previous-target.txt" ]; then
  PREV=$(cat "$BACKUP/previous-target.txt")
  echo "restoring: $PREV"
  ln -sfn "$PREV" "$LINK"
else
  # Fall back to the maintenance (HTTP/TLS 503) site installed during the HTTPS step.
  echo "no publish backup found; falling back to the maintenance site"
  ln -sfn /etc/nginx/sites-available/photodoc.tls "$LINK"
fi

echo "== 2. test and reload =="
nginx -t
systemctl reload nginx

echo "== 3. verify the site is closed again =="
for path in / /services /api/health; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --resolve "photo1storage.ru:443:127.0.0.1" "https://photo1storage.ru$path")
  echo "  $path -> $code (expected 503)"
done

echo "== 4. the published files may stay on disk =="
echo "  they are unreachable while the maintenance site is enabled"
echo "done"
