#!/usr/bin/env bash
# Find-a-Line: set up, update and check the public site on a VPS with Ubuntu 26.04 LTS
# (docs/deployment.md: the script runs the commands of its sections). From the checkout, as root
# through sudo (as your own user: that user's SSH key is checked before passwords are turned off):
#   sudo deploy/vps.sh setup --site findaline.example.org --email you@example.org [--my-ip 203.0.113.7]
#   sudo deploy/vps.sh update
#   sudo deploy/vps.sh check
#
# setup: packages and Docker (section 1), SSH with a key only and the firewall (2), the DNS check
# (3), nginx and the certificate (4), fail2ban (5), automatic security updates (6), the settings
# and secrets (8: asks for the sign-in apps' values of section 7 when run in a terminal), the
# start (9), the daily backup when /etc/findaline-backup.env exists (11), then check. Every step
# looks at what is there first, so setup can run again: after a stop (it says what is missing, e.g.
# DNS not pointing here yet), or to apply changed options.
# update: the CI of the new commits (GitHub's check runs), git pull, the files of deploy/ again
# where they changed (nginx reloaded, fail2ban restarted, ufw reloaded), the image built again and
# the services restarted (section 13).
# check: the checks of sections 2 and 12 that can run on the server.
#
# Options:
#   --site NAME      the site's name (setup; later read from FINDALINE_PUBLIC_URL in findaline.env)
#   --email ADDRESS  Let's Encrypt's expiry notices (setup, for the first certificate) and, unless
#                    --contact, FINDALINE_CONTACT
#   --contact TEXT   FINDALINE_CONTACT (an e-mail address or URL for the data services' operators)
#   --my-ip ADDRESS  your own fixed address: SSH only from it, never banned by fail2ban
#   --data-root DIR  Docker's data root on a mounted volume of its own (section 1, Disk; only
#                    before the site's volumes exist)
#   --disk-image SIZE  without a block volume: a file of SIZE (e.g. 200G) on the system disk with
#                    a file system of its own, mounted at --data-root (default /srv/docker)
#   --skip-ci        update without checking the CI
#   --yes            answer yes to the questions (SSH without a recent key login, --my-ip not
#                    seen among the current logins, a CI that has not passed)
set -euo pipefail

