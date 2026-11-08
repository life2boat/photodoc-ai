#!/usr/bin/env bash
# Isolated simulations for publish.sh and rollback.sh.
#
# Builds a temporary sandbox with fake nginx/systemctl/curl and exercises the fail-closed
# paths. The fake curl derives the served state from the active site link, so switching the
# link changes the simulated public behaviour. Nothing outside the temporary directory is
# touched. Exit code is nonzero when any scenario does not behave as required.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PUBLISH="$HERE/../deploy/publish.sh"
ROLLBACK="$HERE/../deploy/rollback.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<'FAKE'
#!/usr/bin/env bash
link=$(readlink -f "$SIM_LINK" 2>/dev/null || echo "")
target=""
for arg in "$@"; do
    case "$arg" in
        https://*|http://*) target="$arg" ;;
    esac
done
path=$(printf '%s' "$target" | sed -E 's#^https?://[^/]+##; s#\?.*$##')
method=GET
prev=""
for arg in "$@"; do
    [ "$prev" = "-X" ] && method="$arg"
    prev="$arg"
done

case "$target" in
    http://*) printf '301'; exit 0 ;;
esac
case "$link" in
    *candidate.conf)
        code=404
        case "$path" in
            /|/services|/contacts|/offer|/privacy|/refund|/assets/styles.css) code=200 ;;
        esac
        [ "$method" = "POST" ] && code=403
        if [ -f "$SIM_FORCE_LEAK" ]; then
            case "$path" in
                /api/health|/api/payment/create|/order-status|/admin) code=200 ;;
            esac
        fi
        ;;
    *) code=503 ;;
esac
[ -f "$SIM_FORCE_200" ] && code=200
printf '%s' "$code"
FAKE

cat > "$WORK/bin/nginx" <<'FAKE'
#!/usr/bin/env bash
if [ "$*" = "-t" ] && [ -f "$SIM_NGINX_FAIL" ]; then
    echo "nginx: configuration file test failed" >&2
    exit 1
fi
exit 0
FAKE

cat > "$WORK/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
chmod +x "$WORK/bin/"*

make_sandbox() { # $1 = initial link target name (blocked|candidate), $2 = marker (yes|no)
    SB="$WORK/sandbox-$RANDOM"
    mkdir -p "$SB/site/assets" "$SB/nginx/sites-available" "$SB/nginx/sites-enabled" "$SB/backups"
    for page in index services contacts offer privacy refund; do
        printf '<html>%s</html>\n' "$page" > "$SB/site/$page.html"
    done
    printf 'body{}\n' > "$SB/site/assets/styles.css"
    printf 'server { return 503; }\n' > "$SB/nginx/sites-available/photodoc.tls"
    cp "$HERE/../deploy/preview.conf" "$SB/nginx/sites-available/candidate.conf"
    case "$1" in
        blocked) ln -sfn "$SB/nginx/sites-available/photodoc.tls" "$SB/nginx/sites-enabled/photodoc" ;;
        *) ln -sfn "$SB/nginx/sites-available/candidate.conf" "$SB/nginx/sites-enabled/photodoc" ;;
    esac
    [ "$2" = "yes" ] && printf 'blocked\n' > "$SB/DEPLOYMENT_BLOCKED"
}

run_publish() {
    SIM_LINK="$SB/nginx/sites-enabled/photodoc" SIM_NGINX_FAIL="${SIM_NGINX_FAIL:-}" \
    SIM_FORCE_LEAK="${SIM_FORCE_LEAK:-}" SIM_FORCE_200="${SIM_FORCE_200:-}" \
    SITE_ROOT="$SB/site" SITE_CONF_SRC="$HERE/../deploy/preview.conf" \
    SITE_CONF_DST="$SB/nginx/sites-available/candidate.conf" \
    LINK="$SB/nginx/sites-enabled/photodoc" \
    BLOCKED_SITE="$SB/nginx/sites-available/photodoc.tls" \
    MARKER="$SB/DEPLOYMENT_BLOCKED" BACKUP_ROOT="$SB/backups" \
    NGINX_BIN="$WORK/bin/nginx" SYSTEMCTL_BIN="$WORK/bin/systemctl" CURL_BIN="$WORK/bin/curl" \
    PUBLIC_IP=127.0.0.1 \
    bash "$PUBLISH" > "$SB/log" 2>&1
}

