#!/usr/bin/env bash
# Isolated simulations for publish.sh and rollback.sh (including the fail-closed recovery).
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC="$HERE/.."
PUBLISH="$SRC/deploy/publish.sh"
ROLLBACK="$SRC/deploy/rollback.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$1"; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<'FAKE'
#!/usr/bin/env bash
link=$(readlink -f "$SIM_LINK" 2>/dev/null || echo "")
target=""
for a in "$@"; do case "$a" in https://*|http://*) target="$a";; esac; done
path=$(printf '%s' "$target" | sed -E 's#^https?://[^/]+##; s#\?.*$##')
method=GET; prev=""
for a in "$@"; do [ "$prev" = "-X" ] && method="$a"; prev="$a"; done
case "$target" in http://*) printf '301'; exit 0;; esac
case "$link" in
  *candidate.conf)
    code=404
    case "$path" in /|/services|/contacts|/offer|/privacy|/refund|/assets/styles.css) code=200;; esac
    [ "$method" = "POST" ] && code=403
    ;;
  *photodoc.tls)
    if [ -f "$SIM_BROKEN_BLOCKED" ]; then code=200; else code=503; fi
    ;;
  *) code=503;;
esac
[ -f "$SIM_FORCE_200" ] && code=200
printf '%s' "$code"
FAKE
cat > "$WORK/bin/nginx" <<'FAKE'
#!/usr/bin/env bash
[ "$*" = "-t" ] && [ -f "$SIM_NGINX_FAIL" ] && { echo "nginx: test failed" >&2; exit 1; }
exit 0
FAKE
cat > "$WORK/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
[ -f "$SIM_SYSTEMCTL_FAIL" ] && exit 1
exit 0
FAKE
chmod +x "$WORK/bin/"*

sandbox() { # $1 = blocked|candidate|proxy
  SB="$WORK/sb-$RANDOM"
  mkdir -p "$SB/site/assets" "$SB/nginx/sites-available" "$SB/nginx/sites-enabled" "$SB/backups"
  for p in index services contacts offer privacy refund; do printf 'x' > "$SB/site/$p.html"; done
  printf 'b' > "$SB/site/assets/styles.css"
  printf 'server { return 503; }\n' > "$SB/nginx/sites-available/photodoc.tls"
  printf 'server { proxy_pass http://backend:8000; }\n' > "$SB/nginx/sites-available/backend.conf"
  cp "$SRC/deploy/preview.conf" "$SB/nginx/sites-available/candidate.conf"
  case "$1" in
    blocked) ln -sfn "$SB/nginx/sites-available/photodoc.tls" "$SB/nginx/sites-enabled/photodoc";;
    proxy) ln -sfn "$SB/nginx/sites-available/backend.conf" "$SB/nginx/sites-enabled/photodoc";;
    *) ln -sfn "$SB/nginx/sites-available/candidate.conf" "$SB/nginx/sites-enabled/photodoc";;
  esac
  printf '1' > "$SB/DEPLOYMENT_BLOCKED"
}
run_publish() {
  SIM_LINK="$SB/nginx/sites-enabled/photodoc" SIM_NGINX_FAIL="${SIM_NGINX_FAIL:-}" \
  SIM_SYSTEMCTL_FAIL="${SIM_SYSTEMCTL_FAIL:-}" SIM_BROKEN_BLOCKED="${SIM_BROKEN_BLOCKED:-}" \
  SIM_FORCE_200="${SIM_FORCE_200:-}" \
  SITE_ROOT="$SB/site" SITE_CONF_SRC="$SRC/deploy/preview.conf" \
  SITE_CONF_DST="$SB/nginx/sites-available/candidate.conf" \
  LINK="$SB/nginx/sites-enabled/photodoc" BLOCKED_SITE="$SB/nginx/sites-available/photodoc.tls" \
  MARKER="$SB/DEPLOYMENT_BLOCKED" BACKUP_ROOT="$SB/backups" \
  NGINX_BIN="$WORK/bin/nginx" SYSTEMCTL_BIN="$WORK/bin/systemctl" CURL_BIN="$WORK/bin/curl" \
  PUBLIC_IP=127.0.0.1 bash "$PUBLISH" >"$SB/log" 2>&1
}
run_rollback() {
  SIM_LINK="$SB/nginx/sites-enabled/photodoc" SIM_NGINX_FAIL="${SIM_NGINX_FAIL:-}" \
  SIM_SYSTEMCTL_FAIL="${SIM_SYSTEMCTL_FAIL:-}" SIM_BROKEN_BLOCKED="${SIM_BROKEN_BLOCKED:-}" \
  SIM_FORCE_200="${SIM_FORCE_200:-}" \
  LINK="$SB/nginx/sites-enabled/photodoc" BLOCKED_SITE="$SB/nginx/sites-available/photodoc.tls" \
  MARKER="$SB/DEPLOYMENT_BLOCKED" \
  NGINX_BIN="$WORK/bin/nginx" SYSTEMCTL_BIN="$WORK/bin/systemctl" CURL_BIN="$WORK/bin/curl" \
  PUBLIC_IP=127.0.0.1 bash "$ROLLBACK" >"$SB/log" 2>&1
}
target() { readlink -f "$SB/nginx/sites-enabled/photodoc" 2>/dev/null || true; }

