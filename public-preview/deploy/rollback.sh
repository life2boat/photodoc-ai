#!/usr/bin/env bash
# Roll the public site back to the blocked HTTPS 503 configuration — fail closed.
#
# Only the approved maintenance configuration is a trusted target. Recovery after a failed
# rollback never reactivates a partially published site blindly and never restores a
# configuration that can proxy to the backend:
#
#   * a previous configuration is restored only when it passes a structural safety check
#     (regular file inside the approved directory, no active proxy_pass/fastcgi_pass/
#     uwsgi_pass/scgi_pass/grpc_pass, no include that could pull one in);
#   * every recovery step is validated against the public entry point — either HTTPS 503 or
#     "no backend exposure" (forbidden API routes must never answer 200);
#   * a failed nginx reload is reported as an unknown loaded state, never as success;
#   * the script exits nonzero whenever the blocked state cannot be confirmed.
set -euo pipefail

LINK="${LINK:-/etc/nginx/sites-enabled/photodoc}"
BLOCKED_SITE="${BLOCKED_SITE:-/etc/nginx/sites-available/photodoc.tls}"
MARKER="${MARKER:-/srv/photodoc/DEPLOYMENT_BLOCKED}"
PUBLIC_HOST="${PUBLIC_HOST:-photo1storage.ru}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
NGINX_BIN="${NGINX_BIN:-nginx}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
SITE_ROOT="${SITE_ROOT:-/srv/photodoc/public-preview}"
BLOCKED_PATHS=(/ /services /api/health)
FORBIDDEN_PATHS=(/api/health /api/payment/create /api/payment/robokassa/result /order-status /admin /uploads/1_x.jpg)

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

verify_no_backend_exposure() {
    local path code
    for path in "${FORBIDDEN_PATHS[@]}"; do
        code=$(http_code "$path")
        # M-04: a redirect is not proof of isolation — only 404/503 are acceptable here.
        case "$code" in
            404|503) log "  $path -> $code (not exposed)" ;;
            *) log "  $path -> $code (exposure or redirect; refusing)"; return 1 ;;
        esac
    done
    return 0
}

# Structural safety of a candidate recovery configuration. This is deliberately stricter
# than a text search: it rejects every directive that can hand a request to another
# process, and rejects includes, because an included file could carry such a directive.
is_safe_config() {
    local candidate="$1" resolved config_dir lineno line
    [ -n "$candidate" ] || return 1
    [ -e "$candidate" ] || return 1
    resolved=$(readlink -f "$candidate" 2>/dev/null || true)
    [ -n "$resolved" ] || return 1
    [ -f "$resolved" ] || return 1
    config_dir=$(dirname "$(readlink -f "$BLOCKED_SITE" 2>/dev/null || printf '%s' "$BLOCKED_SITE")")
    case "$resolved" in
        "$config_dir"/*) ;;
        *) return 1 ;;
    esac
    grep -Eq '(^|[[:space:]])(server[[:space:]]*\{)' "$resolved" || return 1
    while IFS= read -r line; do
        case "$line" in
            \#*) continue ;;
        esac
        if printf '%s\n' "$line" | grep -Eq '(^|[[:space:]])(proxy_pass|fastcgi_pass|uwsgi_pass|scgi_pass|grpc_pass|include|alias)[[:space:]]'; then
            return 1
        fi
        # M-04: a root outside the published site directory would expose arbitrary paths.
        if printf '%s\n' "$line" | grep -Eq '(^|[[:space:]])root[[:space:]]+[^;]+;'; then
            local root_path
            root_path=$(printf '%s\n' "$line" | sed -E 's/.*root[[:space:]]+([^;]+);.*/\1/')
            # Only the deployment tree may be served; anything else (/etc, /var, ...) is
            # an arbitrary file read and must never be reactivated.
            case "$root_path" in
                /srv/photodoc/*) ;;          # deployment tree
                /var/www/certbot) ;;         # ACME webroot only
                *) return 1 ;;
            esac
        fi
    done < "$resolved"
    return 0
}

PREVIOUS_TARGET=""
CHANGED=0
RECOVERED=0

reload_nginx() {
    if ! "$SYSTEMCTL_BIN" reload nginx; then
        log "  nginx reload failed; the loaded configuration is unknown"
        return 1
    fi
    return 0
}

# Fail-closed recovery. Never restores an unsafe configuration.
recover() {
    [ "$RECOVERED" -eq 1 ] && return 0
    RECOVERED=1

    log "== recovery: restoring the blocked configuration =="
    ln -sfn "$BLOCKED_SITE" "$LINK"
    if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
        "$NGINX_BIN" -t || true
        log "  the blocked configuration does not pass nginx -t; nothing was activated"
        return 0
    fi
    if ! reload_nginx; then
        log "  blocked state is on disk but not confirmed in nginx"
        return 0
    fi
    if verify_blocked; then
        log "  blocked state confirmed"
        return 0
    fi

    log "== recovery: blocked configuration does not answer 503 =="
    if is_safe_config "$PREVIOUS_TARGET"; then
        log "  previous configuration passed the safety check: $PREVIOUS_TARGET"
        ln -sfn "$PREVIOUS_TARGET" "$LINK"
        if "$NGINX_BIN" -t >/dev/null 2>&1 && reload_nginx && verify_no_backend_exposure; then
            log "  published site restored; backend is not exposed; blocked state NOT confirmed"
            return 0
        fi
    else
        log "  previous configuration is missing, outside the approved directory or can proxy to the backend; it was NOT restored"
    fi

    log "  leaving the blocked configuration on disk; the effective state is unknown"
    ln -sfn "$BLOCKED_SITE" "$LINK"
    "$NGINX_BIN" -t >/dev/null 2>&1 || true
    return 0
}

on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$CHANGED" -eq 1 ]; then
        recover || true
    fi
    exit "$status"
}
trap on_exit EXIT

log "== 1. preconditions =="
[ -f "$MARKER" ] || die "DEPLOYMENT_BLOCKED is missing at $MARKER; refusing to change the site"
log "  safety marker present"

[ -f "$BLOCKED_SITE" ] || die "blocked configuration not found: $BLOCKED_SITE"
grep -q 'return 503' "$BLOCKED_SITE" || die "$BLOCKED_SITE does not contain a 503 response"
is_safe_config "$BLOCKED_SITE" || die "$BLOCKED_SITE contains backend proxying or an include; refusing to use it"
log "  blocked configuration verified (503, no backend proxying)"

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
    die "blocked configuration failed nginx -t"
fi
log "  nginx -t passed"

reload_nginx || die "nginx reload failed; the blocked state is not confirmed"

log "== 3. verify the blocked state from the public entry point =="
if ! verify_blocked; then
    die "rollback verification failed"
fi
CHANGED=0
log "== rolled back: HTTPS 503 confirmed =="