run_rollback() {
    SIM_LINK="$SB/nginx/sites-enabled/photodoc" SIM_NGINX_FAIL="${SIM_NGINX_FAIL:-}" \
    SIM_FORCE_LEAK="${SIM_FORCE_LEAK:-}" SIM_FORCE_200="${SIM_FORCE_200:-}" \
    LINK="$SB/nginx/sites-enabled/photodoc" \
    BLOCKED_SITE="$SB/nginx/sites-available/photodoc.tls" \
    MARKER="$SB/DEPLOYMENT_BLOCKED" \
    NGINX_BIN="$WORK/bin/nginx" SYSTEMCTL_BIN="$WORK/bin/systemctl" CURL_BIN="$WORK/bin/curl" \
    PUBLIC_IP=127.0.0.1 \
    bash "$ROLLBACK" > "$SB/log" 2>&1
}

target() { readlink -f "$SB/nginx/sites-enabled/photodoc" 2>/dev/null || true; }

# ---------------------------------------------------------------- H2: missing marker
make_sandbox blocked no
if run_publish; then bad "publish must fail when DEPLOYMENT_BLOCKED is missing"; else ok "publish fails closed without the safety marker"; fi
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "link unchanged after marker failure" || bad "link changed after marker failure"

# ---------------------------------------------------------------- H2: API leaks after publish
make_sandbox blocked yes
printf 'leak\n' > "$SB/force-leak"
SIM_FORCE_LEAK="$SB/force-leak" run_publish && bad "publish must fail when a forbidden endpoint returns 200" || ok "publish fails closed on a forbidden 200"
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "link restored to blocked after leak" || bad "link left on the candidate after leak"

# ---------------------------------------------------------------- H2: unexpected initial state
make_sandbox candidate yes
if run_publish; then bad "publish must fail when the site is already public"; else ok "publish fails closed on an unexpected initial state"; fi

# ---------------------------------------------------------------- H3: nginx -t failure
make_sandbox blocked yes
printf 'fail\n' > "$SB/nginx-fail"
SIM_NGINX_FAIL="$SB/nginx-fail" run_publish && bad "publish succeeded despite nginx -t failure" || ok "publish fails closed when nginx -t rejects the candidate"
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "link restored after nginx -t failure" || bad "link left on the failing candidate"

# ---------------------------------------------------------------- happy path
make_sandbox blocked yes
if run_publish; then ok "publish succeeds on a healthy blocked state"; else bad "publish failed on a healthy state"; fi
[ "$(target)" = "$SB/nginx/sites-available/candidate.conf" ] && ok "link switched to the published site" || bad "link not switched on success"

# ---------------------------------------------------------------- H1: rollback ignores backups
make_sandbox candidate yes
mkdir -p "$SB/backups/preview-publish-test"
printf 'server { proxy_pass http://backend:8000; }\n' > "$SB/backups/preview-publish-test/previous-site.conf"
printf '%s\n' "$SB/backups/preview-publish-test/previous-site.conf" > "$SB/backups/preview-publish-test/previous-target.txt"
if run_rollback; then ok "rollback succeeds with a stray backup present"; else bad "rollback failed on a normal published state"; fi
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "rollback ignored the backup and used the blocked target" || bad "rollback restored an arbitrary backup target"

# ---------------------------------------------------------------- H1: rollback never returns 200
make_sandbox candidate yes
printf 'force\n' > "$SB/force-200"
SIM_FORCE_200="$SB/force-200" run_rollback && bad "rollback must fail when the result is not 503" || ok "rollback fails closed when verification returns 200"

# ---------------------------------------------------------------- idempotency
make_sandbox blocked yes
if run_rollback; then ok "rollback is idempotent when already blocked"; else bad "rollback failed when already blocked"; fi

# ---------------------------------------------------------------- blocked target with proxy_pass
make_sandbox candidate yes
printf 'server { proxy_pass http://backend:8000; return 503; }\n' > "$SB/nginx/sites-available/photodoc.tls"
if run_rollback; then bad "rollback must refuse a target containing proxy_pass"; else ok "rollback refuses a target with proxy_pass"; fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
