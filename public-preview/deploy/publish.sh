#!/usr/bin/env bash
# Publish the PhotoDoc AI informational site — fail closed.
#
# Every precondition is verified before the active configuration changes, the candidate
# config is validated with `nginx -t` before reload, and any post-reload mismatch rolls
# back automatically. A missing safety marker, an unexpected HTTP code or a failed check
# always produces a nonzero exit code.
#
# Paths and binaries can be overridden for isolated simulations; production defaults are
# used when the variables are unset.
set -euo pipefail

SITE_ROOT="${SITE_ROOT:-/srv/photodoc/public-preview}"
SITE_CONF_SRC="${SITE_CONF_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/preview.conf}"
SITE_CONF_DST="${SITE_CONF_DST:-/etc/nginx/sites-available/photodoc-preview}"
LINK="${LINK:-/etc/nginx/sites-enabled/photodoc}"
BLOCKED_SITE="${BLOCKED_SITE:-/etc/nginx/sites-available/photodoc.tls}"
MARKER="${MARKER:-/srv/photodoc/DEPLOYMENT_BLOCKED}"
BACKUP_ROOT="${BACKUP_ROOT:-/root}"
PUBLIC_HOST="${PUBLIC_HOST:-photo1storage.ru}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
NGINX_BIN="${NGINX_BIN:-nginx}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
PUBLIC_PAGES=(/ /services /contacts /offer /privacy /refund)
FORBIDDEN=(/api/health /api/payment/create /api/payment/robokassa/result /order-status /admin /uploads/1_x.jpg)

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_ROOT/preview-publish-$STAMP"
PREVIOUS_TARGET=""
PREVIOUS_BACKUP_DIR=""
SWITCHED=0

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

http_code() {
    "$CURL_BIN" -sS -o /dev/null -m 10 -w '%{http_code}' \
        --resolve "${PUBLIC_HOST}:443:${PUBLIC_IP}" "https://${PUBLIC_HOST}$1" || echo "000"
}

http_code_http() {
    "$CURL_BIN" -sS -o /dev/null -m 10 -w '%{http_code}' \
        --resolve "${PUBLIC_HOST}:80:${PUBLIC_IP}" "http://${PUBLIC_HOST}$1" || echo "000"
}

post_code() {
    "$CURL_BIN" -sS -o /dev/null -m 10 -w '%{http_code}' -X POST \
        --resolve "${PUBLIC_HOST}:443:${PUBLIC_IP}" "https://${PUBLIC_HOST}$1" || echo "000"
}

expect_code() { # label path expected
    local code
    code=$(http_code "$2")
    [ "$code" = "$3" ] || { log "  $1 $2 -> $code (expected $3)"; return 1; }
    log "  $1 $2 -> $code"
    return 0
}

verify_blocked() {
    expect_code "blocked" "/" 503 || return 1
    expect_code "blocked" "/services" 503 || return 1
    expect_code "blocked" "/api/health" 503 || return 1
    return 0
}

verify_published() {
    local page
    for page in "${PUBLIC_PAGES[@]}"; do
        expect_code "page" "$page" 200 || return 1
    done
    expect_code "asset" "/assets/styles.css" 200 || return 1
    for page in "${FORBIDDEN[@]}"; do
        expect_code "forbidden" "$page" 404 || return 1
    done
    local code
    code=$(post_code "/services")
    [ "$code" = "403" ] || { log "  POST /services -> $code (expected 403)"; return 1; }
    log "  POST /services -> $code"
    code=$(http_code_http "/")
    [ "$code" = "301" ] || { log "  http / -> $code (expected 301)"; return 1; }
    log "  http / -> $code"
    return 0
}

restore_previous() {
    if [ -n "$PREVIOUS_TARGET" ]; then
        ln -sfn "$PREVIOUS_TARGET" "$LINK"
    else
        rm -f "$LINK"
    fi
    "$NGINX_BIN" -t >/dev/null 2>&1 || true
    "$SYSTEMCTL_BIN" reload nginx >/dev/null 2>&1 || true
}

on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$SWITCHED" -eq 1 ]; then
        log "Rolling back to the previous configuration after a failed check"
        restore_previous
    fi
    exit "$status"
}
trap on_exit EXIT

log "== 1. preconditions (no changes yet) =="
[ -f "$MARKER" ] || die "DEPLOYMENT_BLOCKED is missing at $MARKER; refusing to publish"
log "  safety marker present"
[ -f "$SITE_CONF_SRC" ] || die "candidate nginx config not found: $SITE_CONF_SRC"
for page in index services contacts offer privacy refund; do
    [ -s "$SITE_ROOT/$page.html" ] || die "missing or empty page: $SITE_ROOT/$page.html"
done
[ -s "$SITE_ROOT/assets/styles.css" ] || die "missing stylesheet in $SITE_ROOT"
log "  six pages and the stylesheet are present"
[ -f "$BLOCKED_SITE" ] && grep -q 'return 503' "$BLOCKED_SITE" \
    || die "expected blocked site $BLOCKED_SITE with a 503 response is missing"
log "  blocked configuration available"

PREVIOUS_TARGET=$(readlink -f "$LINK" 2>/dev/null || true)
[ -n "$PREVIOUS_TARGET" ] || die "active site link $LINK does not resolve; unexpected state"
[ "$PREVIOUS_TARGET" = "$BLOCKED_SITE" ] \
    || die "active site is not the blocked configuration ($PREVIOUS_TARGET); refusing to publish"
log "  active site is the blocked configuration"

"$NGINX_BIN" -t >/dev/null 2>&1 || die "current nginx configuration does not pass nginx -t"
log "  current nginx configuration is valid"
verify_blocked || die "current public state is not the expected 503; refusing to publish"

log "== 2. backup =="
mkdir -p "$BACKUP" || die "cannot create backup directory $BACKUP"
cp -a "$LINK" "$BACKUP/photodoc.link" 2>/dev/null || die "cannot back up the active link"
printf '%s\n' "$PREVIOUS_TARGET" > "$BACKUP/previous-target.txt" || die "cannot record the previous target"
cp -a "$PREVIOUS_TARGET" "$BACKUP/previous-site.conf" || die "cannot back up the previous site file"
[ -s "$BACKUP/previous-site.conf" ] || die "backup of the previous site is empty"
log "  backup written to $BACKUP"

log "== 3. install the candidate configuration =="
cp -f "$SITE_CONF_SRC" "$SITE_CONF_DST" || die "cannot install $SITE_CONF_DST"
chmod 644 "$SITE_CONF_DST" || die "cannot set permissions on $SITE_CONF_DST"

log "== 4. switch and validate =="
ln -sfn "$SITE_CONF_DST" "$LINK"
SWITCHED=1
if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
    "$NGINX_BIN" -t || true
    die "candidate configuration failed nginx -t; previous configuration restored"
fi
log "  candidate configuration passes nginx -t"

"$SYSTEMCTL_BIN" reload nginx || die "nginx reload failed"

log "== 5. post-deployment verification =="
if ! verify_published; then
    die "public verification failed; previous configuration will be restored"
fi
SWITCHED=0
log "== published successfully =="
log "  backup: $BACKUP"