echo "== publish =="
sandbox blocked; printf '1' > "$SB/broken"; SIM_BROKEN_BLOCKED="$SB/broken" run_publish && bad "publish: broken blocked must fail" || ok "publish: broken blocked fails"
sandbox blocked; rm -f "$SB/DEPLOYMENT_BLOCKED"; run_publish && bad "publish: missing marker must fail" || ok "publish: missing marker fails"
sandbox blocked; run_publish && ok "publish: happy path" || bad "publish: happy path failed"
[ "$(target)" = "$SB/nginx/sites-available/candidate.conf" ] && ok "publish: link switched" || bad "publish: link not switched"

echo "== rollback: normal =="
sandbox candidate; run_rollback && ok "rollback: normal -> 503" || bad "rollback: normal failed"
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "rollback: blocked target" || bad "rollback: wrong target"
run_rollback && ok "rollback: idempotent" || bad "rollback: idempotent failed"

echo "== rollback: stray backup =="
sandbox candidate; mkdir -p "$SB/backups/preview-publish-x"
printf 'server { proxy_pass http://evil; }\n' > "$SB/backups/preview-publish-x/previous-site.conf"
printf '%s\n' "$SB/backups/preview-publish-x/previous-site.conf" > "$SB/backups/preview-publish-x/previous-target.txt"
run_rollback && ok "rollback: stray backup ignored" || bad "rollback: failed with stray backup"
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "rollback: blocked used, not the backup" || bad "rollback: restored a backup ($(target))"

echo "== rollback: blocked target contains proxy_pass =="
sandbox candidate; printf 'server { proxy_pass http://backend:8000; return 503; }\n' > "$SB/nginx/sites-available/photodoc.tls"
run_rollback && bad "rollback: proxy_pass target must be refused" || ok "rollback: proxy_pass target refused"

echo "== rollback: missing maintenance config =="
sandbox candidate; rm -f "$SB/nginx/sites-available/photodoc.tls"
run_rollback && bad "rollback: missing blocked must fail" || ok "rollback: missing blocked fails"

echo "== rollback: reload failure =="
sandbox candidate; printf '1' > "$SB/systemctl-fail"; SIM_SYSTEMCTL_FAIL="$SB/systemctl-fail" run_rollback
rc=$?; [ "$rc" -ne 0 ] && ok "rollback: reload failure exits nonzero" || bad "rollback: reload failure exit 0"

echo "== rollback: broken blocked + UNSAFE previous (the reported exploit) =="
sandbox proxy; printf '1' > "$SB/broken"
SIM_BROKEN_BLOCKED="$SB/broken" run_rollback
rc=$?
[ "$rc" -ne 0 ] && ok "exploit: exit nonzero" || bad "exploit: exit 0"
if [ "$(target)" = "$SB/nginx/sites-available/backend.conf" ]; then
  bad "exploit: unsafe previous configuration was reactivated"
else
  ok "exploit: unsafe previous configuration NOT reactivated ($(target))"
fi
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "exploit: blocked config left on disk" || bad "exploit: unexpected final target $(target)"

echo "== rollback: broken blocked + previous that hides proxying behind include =="
sandbox candidate
printf 'server { include /etc/nginx/snippets/backend.conf; }\n' > "$SB/nginx/sites-available/included.conf"
ln -sfn "$SB/nginx/sites-available/included.conf" "$SB/nginx/sites-enabled/photodoc"
printf '1' > "$SB/broken"
SIM_BROKEN_BLOCKED="$SB/broken" run_rollback
rc=$?
[ "$rc" -ne 0 ] && ok "include previous: exit nonzero" || bad "include previous: exit 0"
if [ "$(target)" = "$SB/nginx/sites-available/included.conf" ]; then
  bad "include previous: configuration with include was reactivated"
else
  ok "include previous: configuration with include NOT reactivated"
fi

echo "== rollback: broken blocked + SAFE previous =="
sandbox candidate; printf '1' > "$SB/broken"
SIM_BROKEN_BLOCKED="$SB/broken" run_rollback
rc=$?
[ "$rc" -ne 0 ] && ok "safe previous: exit nonzero (blocked not confirmed)" || bad "safe previous: exit 0"
[ "$(target)" = "$SB/nginx/sites-available/candidate.conf" ] && ok "safe previous: published site restored" || bad "safe previous: target $(target)"

echo "== rollback: interrupted recovery is repeatable =="
sandbox candidate; printf '1' > "$SB/broken"
SIM_BROKEN_BLOCKED="$SB/broken" run_rollback || true
rm -f "$SB/broken"
run_rollback && ok "recovery: second run reaches 503" || bad "recovery: second run failed"
[ "$(target)" = "$SB/nginx/sites-available/photodoc.tls" ] && ok "recovery: blocked confirmed" || bad "recovery: target $(target)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