REPO=${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DEPLOY=$REPO/deploy
SITE=${SITE:-}
EMAIL=
CONTACT=
MY_IP=
DATA_ROOT=
DISK_IMAGE=
IMAGE_FILE=/srv/findaline-docker.img
SKIP_CI=0
YES=0
PULLED=0
CHANGED=0
SETTINGS_READY=0
TODO=()

step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
todo() { TODO+=("$*"); warn "$*"; }

# A question for the operator: yes with --yes, no without a terminal
ask() {
    local answer
    [ "$YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    read -r -p "$1 [y/N] " answer
    [[ $answer == [yY]* ]]
}

# Puts stdin into the file $1 with mode $2 when its content differs; sets CHANGED=1 if it did
put() {
    local target=$1 mode=$2 tmp
    tmp=$(mktemp)
    cat > "$tmp"
    if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
        rm -f "$tmp"
        return 0
    fi
    install -m "$mode" "$tmp" "$target"
    rm -f "$tmp"
    CHANGED=1
    note "wrote $target"
}

# A variable of findaline.env or .env (the last assignment; empty if none)
env_get() {
    if [ -f "$1" ]; then
        sed -n "s/^$2=//p" "$1" | tail -n 1
    fi
}

# Sets a variable in findaline.env or .env: replaces its assignment, else its commented example
# ("#KEY="), else appends it
env_set() {
    python3 - "$1" "$2" "$3" <<'PY'
import sys
path, key, value = sys.argv[1:]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
set_at = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
example_at = [i for i, line in enumerate(lines) if line.startswith("#" + key + "=")]
if set_at:
    lines[set_at[-1]] = f"{key}={value}"
elif example_at:
    lines[example_at[0]] = f"{key}={value}"
else:
    lines.append(f"{key}={value}")
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PY
    note "$(basename "$1"): $2 set"
}

# The checkout belongs to a user (git refuses a repository of another user to root)
owner() { stat -c %U "$REPO"; }
as_owner() { sudo -u "$(owner)" -H -- "$@"; }
compose() { docker compose --project-directory "$REPO" -f "$REPO/compose.yaml" "$@"; }
web_port() { local port; port=$(env_get "$REPO/.env" WEB_PORT); echo "${port:-8765}"; }
public_ip() { curl -4fsS --max-time 10 https://ifconfig.me; }
secret_set() { [ -s "$1" ] && [ -n "$(tr -d '[:space:]' < "$1")" ]; }

# The site's name: --site, or the host of FINDALINE_PUBLIC_URL
site_name() {
    if [ -z "$SITE" ]; then
        SITE=$(env_get "$REPO/findaline.env" FINDALINE_PUBLIC_URL | sed -E 's#^https://##; s#/.*$##')
    fi
    if [ -z "$SITE" ] || [ "$SITE" = findaline.example.org ]; then
        die "the site's name: --site <name> (it goes into FINDALINE_PUBLIC_URL of findaline.env)"
    fi
    [[ $SITE =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "not a host name: $SITE"
}

# --- Section 1 ---------------------------------------------------------------------------------

check_system() {
    step "The system (section 1)"
    [ "$(id -u)" = 0 ] || die "run it as root: sudo $0 ..."
    # shellcheck source=/dev/null
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] || die "made for Ubuntu, this is ${PRETTY_NAME:-unknown}"
    [ "${VERSION_ID:-}" = 26.04 ] || warn "made for Ubuntu 26.04, this is $PRETTY_NAME"
    [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || die "cgroup v2 is needed (section 1)"
    note "$PRETTY_NAME, cgroup v2, $(nproc) cores, $(free -g | awk '/^Mem:/ {print $2}') GB"
}

install_packages() {
    step "Packages (sections 1, 4, 5, 6)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git sqlite3 restic nginx certbot fail2ban unattended-upgrades ufw \
        curl ca-certificates python3 bind9-dnsutils >/dev/null
    note "git, sqlite3, restic, nginx $(nginx -v 2>&1 | sed 's#.*/##'), certbot, fail2ban, ufw"
}

install_docker() {
    step "Docker (section 1)"
    if ! command -v docker >/dev/null; then
        # docs.docker.com/engine/install/ubuntu: Docker's repository, not Ubuntu's docker.io
        dpkg -s docker.io >/dev/null 2>&1 && die "Ubuntu's docker.io is installed: remove it first"
        # shellcheck source=/dev/null
        . /etc/os-release
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        put /etc/apt/sources.list.d/docker.sources 644 <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
            docker-compose-plugin >/dev/null
    fi
    local version
    version=$(docker version --format '{{.Server.Version}}')
    [ "${version%%.*}" -ge 28 ] || die "Docker Engine $version: at least 28 is needed (section 1)"
    docker info 2>/dev/null | grep -qi 'firewall backend: *nftables' \
        && die "Docker runs its nftables firewall: the rules of section 2 need its iptables one"
    note "Docker Engine $version, $(docker compose version --short 2>/dev/null | sed 's/^/Compose /')"

    local root
    root=$(docker info --format '{{.DockerRootDir}}')
    if [ -n "$DATA_ROOT" ] && [ "$root" != "$DATA_ROOT" ]; then
        mountpoint -q "$DATA_ROOT" || die "$DATA_ROOT is not a mounted file system (section 1, Disk)"
        docker volume ls -q | grep -q '^findaline_' \
            && die "the site's volumes are already under $root: move Docker's data root by hand"
        CHANGED=0
        python3 - "$DATA_ROOT" <<'PY'
import json, os, sys
path = "/etc/docker/daemon.json"
config = json.load(open(path)) if os.path.exists(path) else {}
config["data-root"] = sys.argv[1]
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PY
        systemctl restart docker
        root=$(docker info --format '{{.DockerRootDir}}')
        note "Docker's data root: $root"
    elif [ "$(df --output=target "$root" | tail -n 1)" = / ]; then
        warn "Docker's data root $root is on the system disk (section 1, Disk: --data-root or --disk-image)"
    fi
    # A data root on a file system of its own: Docker starts only once it is mounted (with
    # `nofail` the boot goes on without it, and Docker would start on an empty folder)
    if mountpoint -q "$root"; then
        CHANGED=0
        mkdir -p /etc/systemd/system/docker.service.d
        put /etc/systemd/system/docker.service.d/findaline-data-root.conf 644 <<EOF
[Unit]
RequiresMountsFor=$root
EOF
        if [ "$CHANGED" = 1 ]; then systemctl daemon-reload; fi
    fi
}

# Without a block volume: a file of $DISK_IMAGE on the system disk with an ext4 of its own,
# mounted at $DATA_ROOT (section 1, Disk). The file is allocated whole (fallocate, not a sparse
# file), so the searches can fill it but never the system disk.
disk_image() {
    step "Disk image (section 1, Disk)"
    if awk -v dir="$DATA_ROOT" '$1 !~ /^#/ && $2 == dir { found = 1 } END { exit !found }' /etc/fstab; then
        mountpoint -q "$DATA_ROOT" || mount "$DATA_ROOT"
        note "$DATA_ROOT: $(df -h --output=size "$DATA_ROOT" | tail -n 1 | tr -d ' ') (in /etc/fstab already)"
        return 0
    fi
    mountpoint -q "$DATA_ROOT" && die "$DATA_ROOT is mounted already, not from /etc/fstab"
    [ -d "$DATA_ROOT" ] && [ -n "$(ls -A "$DATA_ROOT")" ] && die "$DATA_ROOT is not empty"
    local bytes free
    bytes=$(numfmt --from=iec "$DISK_IMAGE") || die "--disk-image: a size such as 200G"
    if [ -e "$IMAGE_FILE" ]; then
        # Left by a setup that stopped: used only if it holds a file system already
        [ "$(blkid -o value -s TYPE "$IMAGE_FILE" || true)" = ext4 ] \
            || die "$IMAGE_FILE exists without a file system: remove it by hand"
    else
        mkdir -p "$(dirname "$IMAGE_FILE")"
        free=$(df -B1 --output=avail "$(dirname "$IMAGE_FILE")" | tail -n 1 | tr -dc 0-9)
        # At least 10 GB stay for the system: updates, logs, Docker's images go to the image
        [ $((free - bytes)) -ge $((10 * 1024 ** 3)) ] \
            || die "--disk-image $DISK_IMAGE leaves less than 10 GB of $(numfmt --to=iec "$free") free"
        fallocate -l "$bytes" "$IMAGE_FILE"
        chmod 600 "$IMAGE_FILE"
        # nodiscard: mkfs would punch holes in the file, making it sparse again
        mkfs.ext4 -q -E nodiscard -L findaline-docker "$IMAGE_FILE"
        note "made $IMAGE_FILE ($DISK_IMAGE, ext4)"
    fi
    mkdir -p "$DATA_ROOT"
    # X-fstrim.notrim: Ubuntu's weekly fstrim.timer would punch the freed blocks out of the file
    printf '%s %s ext4 loop,nofail,X-fstrim.notrim 0 0\n' "$IMAGE_FILE" "$DATA_ROOT" >> /etc/fstab
    systemctl daemon-reload
    mount "$DATA_ROOT"
    note "$DATA_ROOT mounted from $IMAGE_FILE (added to /etc/fstab)"
}

# --- Section 2 ---------------------------------------------------------------------------------

ssh_key_only() {
    step "SSH with a key only (section 2)"
    local conf=/etc/ssh/sshd_config.d/10-key-only.conf
    local want=$'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no'
    if [ -f "$conf" ] && [ "$(cat "$conf")" = "$want" ]; then
        note "already: $conf"
        return
    fi
    local user=${SUDO_USER:-} home
    if [ -z "$user" ] || [ "$user" = root ]; then
        todo "SSH still takes passwords: run setup with sudo as your own user (not root), whose key is in ~/.ssh/authorized_keys"
        return
    fi
    home=$(getent passwd "$user" | cut -d: -f6)
    if ! grep -qE '^(ssh-|ecdsa-|sk-)' "$home/.ssh/authorized_keys" 2>/dev/null; then
        todo "SSH still takes passwords: $home/.ssh/authorized_keys holds no key"
        return
    fi
    id -nG "$user" | grep -qw sudo || { todo "SSH still takes passwords: $user is not in the group sudo"; return; }
    # A login with that key in the last week (sshd-session logs it since OpenSSH 9.8)
    if ! { journalctl -q -t sshd -t sshd-session --since -7d 2>/dev/null; cat /var/log/auth.log 2>/dev/null; } \
            | grep -q "Accepted publickey for $user from"; then
        if ! ask "$user has not logged in with a key in the last week. Turn SSH passwords off anyway?"; then
            todo "SSH still takes passwords: log in as $user with your key, then run setup again"
            return
        fi
    fi
    CHANGED=0
    put "$conf" 644 <<<"$want"
    sshd -t || { rm -f "$conf"; die "sshd -t failed: $conf removed again"; }
    systemctl try-reload-or-restart ssh
    sshd -T | grep -qx 'passwordauthentication no' \
        || warn "sshd still takes passwords: another file in /etc/ssh/sshd_config.d sorts before 10-key-only.conf"
    note "passwords and root's login off. Keep this session open and log in again from another terminal now"
}

# The blocks of deploy/firewall in ufw's files: the old block out (from its first line, a comment
# "# Find-a-Line:", to its last line), the current one in
ufw_files() {
    local after=/etc/ufw/after.rules before=/etc/ufw/before.rules tmp
    [ -f "$after.bak" ] || cp -p "$after" "$after.bak"
    [ -f "$before.bak" ] || cp -p "$before" "$before.bak"

    tmp=$(mktemp)
    sed '/^# Find-a-Line:/,/^COMMIT/d' "$after" > "$tmp"
    cat "$DEPLOY/firewall/ufw-after.rules" >> "$tmp"
    [ "$(grep -c '^COMMIT' "$tmp")" = "$(grep -c '^COMMIT' "$after")" ] \
        || [ "$(grep -c '^COMMIT' "$tmp")" = "$(( $(grep -c '^COMMIT' "$after") + 1 ))" ] \
        || die "$after: unexpected content; restore $after.bak (section 2)"
    put "$after" 640 < "$tmp"

    sed '/^# Find-a-Line:/,/-i br-fal-+ -j DROP/d' "$before" > "$tmp"
    if grep -q '^# ok icmp codes for INPUT' "$tmp"; then
        sed -i "/^# ok icmp codes for INPUT/e cat '$DEPLOY/firewall/ufw-before.rules'" "$tmp"
    elif grep -q -- '-A ufw-before-input -m conntrack --ctstate INVALID -j DROP' "$tmp"; then
        sed -i "/-A ufw-before-input -m conntrack --ctstate INVALID -j DROP/r $DEPLOY/firewall/ufw-before.rules" "$tmp"
    else
        rm -f "$tmp"
        die "$before has neither '# ok icmp codes for INPUT' nor the INVALID rule: put the rule of deploy/firewall/ufw-before.rules in by hand (section 2)"
    fi
    [ "$(grep -c '^COMMIT' "$tmp")" = "$(grep -c '^COMMIT' "$before")" ] \
        || die "$before: the old block of Find-a-Line has lost its rule; restore $before.bak (section 2)"
    put "$before" 640 < "$tmp"
    rm -f "$tmp"
}

firewall() {
    step "The firewall (section 2)"
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    local ssh_open=1
    if [ -n "$MY_IP" ]; then
        if ss -Htn state established '( sport = :22 )' | awk '{sub(/:[0-9]+$/, "", $NF); print $NF}' | grep -qxF "$MY_IP" \
                || ask "No login from $MY_IP now. Open SSH to $MY_IP only anyway?"; then
            ufw allow from "$MY_IP" to any port 22 proto tcp >/dev/null
            ufw show added | grep -qx 'ufw allow OpenSSH' && ufw delete allow OpenSSH >/dev/null
            ssh_open=0
            note "SSH from $MY_IP only (keep the provider's web console at hand)"
        else
            todo "SSH stays open to everyone: --my-ip $MY_IP has no SSH connection to the server now"
        fi
    fi
    [ "$ssh_open" = 1 ] && ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null

    CHANGED=0
    ufw_files
    ufw insert 1 deny in on br-fal-web >/dev/null
    ufw insert 1 deny in on br-fal-worker >/dev/null
    ufw --force enable >/dev/null
    [ "$CHANGED" = 1 ] && ufw reload >/dev/null
    # iptables -S prints a rule's options in its own order (-d before -i), not the file's
    local rules range
    rules=$(iptables -S DOCKER-USER)
    for range in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
        grep -qxF -- "-A DOCKER-USER -d $range -i br-fal-+ -j DROP" <<< "$rules" \
            || die "DOCKER-USER lacks the rules of ufw-after.rules ($range; iptables -S DOCKER-USER)"
    done
    iptables -S ufw-before-input | grep -q -- '-i br-fal-+ -j DROP' \
        || die "ufw-before-input lacks the rule of ufw-before.rules"
    note "ufw active: $(ufw status | grep -c ALLOW) rules allow, the containers kept from the host and private addresses"
}

# --- Sections 3 and 4 --------------------------------------------------------------------------

# The site's A record points here and there is no AAAA (section 3)
dns_ready() {
    local here there
    here=$(public_ip) || { warn "the server's public address is unknown (ifconfig.me)"; return 1; }
    there=$(dig +short A "$SITE" @1.1.1.1 | grep -E '^[0-9.]+$' | sort | tr '\n' ' ')
    dig +short AAAA "$SITE" @1.1.1.1 | grep -q : \
        && warn "$SITE has an AAAA record: remove it (section 3, IPv4 only for now)"
    if [ "${there% }" != "$here" ]; then
        todo "DNS: the A record of $SITE says '${there% }', the server is $here (section 3); run setup again once it points here"
        return 1
    fi
    note "$SITE → $here"
}

# The nginx files of deploy/nginx with the site's name, the upstream at WEB_PORT, the site of port
# 443 enabled once its certificate exists; sets CHANGED=1 if anything changed
nginx_files() {
    put /etc/nginx/conf.d/findaline-http.conf 644 \
        < <(sed "s/server 127\.0\.0\.1:8765;/server 127.0.0.1:$(web_port);/" "$DEPLOY/nginx/findaline-http.conf")
    put /etc/nginx/snippets/findaline-tls.conf 644 < "$DEPLOY/nginx/findaline-tls.conf"
    local name
    for name in findaline-acme findaline; do
        put "/etc/nginx/sites-available/$name" 644 \
            < <(sed "s/findaline\.example\.org/$SITE/g" "$DEPLOY/nginx/$name.conf")
    done
    if [ -e /etc/nginx/sites-enabled/default ]; then
        rm -f /etc/nginx/sites-enabled/default
        CHANGED=1
    fi
    mkdir -p /var/www/letsencrypt
    enable_site findaline-acme
    [ -f "/etc/letsencrypt/live/$SITE/fullchain.pem" ] && enable_site findaline
    return 0
}

enable_site() {
    if [ "$(readlink "/etc/nginx/sites-enabled/$1" 2>/dev/null)" != "/etc/nginx/sites-available/$1" ]; then
        ln -sfn "/etc/nginx/sites-available/$1" "/etc/nginx/sites-enabled/$1"
        CHANGED=1
        note "enabled the site $1"
    fi
}

nginx_reload() {
    nginx -t 2>/dev/null || { nginx -t; die "nginx -t failed: nginx keeps its old configuration (section 14)"; }
    systemctl reload nginx
}

nginx_and_certificate() {
    step "nginx and the certificate (sections 3, 4)"
    CHANGED=0
    nginx_files
    [ "$CHANGED" = 1 ] && nginx_reload
    if [ -f "/etc/letsencrypt/live/$SITE/fullchain.pem" ]; then
        note "certificate: $(openssl x509 -noout -enddate -in "/etc/letsencrypt/live/$SITE/fullchain.pem")"
        return
    fi
    dns_ready || return 0
    if [ -z "$EMAIL" ]; then
        todo "the certificate: run setup again with --email <address> (Let's Encrypt's expiry notices)"
        return
    fi
    certbot certonly --non-interactive --webroot -w /var/www/letsencrypt -d "$SITE" -m "$EMAIL" \
        --agree-tos --no-eff-email --deploy-hook 'systemctl reload nginx'
    CHANGED=0
    nginx_files
    nginx_reload
    certbot renew --dry-run --cert-name "$SITE" >/dev/null 2>&1 || warn "certbot renew --dry-run failed: sudo certbot renew --dry-run"
    systemctl is-active -q certbot.timer || warn "certbot.timer is not active: the certificate would not be renewed"
    note "certificate for $SITE, renewed by certbot.timer"
}

# --- Sections 5 and 6 --------------------------------------------------------------------------

fail2ban_files() {
    put /etc/fail2ban/filter.d/findaline.conf 644 < "$DEPLOY/fail2ban/findaline.conf"
    put /etc/fail2ban/jail.d/findaline.local 644 < "$DEPLOY/fail2ban/findaline.local"
    if [ -n "$MY_IP" ]; then
        put /etc/fail2ban/jail.d/00-ignore.local 644 \
            < <(printf '# Find-a-Line setup (--my-ip): never banned, by any jail\n[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 %s\n' "$MY_IP")
    fi
}

fail2ban() {
    step "fail2ban (section 5)"
    CHANGED=0
    fail2ban_files
    if [ "$CHANGED" = 1 ] || ! fail2ban-client ping >/dev/null 2>&1; then
        fail2ban-client --test >/dev/null || die "fail2ban's configuration: fail2ban-client --test"
        systemctl restart fail2ban
    fi
    local i
    for i in $(seq 15); do
        fail2ban-client status findaline >/dev/null 2>&1 && break
        [ "$i" = 15 ] && die "the jail findaline does not run: journalctl -u fail2ban"
        sleep 1
    done
    note "jails: $(fail2ban-client status | sed -n 's/.*Jail list:\s*//p')"
}

security_updates() {
    step "Automatic security updates (section 6)"
    if ! grep -qs 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades; then
        echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' | debconf-set-selections
        dpkg-reconfigure -f noninteractive unattended-upgrades
    fi
    note "unattended-upgrades on"
    [ -f /var/run/reboot-required ] && todo "a reboot is pending (/var/run/reboot-required): sudo reboot"
    return 0
}

# --- Sections 7 and 8 --------------------------------------------------------------------------

# Asks for a sign-in app's client id and secret (section 7), when there is a terminal and none set
ask_provider() {
    local provider=$1 label=$2 env=$REPO/findaline.env secret=$REPO/secrets/findaline_$1_client_secret id value
    local key=FINDALINE_${provider^^}_CLIENT_ID
    [ -n "$(env_get "$env" "$key")" ] && secret_set "$secret" && return
    [ -t 0 ] || return 0
    read -r -p "   Client ID of the $label app (section 7; empty: no sign-in with $label): " id
    [ -n "$id" ] || return 0
    read -r -s -p "   Client secret of the $label app: " value
    echo
    [ -n "$value" ] || { warn "no secret: $label is not set"; return; }
    env_set "$env" "$key" "$id"
    chmod 600 "$secret"
    printf '%s\n' "$value" > "$secret"
}

settings() {
    step "Settings and secrets (sections 7, 8)"
    local env=$REPO/findaline.env compose_env=$REPO/.env user group file
    user=$(owner)
    group=$(id -gn "$user")
    [ -f "$compose_env" ] || install -m 644 -o "$user" -g "$group" "$REPO/.env.example" "$compose_env"
    [ -n "$(env_get "$env" FINDALINE_TRUSTED_PROXY)" ] || env_set "$env" FINDALINE_TRUSTED_PROXY 172.30.0.1
    [ -z "$CONTACT" ] && [ "$(env_get "$env" FINDALINE_CONTACT)" = you@example.org ] && CONTACT=$EMAIL
    [ -n "$CONTACT" ] && [ "$(env_get "$env" FINDALINE_CONTACT)" != "$CONTACT" ] \
        && env_set "$env" FINDALINE_CONTACT "$CONTACT"

    # The worker's resources: all cores but one, the memory but 2 GB (web, nginx, the system) on a
    # smaller server than compose.yaml's default of 8 GB assumes
    if [ -z "$(env_get "$compose_env" WORKER_CPUS)" ]; then
        env_set "$compose_env" WORKER_CPUS "$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))"
    fi
    local memory_mb
    memory_mb=$(free -m | awk '/^Mem:/ {print $2}')
    if [ -z "$(env_get "$compose_env" WORKER_MEMORY)" ] && [ "$memory_mb" -lt 10240 ]; then
        [ "$memory_mb" -ge 4096 ] || warn "only $memory_mb MB of memory: the searches will be small"
        env_set "$compose_env" WORKER_MEMORY "$(( memory_mb > 4096 ? memory_mb - 2048 : 2048 ))m"
    fi

    install -d -m 755 -o "$user" -g "$group" "$REPO/secrets"
    for file in "$DEPLOY"/secrets.example/*; do
        [ -e "$REPO/secrets/${file##*/}" ] || install -m 600 /dev/null "$REPO/secrets/${file##*/}"
    done
    if ! secret_set "$REPO/secrets/findaline_session_secret"; then
        chmod 600 "$REPO/secrets/findaline_session_secret"
        python3 -c "import secrets; print(secrets.token_urlsafe(48))" > "$REPO/secrets/findaline_session_secret"
        note "secrets/findaline_session_secret: a new one"
    fi
    ask_provider github GitHub
    ask_provider google Google
    if [ -z "$(env_get "$env" FINDALINE_ADMINS)" ] && [ -t 0 ]; then
        local login id
        read -r -p "   GitHub login or numeric id of the administrator (empty: none yet; Google: section 8): " login
        if [ -n "$login" ]; then
            [[ $login =~ ^[A-Za-z0-9-]+$ ]] || die "not a GitHub login: $login"
            # A number is an id (/user/<id>), anything else a login (/users/<login>)
            local url="https://api.github.com/users/$login"
            [[ $login =~ ^[0-9]+$ ]] && url="https://api.github.com/user/$login"
            id=$(curl -fsS --max-time 20 "$url" \
                | python3 -c 'import json, sys; u = json.load(sys.stdin); print(u["id"], u["login"])') \
                || die "no GitHub user $login"
            note "GitHub user ${id#* } (id ${id%% *})"
            env_set "$env" FINDALINE_ADMINS "github:${id%% *}"
        fi
    fi
    # Each secret readable by the containers' user only (secrets/README.md)
    chown 10001:10001 "$REPO"/secrets/findaline_* "$REPO/secrets/opentopography_api_key"
    chmod 400 "$REPO"/secrets/findaline_* "$REPO/secrets/opentopography_api_key"

    local missing=() provider
    [ "$(env_get "$env" FINDALINE_CONTACT)" = you@example.org ] && missing+=("FINDALINE_CONTACT in findaline.env (--contact)")
    local providers=0 ids=0
    for provider in github google; do
        if [ -n "$(env_get "$env" "FINDALINE_${provider^^}_CLIENT_ID")" ]; then
            ids=$((ids + 1))
            if secret_set "$REPO/secrets/findaline_${provider}_client_secret"; then
                providers=$((providers + 1))
            else
                missing+=("secrets/findaline_${provider}_client_secret (sudo nano; FINDALINE_${provider^^}_CLIENT_ID is set)")
            fi
        fi
    done
    [ "$ids" -gt 0 ] \
        || missing+=("a sign-in app (section 7): FINDALINE_GITHUB_CLIENT_ID or _GOOGLE_ in findaline.env, its secret in secrets/")
    [ -n "$(env_get "$env" FINDALINE_ADMINS)" ] || warn "FINDALINE_ADMINS is empty: nobody is an administrator (section 8)"
    if [ "${#missing[@]}" -gt 0 ]; then
        local item
        for item in "${missing[@]}"; do todo "missing: $item"; done
        return
    fi
    SETTINGS_READY=1
    note "findaline.env, .env and secrets/ ready (sign-in providers: $providers)"
}

# --- Sections 9 and 11 -------------------------------------------------------------------------

start() {
    step "Start (section 9)"
    compose build --pull
    compose up -d --wait --wait-timeout 180 || { compose ps; compose logs --tail 30; die "the services did not become healthy"; }
    docker image prune -f >/dev/null
    local gateway proxy
    gateway=$(docker network inspect findaline_web --format '{{(index .IPAM.Config 0).Gateway}}')
    proxy=$(env_get "$REPO/findaline.env" FINDALINE_TRUSTED_PROXY)
    [ "$gateway" = "$proxy" ] \
        || todo "the network findaline_web has the gateway $gateway, FINDALINE_TRUSTED_PROXY is $proxy: docker compose down, then setup again (section 14)"
    compose ps
}

backups() {
    step "Backups (section 11)"
    local env=/etc/findaline-backup.env
    if [ ! -f "$env" ]; then
        todo "no daily backup: /etc/findaline-backup.env (section 11), then setup again"
        return
    fi
    # shellcheck source=/dev/null
    if ! (set -a; . "$env"; restic cat config >/dev/null 2>&1); then
        todo "the restic repository of $env cannot be opened: restic init (section 11)"
        return
    fi
    local on_failure=
    if [ -f /etc/systemd/system/notify-failure@.service ]; then
        on_failure='OnFailure=notify-failure@%N.service'
    else
        warn "no notify-failure@.service (section 11): a failed backup goes unnoticed"
    fi
    CHANGED=0
    put /etc/systemd/system/findaline-backup.service 644 <<EOF
[Unit]
Description=Find-a-Line backup (SQLite copies + restic)
Wants=network-online.target
After=network-online.target docker.service
$on_failure

[Service]
Type=oneshot
EnvironmentFile=$env
ExecStart=$DEPLOY/backup.sh
EOF
    put /etc/systemd/system/findaline-backup.timer 644 <<'EOF'
[Unit]
Description=Daily Find-a-Line backup

[Timer]
OnCalendar=*-*-* 03:30
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF
    [ "$CHANGED" = 1 ] && systemctl daemon-reload
    systemctl enable -q --now findaline-backup.timer
    note "$(systemctl list-timers findaline-backup.timer --no-legend | awk '{print "next backup:", $1, $2, $3}')"
}

# --- Checks (sections 2, 12) -------------------------------------------------------------------

FAILED=0
ok() { note "ok: $*"; }
bad() { printf 'FAIL: %s\n' "$*" >&2; FAILED=1; }

# shellcheck disable=SC2015  # "A && ok || bad": ok never fails
check() {
    step "Checks (sections 2, 12)"
    site_name
    local code headers name addresses service out
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Host: $SITE" "http://127.0.0.1:$(web_port)/") || true
    [ "$code" = 200 ] && ok "the web service answers on 127.0.0.1:$(web_port)" || bad "the web service: $code (section 14, 502)"
    headers=$(curl -sS --max-time 20 -o /dev/null -D - "https://$SITE/" 2>&1) || true
    if echo "$headers" | head -n 1 | grep -q '^HTTP/2 200'; then
        ok "https://$SITE/ over HTTP/2"
        for name in content-security-policy x-frame-options referrer-policy x-content-type-options \
                    permissions-policy cross-origin-opener-policy cross-origin-resource-policy \
                    strict-transport-security; do
            [ "$(echo "$headers" | grep -ci "^$name:")" = 1 ] || bad "the header $name is not there once"
        done
        echo "$headers" | grep -qi '^server: nginx\s*$' || bad "the Server header names a version"
    else
        bad "https://$SITE/: $(echo "$headers" | head -n 1)"
    fi
    curl -sS --max-time 20 -o /dev/null -D - -H 'Accept-Encoding: gzip' "https://$SITE/app/app.js" 2>/dev/null \
        | grep -qi '^content-encoding: gzip' && ok "gzip" || bad "app.js not compressed"

    ufw status | grep -q '^Status: active' && ok "ufw active" || bad "ufw is not active"
    fail2ban-client status findaline >/dev/null 2>&1 && ok "the jail findaline" || bad "the jail findaline does not run"
    systemctl is-active -q certbot.timer && ok "certbot.timer" || bad "certbot.timer is not active"
    sshd -T 2>/dev/null | grep -qx 'passwordauthentication no' && ok "SSH with a key only" || bad "SSH takes passwords"

    # The containers reach nothing but the internet: every address of the host (its public one,
    # private ones, the gateways of the Docker networks), the metadata service, private networks
    addresses=$( { public_ip || true; echo; ip -4 -o addr show scope global | awk '{sub(/\/.*/, "", $4); print $4}'; } | sort -u | grep . | tr '\n' ' ')
    for service in web worker; do
        out=$(compose exec -T -e ADDRESSES="$addresses" "$service" python -c "
import os, socket, urllib.request
targets = [(a, 22) for a in os.environ['ADDRESSES'].split()] + [(a, 443) for a in os.environ['ADDRESSES'].split()]
targets += [('172.30.0.1', 8765), ('169.254.169.254', 80), ('10.0.0.1', 80), ('192.168.0.1', 80), ('100.64.0.1', 80)]
for host, port in targets:
    try:
        socket.create_connection((host, port), 3).close()
        print('REACHED', host, port)
    except TimeoutError:
        pass
    except OSError as e:
        print('ANSWERED', host, port, type(e).__name__)
try:
    print('internet', urllib.request.urlopen('https://nominatim.openstreetmap.org/status', timeout=10).status)
except OSError as e:
    print('internet', e)
" 2>&1) || true
        if echo "$out" | grep -qE '^(REACHED|ANSWERED)'; then
            bad "$service reaches: $(echo "$out" | grep -E '^(REACHED|ANSWERED)' | tr '\n' ';') (section 2)"
        else
            ok "$service blocked from the host and private addresses"
        fi
        echo "$out" | grep -qx 'internet 200' && ok "$service reaches the internet" || bad "$service and the internet: $(echo "$out" | grep '^internet')"
    done
    out=$(compose exec -T worker python -c "import socket; socket.create_connection(('web', 8765), 3)" 2>&1) || true
    echo "$out" | grep -q gaierror && ok "the worker does not see web" || bad "the worker and web: $out"

    local free
    free=$(df -BG --output=avail "$(docker info --format '{{.DockerRootDir}}')" | tail -n 1 | tr -dc 0-9)
    [ "$free" -ge 20 ] && ok "$free GB free for Docker" || bad "$free GB free for Docker: the site accepts no searches below 20"
    if [ -f "$IMAGE_FILE" ]; then
        # Holes in the disk image (a trim, a copy) give its space back to the system disk, which
        # a full one would then need for writes into the image
        local size blocks unit
        read -r size blocks unit < <(stat -c '%s %b %B' "$IMAGE_FILE")
        [ $((blocks * unit)) -ge "$size" ] && ok "$IMAGE_FILE allocated whole" \
            || bad "$IMAGE_FILE has holes: sudo fallocate -l $size $IMAGE_FILE (section 1, Disk)"
    fi
    return "$FAILED"
}

# --- Updates (section 13) ----------------------------------------------------------------------

# GitHub's check runs of a commit (the CI of .github/workflows/ci.yml) must have passed
ci_passed() {
    local remote result
    remote=$(as_owner git -C "$REPO" remote get-url origin)
    result=$(python3 - "$remote" "$1" <<'PY'
import json, re, sys, urllib.request
remote, commit = sys.argv[1:]
match = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", remote)
if not match:
    sys.exit(f"not a GitHub remote: {remote}")
request = urllib.request.Request(
    f"https://api.github.com/repos/{match[1]}/commits/{commit}/check-runs?per_page=100",
    headers={"Accept": "application/vnd.github+json"})
try:
    runs = json.load(urllib.request.urlopen(request, timeout=20))["check_runs"]
except OSError as e:
    sys.exit(f"GitHub's API: {e} (a private repository needs a look at its Actions page)")
if not runs:
    sys.exit("no check runs (yet)")
waiting = [r["name"] for r in runs if r["status"] != "completed"]
failed = [r["name"] for r in runs if r["status"] == "completed" and r["conclusion"] not in ("success", "skipped", "neutral")]
if waiting or failed:
    sys.exit("still running: " * bool(waiting) + ", ".join(waiting) + " failed: " * bool(failed) + ", ".join(failed))
print(f"{len(runs)} check runs passed")
PY
    ) && { note "CI: $result"; return 0; }
    warn "CI of ${1:0:8}: $result"
    return 1
}

update() {
    [ "$(id -u)" = 0 ] || die "run it as root: sudo $0 update"
    site_name
    if [ "$PULLED" = 0 ]; then
        step "New commits (section 13)"
        local old new
        as_owner git -C "$REPO" fetch --quiet
        old=$(as_owner git -C "$REPO" rev-parse HEAD)
        new=$(as_owner git -C "$REPO" rev-parse '@{upstream}')
        if [ "$old" = "$new" ]; then
            note "none: $(as_owner git -C "$REPO" log -1 --format='%h %s')"
        else
            as_owner git -C "$REPO" log --oneline "$old..$new"
            if [ "$SKIP_CI" = 0 ] && ! ci_passed "$new"; then
                ask "Deploy anyway?" || die "nothing changed; --skip-ci deploys without the CI"
            fi
            as_owner git -C "$REPO" merge --ff-only --quiet '@{upstream}'
            local changed
            changed=$(as_owner git -C "$REPO" diff --name-only "$old" "$new")
            if echo "$changed" | grep -qE '^(findaline\.env\.example|\.env\.example)$'; then
                warn "new or changed settings: git -C $REPO diff ${old:0:8} -- findaline.env.example .env.example"
            fi
            # The new version of this script does the rest
            exec bash "$DEPLOY/vps.sh" update --pulled
        fi
    fi

    step "The files of deploy/ (sections 2, 4, 5, 11)"
    CHANGED=0
    nginx_files
    if [ "$CHANGED" = 1 ]; then nginx_reload; note "nginx reloaded"; fi
    CHANGED=0
    fail2ban_files
    if [ "$CHANGED" = 1 ]; then systemctl restart fail2ban; note "fail2ban restarted"; fi
    CHANGED=0
    ufw_files
    if [ "$CHANGED" = 1 ]; then ufw reload >/dev/null; note "ufw reloaded"; fi
    [ -f /etc/systemd/system/findaline-backup.service ] && backups
    note "up to date"

    start
    compose logs --tail 20
    local item
    for item in "${TODO[@]}"; do note "- $item"; done
}

setup() {
    check_system
    install_packages
    [ -n "$DISK_IMAGE" ] && disk_image
    install_docker
    ssh_key_only
    firewall
    # The site's name first: nginx needs it, findaline.env keeps it
    step "The site"
    [ -f "$REPO/findaline.env" ] \
        || install -m 644 -o "$(owner)" -g "$(id -gn "$(owner)")" "$REPO/findaline.env.example" "$REPO/findaline.env"
    if [ -n "$SITE" ]; then
        site_name
        [ "$(env_get "$REPO/findaline.env" FINDALINE_PUBLIC_URL)" = "https://$SITE" ] \
            || env_set "$REPO/findaline.env" FINDALINE_PUBLIC_URL "https://$SITE"
    fi
    site_name
    note "$SITE"
    nginx_and_certificate
    fail2ban
    security_updates
    settings
    if [ "$SETTINGS_READY" = 1 ]; then
        start
        backups
        check || todo "some checks failed (above)"
    fi
    step "Summary"
    if [ "${#TODO[@]}" -eq 0 ]; then
        note "done: https://$SITE/ (the checks of section 12 that need a browser or another network: sign-in, rate limits, the jail)"
        return 0
    fi
    local item
    for item in "${TODO[@]}"; do note "- $item"; done
    note "then: sudo $DEPLOY/vps.sh setup"
    return 2
}

main() {
    local command=${1:-}
    shift || true
    while [ $# -gt 0 ]; do
        case $1 in
            --site) SITE=${2:?--site needs a name}; shift ;;
            --email) EMAIL=${2:?--email needs an address}; shift ;;
            --contact) CONTACT=${2:?--contact needs a value}; shift ;;
            --my-ip) MY_IP=${2:?--my-ip needs an address}; shift ;;
            --data-root) DATA_ROOT=${2:?--data-root needs a folder}; shift ;;
            --disk-image) DISK_IMAGE=${2:?--disk-image needs a size}; shift ;;
            --skip-ci) SKIP_CI=1 ;;
            --yes) YES=1 ;;
            --pulled) PULLED=1 ;;
            *) die "unknown option $1 (see the head of $0)" ;;
        esac
        shift
    done
    if [ -n "$DISK_IMAGE" ]; then
        DATA_ROOT=${DATA_ROOT:-/srv/docker}
        [[ $DATA_ROOT == /?* ]] || die "--data-root: an absolute path"
    fi
    if [ -n "$MY_IP" ]; then
        [[ $MY_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--my-ip: an IPv4 address"
    fi
    case $command in
        setup) setup ;;
        update) update ;;
        check) [ "$(id -u)" = 0 ] || die "run it as root: sudo $0 check"; check ;;
        *) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 1 ;;
    esac
}

# Sourced (tests/nginx_check.sh), it only defines the functions
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
