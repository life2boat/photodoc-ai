#!/usr/bin/env bash
# Roll the public site back to the blocked HTTPS 503 configuration — fail closed.
#
# Only one target is accepted: the known maintenance site that answers 503. The script
# never follows a previous-target file and never restores an arbitrary configuration, so a
# stray backup cannot reopen the backend. Success is asserted from the public entry point:
# every check must return 503, otherwise the previous link is restored and the script exits
# nonzero.
set -euo pipefail

LINK="${LINK:-/etc/nginx/sites-enabled/photodoc}"
BLOCKED_SITE="${BLOCKED_SITE:-/etc/nginx/sites-available/photodoc.tls}"
MARKER="${MARKER:-/srv/photodoc/DEPLOYMENT_BLOCKED}"
PUBLIC_HOST="${PUBLIC_HOST:-photo1storage.ru}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
NGINX_BIN="${NGINX_BIN:-nginx}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
BLOCKED_PATHS=(/ /services /api/health)

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

http_code() {
    "$CURL_BIN" -sS -o /dev/null -m 10 -w '%{http_code}' \
        --resolve "${PUBLIC_HOST}:443:${PUBLIC_IP}" "https://${PUBLIC_HOST}$1" || echo "000"
}

verify_blocked() {
    local path code
    for path in "${BLOCKED_PATHS[@]}"; do
        code=$(http_code "$path")
        [ "$code" = "503" ] || { log "  $path -> $code (expected 503)"; return 1; }
        log "  $path -> $code"
    done
    return 0
}

PREVIOUS_TARGET=""
CHANGED=0

restore_previous() {
    if [ -n "$PREVIOUS_TARGET" ]; then
        ln -sfn "$PREVIOUS_TARGET" "$LINK"
    fi
    "$NGINX_BIN" -t >/dev/null 2>&1 || true
    "$SYSTEMCTL_BIN" reload nginx >/dev/null 2>&1 || true
}

on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$CHANGED" -eq 1 ]; then
        log "Restoring the previous link after a failed check"
        restore_previous
    fi
    exit "$status"
}
trap on_exit EXIT

log "== 1. preconditions =="
[ -f "$MARKER" ] || die "DEPLOYMENT_BLOCKED is missing at $MARKER; refusing to change the site"
log "  safety marker present"

[ -f "$BLOCKED_SITE" ] || die "blocked configuration not found: $BLOCKED_SITE"
grep -q 'return 503' "$BLOCKED_SITE" || die "$BLOCKED_SITE does not contain a 503 response"
if grep -Eq '(^|[[:space:]])proxy_pass[[:space:]]' "$BLOCKED_SITE"; then
    die "$BLOCKED_SITE contains proxy_pass; refusing to use it as the blocked target"
fi
log "  blocked configuration verified (503, no proxy_pass)"

PREVIOUS_TARGET=$(readlink -f "$LINK" 2>/dev/null || true)
log "  current target: ${PREVIOUS_TARGET:-<none>}"

if [ "$PREVIOUS_TARGET" = "$BLOCKED_SITE" ]; then
    log "== already rolled back; verifying =="
    verify_blocked || die "site is already on the blocked configuration but does not answer 503"
    log "== nothing to do =="
    exit 0
fi

"$NGINX_BIN" -t >/dev/null 2>&1 || die "current nginx configuration does not pass nginx -t"

log "== 2. switch to the blocked configuration =="
ln -sfn "$BLOCKED_SITE" "$LINK"
CHANGED=1
if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
    "$NGINX_BIN" -t || true
    die "blocked configuration failed nginx -t; previous link restored"
fi
log "  nginx -t passed"

"$SYSTEMCTL_BIN" reload nginx || die "nginx reload failed"

log "== 3. verify the blocked state from the public entry point =="
if ! verify_blocked; then
    die "rollback verification failed; previous link will be restored"
fi
CHANGED=0
log "== rolled back: HTTPS 503 confirmed =="
