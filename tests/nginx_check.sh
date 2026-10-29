#!/usr/bin/env bash
# The nginx and fail2ban files of deploy/ on the packages of Ubuntu 26.04, installed as
# docs/deployment.md (sections 4 and 5) installs them, in front of the running web service of
# compose.yaml: nginx -t, then the answers through it (headers once each, HTTP/2, gzip, Upgrade
# refused, http to https, unknown names refused, the masked log, the rate limits) and fail2ban's
# filter on the log it wrote. A self-signed certificate stands in for certbot's. Run it in a fresh
# container on the network of the web service (CI: the docker job):
#   docker run --rm --network findaline_web -v "$PWD:/src:ro" \
#     ubuntu:26.04@sha256:<digest of .github/workflows/ci.yml> bash /src/tests/nginx_check.sh
# UPSTREAM is where nginx finds the app (web:8765, the service's name in that network); SRC the
# checkout.
set -euo pipefail
SRC=${SRC:-/src}
UPSTREAM=${UPSTREAM:-web:8765}
SITE=findaline.example.org
TOKEN=AbCdEf0123456789-_shareTOKENxxxxxxxxxxxxxxx
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends nginx fail2ban curl openssl ca-certificates >/dev/null
nginx -v

echo "== install (docs/deployment.md, sections 4 and 5)"
cp "$SRC/deploy/nginx/findaline-http.conf" /etc/nginx/conf.d/
cp "$SRC/deploy/nginx/findaline-tls.conf" /etc/nginx/snippets/
sed "s/findaline\.example\.org/$SITE/g" "$SRC/deploy/nginx/findaline-acme.conf" > /etc/nginx/sites-available/findaline-acme
sed "s/findaline\.example\.org/$SITE/g" "$SRC/deploy/nginx/findaline.conf" > /etc/nginx/sites-available/findaline
rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/findaline-acme /etc/nginx/sites-enabled/
mkdir -p /var/www/letsencrypt/.well-known/acme-challenge
echo probe > /var/www/letsencrypt/.well-known/acme-challenge/probe
nginx -t
ln -s /etc/nginx/sites-available/findaline /etc/nginx/sites-enabled/
cp "$SRC/deploy/fail2ban/findaline.conf" /etc/fail2ban/filter.d/
cp "$SRC/deploy/fail2ban/findaline.local" /etc/fail2ban/jail.d/
# Only for this test: the upstream, and certbot's files
sed -i "s/server 127\.0\.0\.1:8765;/server $UPSTREAM;/" /etc/nginx/conf.d/findaline-http.conf
mkdir -p "/etc/letsencrypt/live/$SITE"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=$SITE" \
  -keyout "/etc/letsencrypt/live/$SITE/privkey.pem" -out "/etc/letsencrypt/live/$SITE/fullchain.pem" 2>/dev/null
nginx -t
nginx
fail2ban-client --test >/dev/null || fail "fail2ban's configuration"

C=(curl -sS -k --resolve "$SITE:443:127.0.0.1" --resolve "$SITE:80:127.0.0.1" --max-time 20)
code() { "${C[@]}" -o /dev/null -w '%{http_code}' "$@"; }

echo "== headers"
headers=$("${C[@]}" --http2 -o /dev/null -D - "https://$SITE/")
echo "$headers" | head -1 | grep -q '^HTTP/2 200' || fail "GET / over HTTP/2: $(echo "$headers" | head -1)"
for name in content-security-policy x-frame-options referrer-policy x-content-type-options permissions-policy \
            cross-origin-opener-policy cross-origin-resource-policy strict-transport-security; do
  count=$(echo "$headers" | grep -ci "^$name:" || true)
  [ "$count" = 1 ] || fail "$name sent $count times"
done
echo "$headers" | grep -qi '^server: nginx\s*$' || fail "the server header names a version"

echo "== gzip, Upgrade, http, unknown names"
"${C[@]}" -o /dev/null -D - -H 'Accept-Encoding: gzip' "https://$SITE/app/app.js" | grep -qi '^content-encoding: gzip' \
  || fail "app.js not compressed"
[ "$(code --http1.1 -H 'Upgrade: websocket' -H 'Connection: Upgrade' "https://$SITE/")" = 400 ] \
  || fail "Upgrade not refused"  # HTTP/1.1 only: HTTP/2 has no Upgrade
[ "$(code "http://$SITE/app/")" = 301 ] || fail "http not sent to https"
"${C[@]}" -o /dev/null -D - "http://$SITE/app/?x=1" | grep -qi "^location: https://$SITE/app/?x=1" || fail "redirect target"
[ "$(curl -sS --resolve "$SITE:80:127.0.0.1" "http://$SITE/.well-known/acme-challenge/probe")" = probe ] \
  || fail "ACME challenge"
if curl -sk --resolve other.example.org:443:127.0.0.1 https://other.example.org/ -o /dev/null; then
  fail "TLS handshake for another name"
fi
if curl -s --resolve other.example.org:80:127.0.0.1 http://other.example.org/ -o /dev/null; then
  fail "an answer on port 80 for another name"
fi

echo "== the masked log"
code "https://$SITE/s/$TOKEN/map.html" >/dev/null
code "http://$SITE/s/$TOKEN/map.html" >/dev/null
code -e "https://$SITE/s/$TOKEN/map.html" "https://$SITE/cache/none.png" >/dev/null
code "https://$SITE/auth/github?code=secretcode&state=secretstate" >/dev/null
log=/var/log/nginx/findaline.access.log
! grep -q "$TOKEN" $log /var/log/nginx/findaline.error.log || fail "a share token in the logs"
! grep -q secret $log /var/log/nginx/findaline.error.log || fail "a sign-in query in the logs"
grep -q '"GET /s/<token>/map.html HTTP/2.0"' $log || fail "the masked share link"
grep -q '"GET /auth/github?<query> HTTP/2.0"' $log || fail "the masked sign-in callback"
grep -q '"https://findaline.example.org/s/<token>/map.html"' $log || fail "the masked Referer"

echo "== rate limits and the jail's filter"
counts=$(seq 400 | xargs -P 16 -I{} "${C[@]}" -o /dev/null -w '%{http_code}\n' "https://$SITE/api/schema" | sort | uniq -c)
echo "$counts"
echo "$counts" | grep -q ' 429$' || fail "no 429 beyond the burst"
echo "$counts" | grep -q ' 401$' || fail "no request reached the app"
logins=$(for _ in $(seq 15); do code "https://$SITE/login/github"; echo; done | sort | uniq -c)
echo "$logins"
echo "$logins" | grep -q ' 429$' || fail "no 429 beyond the sign-in burst"
matched=$(fail2ban-regex $log /etc/fail2ban/filter.d/findaline.conf | sed -n 's/^Lines: .* \([0-9]*\) matched.*/\1/p')
echo "fail2ban-regex: $matched lines matched"
[ "${matched:-0}" -gt 100 ] || fail "the filter matched ${matched:-0} refusals"

nginx -s quit
echo "OK: nginx and fail2ban of deploy/"
