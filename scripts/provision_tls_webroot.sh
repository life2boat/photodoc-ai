#!/usr/bin/env bash
# Provision the ACME HTTP-01 webroot required by
# infra/nginx/photodoc-http-bootstrap.conf and infra/nginx/photodoc-https.conf.
#
# Idempotent. Safe to re-run. Creates only the challenge directory tree.
set -euo pipefail

WEBROOT="${WEBROOT:-/var/www/certbot}"
OWNER="${OWNER:-root:www-data}"
MODE="${MODE:-755}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run as root (needs to create ${WEBROOT})" >&2
  exit 1
fi

install -d -m "${MODE}" -o "${OWNER%%:*}" -g "${OWNER##*:}" \
  "${WEBROOT}" \
  "${WEBROOT}/.well-known" \
  "${WEBROOT}/.well-known/acme-challenge"

# certbot writes tokens here; nginx only needs read access.
chown -R "${OWNER%%:*}:${OWNER##*:}" "${WEBROOT}"
chmod -R "${MODE}" "${WEBROOT}"

echo "webroot ready: ${WEBROOT}"
find "${WEBROOT}" -maxdepth 3 -printf '%M %u:%g %p\n'
