# Deployment on a home server

How to run the public site of Find-a-Line (`findaline.py web --public` and `worker --public`, in
Docker) on a home Linux server that already runs [SWAG](https://github.com/linuxserver/docker-swag)
(nginx, Certbot, fail2ban) as the reverse proxy of other services, with ports 80 and 443 of the
router forwarded to it. Follow the sections in order; every command is meant to be copied.

Throughout, replace:
- `findaline.example.org` with the address of the site (the subdomain `findaline` of your domain);
- `/srv/find-a-line` with the folder of the checkout;
- `/srv/swag` with the folder of SWAG's `/config` (the volume or bind mount of your SWAG
  container), and `swag` with the name of its container;
- `192.168.1.0/24` with your LAN, `192.168.1.1` with the router.

What the site needs from the outside, and what stays closed:

| From | To | What |
| --- | --- | --- |
| the internet | SWAG (80, 443) | the site; SWAG sends it to `web` in the network `findaline-proxy` |
| `web` | the internet (HTTPS) | sign-in: GitHub's and Google's token endpoints, Google's OpenID metadata and keys |
| `worker` | the internet (HTTP/HTTPS) | the data services, Nominatim, Overpass, OpenStreetMap extracts |
| `web`, `worker` | the LAN, the host, other containers | nothing: the firewall rules below |
| `worker` | `web`, SWAG | nothing: they share no network (they talk through the data volume) |

## 1. Prerequisites
- A Linux server (Debian 13 or Ubuntu 24.04 are assumed below) with Docker Engine and the Compose
  plugin from Docker's repository (`docker compose version`), git, `sqlite3`, `restic`, `ufw`:
  `sudo apt install git sqlite3 restic ufw`.
- Cores and memory for the searches: the worker takes `WORKER_CPUS` cores (all but one) and
  `WORKER_MEMORY` (8 GB by default), the web service 1 core and 1 GB. Disk: the data volume holds
  the cache of elevation data, orthophotos and OpenStreetMap extracts and the saved runs; the site
  accepts no new searches below 20 GB free (`FINDALINE_LIMIT_FREE_DISK_GB_MIN`).
- SWAG running, with a domain pointing at your public address (a DNS record you manage, or a
  dynamic DNS name) and ports 80 and 443 of the router forwarded to it.
- An e-mail address or URL where the operators of Nominatim, Overpass and the data services can
  reach you (`FINDALINE_CONTACT`, sent in the User-Agent).
- The checkout: `sudo git clone https://github.com/<you>/Find-a-Line.git /srv/find-a-line`
  (or your own remote), owned by your user: `sudo chown -R $USER: /srv/find-a-line`.

## 2. DNS and the certificate
SWAG's certificate must cover `findaline.example.org`. Either:
- **a subdomain**: add it to `SUBDOMAINS` of SWAG's container (e.g. `SUBDOMAINS=www,findaline`
  with `URL=example.org`), create a DNS record for `findaline` (a CNAME to your main name, or an
  A/AAAA record to your public address) and recreate SWAG (`docker compose up -d swag` in its
  folder); SWAG requests the new certificate at start (`docker logs swag` shows it);
- **or a wildcard certificate**: `SUBDOMAINS=wildcard`, `VALIDATION=dns` with your DNS provider's
  plugin (`DNSPLUGIN=` and its credentials in `/srv/swag/dns-conf/<provider>.ini`); then only the
  DNS record for `findaline` (or a wildcard record) is needed.

If your DNS provider proxies traffic (Cloudflare's orange cloud), turn the proxy off for this name:
otherwise every request reaches SWAG from the provider's addresses, and the per-address limits
and fail2ban would hit everyone at once.

## 3. The network between SWAG and the site
SWAG reaches the web service through a network of their own, `findaline-proxy`. It is internal
(no traffic leaves it), has a fixed subnet, and its bridge has a fixed name for the firewall.
SWAG gets the fixed address `172.30.0.2`; the containers get theirs from `172.30.0.128/25`, so
none of them can take it first. Create it once:

```sh
docker network create --internal --subnet 172.30.0.0/24 --ip-range 172.30.0.128/25 \
  --opt com.docker.network.bridge.name=br-fal-proxy findaline-proxy
```

If the subnet is taken (`Pool overlaps with other one on this address space`), pick another
private /24 that `docker network inspect $(docker network ls -q) | grep Subnet` does not list,
and use it everywhere below (SWAG's address, `FINDALINE_TRUSTED_PROXY`).

Join SWAG to it. In SWAG's `compose.yaml` (keep its existing networks; a service without a
`networks:` key is on `default`):

```yaml
services:
  swag:
    # ... as before ...
    networks:
      default:
      findaline-proxy:
        ipv4_address: 172.30.0.2

networks:
  findaline-proxy:
    external: true
```

then `docker compose up -d swag` in SWAG's folder. (A SWAG started with `docker run`:
`docker network connect --ip 172.30.0.2 findaline-proxy swag`; it stays connected across
restarts, but not when the container is created again.) Check:
`docker inspect swag --format '{{json .NetworkSettings.Networks}}'` lists `findaline-proxy` with
`"IPAddress":"172.30.0.2"`.

## 4. SWAG configuration
The files are in `deploy/swag/` of the checkout.

1. The http-context part (rate limit zones, the masked log format):
   ```sh
   sudo cp /srv/find-a-line/deploy/swag/00-findaline-http.conf /srv/swag/nginx/site-confs/
   ```
   SWAG's `nginx.conf` includes `site-confs/*.conf` inside its `http` block in alphabetical
   order, and `default.conf` there includes the proxy confs; the file must keep its `00-` prefix
   so it comes first (otherwise `nginx -t` says `unknown log format "findaline"`).
2. The site:
   ```sh
   sudo cp /srv/find-a-line/deploy/swag/findaline.subdomain.conf /srv/swag/nginx/proxy-confs/
   ```
   It answers `server_name findaline.*`; with another subdomain change that line. It proxies to
   `findaline-web:8765` (the web service's alias in `findaline-proxy`) through SWAG's
   `resolver.conf` (Docker's DNS, `127.0.0.11`), so SWAG starts even while the site is down.
   What it sets: `client_max_body_size 2m`; per client address 20 requests/s with a burst of 200
   (a map view loads dozens of orthophoto tiles at once) and 40 connections, 10 requests/minute
   with a burst of 10 for `/login/` and `/auth/` (beyond them: 429); `/api/events` (the live
   event stream) without buffering and with a read timeout of an hour; gzip for scripts, JSON,
   CSS and CSV (a run's `data.js` shrinks about 4.7 times); HSTS; its own access log,
   `/config/log/nginx/findaline.access.log`, where the token of a share link and the query of a
   sign-in callback are replaced (`/s/<token>/map.html`, `/auth/github?<query>`), also in the
   Referer. Requests refused by the limits are not written to the error log (it would show the
   request unmasked). SWAG's logrotate rotates both logs weekly and keeps 14.
3. **Security headers.** The app sends its own (Content-Security-Policy, X-Frame-Options,
   Referrer-Policy, X-Content-Type-Options, Permissions-Policy, Cross-Origin-Opener-Policy and
   -Resource-Policy). SWAG's `ssl.conf` (shared by all your sites) may add headers too: the
   current sample sends `Strict-Transport-Security` (HSTS), older copies have it commented out,
   and it lists optional ones, commented out (CSP, X-Frame-Options, Referrer-Policy...). nginx
   passes `add_header` of the server level (where `ssl.conf` is included) to a location only if
   the location has none of its own; every location of `findaline.subdomain.conf` adds HSTS
   itself, so nothing of `ssl.conf` reaches this site, and no header is ever sent twice, whatever
   you enable there for your other services. HSTS for this site therefore needs no change of
   `ssl.conf`. Do not add headers to the `server` block of `findaline.subdomain.conf` (they would
   not apply); add them to each location.
4. fail2ban: the filter and the jail.
   ```sh
   sudo cp /srv/find-a-line/deploy/swag/fail2ban/findaline.conf /srv/swag/fail2ban/filter.d/
   cat /srv/find-a-line/deploy/swag/fail2ban/jail.findaline.local | sudo tee -a /srv/swag/fail2ban/jail.local
   ```
   The jail `findaline` reads the site's own log and bans an address with more than 100
   refusals (401 sign-in required, 403 forbidden, 429 too many requests) in 10 minutes for an
   hour, at SWAG's firewall (SWAG's iptables, all ports). That is far above normal use: an
   expired session or an account waiting for approval gives a few 401/403 per page load, the
   app's own limits a few 429 a minute. SWAG's `[DEFAULT]` `ignoreip` skips private addresses,
   so nobody on the LAN is banned. SWAG's own jails (`nginx-unauthorized` bans after 5 401s)
   read `access.log`, which this site does not write to.
5. Check and restart:
   ```sh
   docker exec swag nginx -t
   docker restart swag
   docker exec swag fail2ban-client status findaline
   ```
   (`nginx -t` must say `syntax is ok` and `test is successful`; the restart also copies the
   fail2ban files into place.) Until the site runs (section 9), SWAG answers 502 for it.

## 5. Firewall
Docker publishes ports through its own iptables rules, past `ufw`: SWAG's 80 and 443 are open
whatever `ufw` says, and `ufw` rules do not apply to traffic forwarded to and from containers.
Hence two parts: `ufw` for the host itself, and rules in Docker's `DOCKER-USER` chain (which
Docker runs before its own for forwarded traffic and never changes) for the containers.

**The host** (incoming traffic denied by default; SSH only from the LAN and only with a key):

```sh
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
```

Check that you can log in with your key (`ssh -o PasswordAuthentication=no <server>` from
another terminal), then turn passwords off:

```sh
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' \
  | sudo tee /etc/ssh/sshd_config.d/10-key-only.conf
sudo sshd -t && sudo systemctl reload ssh
```

and, keeping the current session open, log in again from another terminal before going on.

**The containers** may reach the internet only. The containers of Find-a-Line sit on the
bridges `br-fal-proxy`, `br-fal-web` and `br-fal-worker` (named in compose.yaml and in the
command of section 3). Two sets of rules:
- forwarded traffic (`DOCKER-USER`): nothing from these bridges to 10.0.0.0/8, 172.16.0.0/12
  (other Docker networks; their own bridge stays allowed), 192.168.0.0/16, 169.254.0.0/16 (link
  local, cloud metadata) and 100.64.0.0/10 (carrier-grade NAT, Tailscale); other containers,
  SWAG included, are not affected (SWAG may keep proxying to services on the LAN);
- traffic from these bridges to the host itself (`INPUT`, `ufw`): nothing.

```sh
sudo cp /etc/ufw/after.rules /etc/ufw/after.rules.bak
cat /srv/find-a-line/deploy/firewall/ufw-after.rules | sudo tee -a /etc/ufw/after.rules
sudo ufw insert 1 deny in on br-fal-proxy
sudo ufw insert 1 deny in on br-fal-web
sudo ufw insert 1 deny in on br-fal-worker
sudo ufw enable        # answer y: the SSH rule above keeps your session
sudo ufw reload
sudo iptables -S DOCKER-USER
sudo ufw status numbered
```

`iptables -S DOCKER-USER` must list the rules of `ufw-after.rules`. If it listed rules of your
own before (`sudo iptables -S DOCKER-USER` before the `tee`), add them to the block in
`/etc/ufw/after.rules`: the block empties the chain on every reload. The rules persist: `ufw`
loads them at boot, before Docker starts, and Docker leaves `DOCKER-USER` alone. They need
Docker's default iptables firewall (not the experimental nftables one, `docker info | grep -i
firewall`).

**DNS.** Docker's embedded DNS server (127.0.0.11 in every container) forwards names it does not
know to the host's resolvers, and it sends those queries from the container's own network, so
they pass the rules above (only when the host's resolver is on its loopback, `127.0.0.1` of
dnsmasq or Pi-hole, does Docker send them from the host; with systemd-resolved's `127.0.0.53` it
uses the servers behind it). A host whose resolver is the router would leave the containers
without DNS. compose.yaml therefore gives both services public resolvers (`dns:` 9.9.9.9 and
1.1.1.1, `CONTAINER_DNS_1`/`_2` in `.env` to change them). To use the router anyway, set
`CONTAINER_DNS_1=192.168.1.1` and uncomment the two port-53 lines in `/etc/ufw/after.rules`
(with the router's address), then `sudo ufw reload`.

**IPv6.** The networks of compose.yaml and `findaline-proxy` have no IPv6 (`enable_ipv6: false`,
no `--ipv6`), so the containers have no IPv6 address and no IPv6 route. If you ever enable it,
add the same block for IPv6 to `/etc/ufw/after6.rules` (`fc00::/7`, `fe80::/10` instead of the
IPv4 ranges).

**Test from inside the containers** (after section 9, while the site runs):

```sh
cd /srv/find-a-line
for service in web worker; do docker compose exec $service python -c "
import socket, urllib.request
for host, port in [('192.168.1.1', 80), ('192.168.1.1', 53), ('172.17.0.1', 22), ('169.254.169.254', 80)]:
    try:
        socket.create_connection((host, port), 3); print(host, port, 'REACHABLE: check the rules')
    except OSError as e:
        print(host, port, 'blocked', type(e).__name__)
print('internet:', urllib.request.urlopen('https://nominatim.openstreetmap.org/status', timeout=10).status)
"; done
```

Add the server's own LAN address with port 22 to the list. Every LAN address must say
`blocked` (a timeout), the internet `200`. Also: `docker compose exec worker python -c "import
socket; socket.create_connection(('findaline-web', 8765), 3)"` must fail (`gaierror`: the worker
does not even see the name).

**A separate VLAN.** If the router supports VLANs, put the server in its own (a DMZ) with
router rules: DMZ to LAN denied, LAN to DMZ only SSH and what you need. Then not only the
containers but the whole server is kept from the LAN.

## 6. Automatic security updates
```sh
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```
(answer *Yes*). The security updates of the system then install daily. Docker's own packages
come from Docker's repository; to include them, add `"origin=Docker";` to
`Unattended-Upgrade::Origins-Pattern` in `/etc/apt/apt.conf.d/50unattended-upgrades` (an
upgrade restarts the daemon, and the containers restart with it). The image of Find-a-Line is
updated by section 13, SWAG's by pulling its image.

## 7. Sign-in: GitHub and Google
The site keeps no passwords: visitors sign in with GitHub or Google (one of them is enough).
The callback addresses must match exactly.

**GitHub** (no scopes: the site reads the public user id and name):
1. github.com, *Settings*, *Developer settings*, *OAuth Apps*, *New OAuth App*.
2. *Application name* `Find-a-Line`, *Homepage URL* `https://findaline.example.org`,
   *Authorization callback URL* `https://findaline.example.org/auth/github`; leave *Enable
   Device Flow* off. *Register application*.
3. Copy the *Client ID* (it goes into `findaline.env` as `FINDALINE_GITHUB_CLIENT_ID`), then
   *Generate a new client secret* and copy it at once (it goes into
   `secrets/findaline_github_client_secret`).

**Google** (scopes `openid` and `profile`: an id and a name, no e-mail address):
1. console.cloud.google.com: create a project (e.g. `find-a-line`).
2. *Google Auth Platform* (*APIs & Services*, *OAuth consent screen*): *Get started*; app name
   `Find-a-Line`, a support e-mail; *Audience* *External*; your contact e-mail; agree.
3. *Branding*: the home page `https://findaline.example.org`, the privacy policy
   `https://findaline.example.org/privacy`, the authorized domain `example.org`. No logo (a
   logo needs Google's brand verification).
4. *Data Access*, *Add or remove scopes*: `openid` and `.../auth/userinfo.profile`; save.
5. *Audience*: *Publish app* (*In production*). In *Testing* only the listed test users can
   sign in. With these two non-sensitive scopes Google asks for no verification.
6. *Clients*, *Create client*: *Web application*, name `Find-a-Line`, *Authorized redirect
   URIs* `https://findaline.example.org/auth/google` (no JavaScript origins). *Create*, then
   copy the *Client ID* (`FINDALINE_GOOGLE_CLIENT_ID`) and the *Client secret*
   (`secrets/findaline_google_client_secret`).

## 8. Settings and secrets
```sh
cd /srv/find-a-line
cp findaline.env.example findaline.env
cp .env.example .env
cp -n deploy/secrets.example/* secrets/
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > secrets/findaline_session_secret
```

- `findaline.env` (both services): `FINDALINE_PUBLIC_URL=https://findaline.example.org`,
  `FINDALINE_TRUSTED_PROXY=172.30.0.2` (SWAG's address of section 3: only its
  `X-Forwarded-For` is believed), `FINDALINE_CONTACT`, `FINDALINE_GITHUB_CLIENT_ID` and/or
  `FINDALINE_GOOGLE_CLIENT_ID`, `FINDALINE_ADMINS` (below), `FINDALINE_SIGNUP` (`approval` by
  default: new accounts wait for an administrator), the limits (README, *Limits of the web app*).
- `.env` (compose itself): `WORKER_CPUS` (the number of cores minus one; `nproc` tells them),
  `WORKER_MEMORY`, `FINDALINE_MIRROR_DIR` (section 10), `CONTAINER_DNS_1`/`_2` (section 5).
- `secrets/` (secrets/README.md): the client secrets into their files (`nano
  secrets/findaline_github_client_secret`, the value alone), the file of a provider you do
  not use left empty, `opentopography_api_key` with an OpenTopography key for `--source
  nasadem` (administrators only) or empty. Each service gets only its own: the sign-in secrets
  the web service, the OpenTopography key the worker. They must be readable by the containers'
  user (uid 10001) and nobody else:
  ```sh
  sudo chown 10001:10001 secrets/findaline_* secrets/opentopography_api_key
  sudo chmod 400 secrets/findaline_* secrets/opentopography_api_key
  ```

**Administrators** (`FINDALINE_ADMINS`, comma-separated): `github:<numeric id>`, the id from
`curl -s https://api.github.com/users/<your login> | grep '"id"'` (the first `"id"`), and/or
`google:<sub>`. Google shows the `sub` nowhere: start the site with GitHub as administrator
(or with `FINDALINE_ADMINS` empty), sign in with Google once (the account waits for approval),
and read it:

```sh
docker compose exec web python -c "
import sqlite3
for row in sqlite3.connect('/data/accounts.sqlite').execute('SELECT id, provider, subject, name, status FROM users'):
    print(row)"
```

Then add `google:<subject>` to `FINDALINE_ADMINS`, `docker compose up -d` (the web service is
created again with the new settings) and sign in again: administrators are approved at sign-in.
The first administrator also owns the runs from before the public mode.

## 9. Start
```sh
cd /srv/find-a-line
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail 20
```

`web` must become `healthy` within a minute, the worker says `Find-a-Line worker ... is running
on /data`. Open `https://findaline.example.org/`: the sign-in page. Sign in as the
administrator: the *Admin* tab shows the worker, the free disk and the queue; in it, *OSM*
downloads the OpenStreetMap extracts the site needs (Poland, Malta, Spain, Switzerland,
Slovakia), so the coast and cliffs come without Overpass.

The containers' logs are rotated by Docker (10 MB, 5 files each: compose.yaml, `logging`).

## 10. The data volume and the Slovak mirror
Everything the site writes is in the named volume `findaline_data`
(`/var/lib/docker/volumes/findaline_data/_data` on the host): `cache/` (downloaded data,
re-downloadable), `runs/` (the users' runs), `work/` (temporary), `jobs.sqlite`,
`accounts.sqlite`, `audit.sqlite`, `home/` (numba's compiled code). Both services use it as uid
10001; SQLite in WAL mode needs it on a local file system (no NFS or SMB).

The Slovak tiles (`tools/sk_tiles.py`) are not downloaded by the site: copy the local mirror
(the folder above `slovakia/`) to the server, e.g. `/srv/findaline-mirror`, make it readable
(`sudo chmod -R a+rX /srv/findaline-mirror`), set `FINDALINE_MIRROR_DIR=/srv/findaline-mirror`
in `.env` and `docker compose up -d`. The worker sees it read-only at `/mirror`; without it
`--source auto` takes `europe` for Slovakia.

## 11. Backups
`deploy/backup.sh` (run as root):
1. copies each database with `sqlite3`'s `.backup` (consistent while the site runs) into
   `/var/backups/findaline/` and checks each copy (`PRAGMA integrity_check`): `jobs.sqlite`,
   `accounts.sqlite`, `audit.sqlite` and `cache/osm/nominatim.sqlite` (its answers save
   requests to Nominatim), with `cache/LAYOUT`; it runs `sqlite3` as uid 10001, so no file of
   the volume ends up owned by root;
2. backs up these copies and `runs/` with [restic](https://restic.net/) (encrypted,
   deduplicated) to a repository outside the house, then forgets old snapshots (7 daily, 5
   weekly, 12 monthly) and checks the repository.

The cache is not backed up (it is downloaded again), nor the Slovak mirror.

**The repository** (one of restic's backends: an SFTP server, S3-compatible storage such as
Backblaze B2, a rest-server at a friend's...). Settings in `/etc/findaline-backup.env`:

```sh
sudo install -m 600 /dev/null /etc/findaline-backup.env
sudo install -m 600 /dev/null /etc/findaline-restic-password
openssl rand -base64 32 | sudo tee /etc/findaline-restic-password >/dev/null
sudo tee /etc/findaline-backup.env >/dev/null <<'EOF'
RESTIC_REPOSITORY=s3:https://s3.eu-central-003.backblazeb2.com/<bucket>/findaline
RESTIC_PASSWORD_FILE=/etc/findaline-restic-password
AWS_ACCESS_KEY_ID=<key id>
AWS_SECRET_ACCESS_KEY=<key>
EOF
sudo bash -c 'set -a; . /etc/findaline-backup.env; restic init'
```

Keep a copy of the password outside the server (a password manager): without it the backup
cannot be read. A first run by hand:

```sh
sudo bash -c 'set -a; . /etc/findaline-backup.env; /srv/find-a-line/deploy/backup.sh'
```

**Daily, with a systemd timer**:

```sh
sudo tee /etc/systemd/system/findaline-backup.service >/dev/null <<'EOF'
[Unit]
Description=Find-a-Line backup (sqlite3 .backup + restic)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/findaline-backup.env
ExecStart=/srv/find-a-line/deploy/backup.sh
EOF
sudo tee /etc/systemd/system/findaline-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Daily Find-a-Line backup

[Timer]
OnCalendar=*-*-* 03:30
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now findaline-backup.timer
systemctl list-timers findaline-backup.timer
```

`journalctl -u findaline-backup` shows each run. (With cron instead: `30 3 * * * root set -a;
. /etc/findaline-backup.env; /srv/find-a-line/deploy/backup.sh` in `/etc/cron.d/findaline-backup`.)

**Test a restore** (once now, then every few months):

```sh
sudo bash -c 'set -a; . /etc/findaline-backup.env; restic snapshots; restic restore latest --target /tmp/findaline-restore'
for db in /tmp/findaline-restore/var/backups/findaline/*.sqlite; do echo "$db: $(sudo sqlite3 "$db" 'PRAGMA integrity_check')"; done
sudo ls /tmp/findaline-restore/var/lib/docker/volumes/findaline_data/_data/runs | head
```

**Restore for real** (the site stops meanwhile):

```sh
cd /srv/find-a-line
docker compose stop
V=/var/lib/docker/volumes/findaline_data/_data
R=/tmp/findaline-restore
sudo rm -f $V/jobs.sqlite* $V/accounts.sqlite* $V/audit.sqlite*   # with their -wal/-shm
sudo cp $R/var/backups/findaline/*.sqlite $V/
sudo mkdir -p $V/cache/osm
sudo rm -f $V/cache/osm/nominatim.sqlite*
sudo cp $R/var/backups/findaline/cache/osm/nominatim.sqlite $V/cache/osm/
sudo cp -n $R/var/backups/findaline/cache/LAYOUT $V/cache/
sudo rsync -a $R$V/runs/ $V/runs/
sudo chown -R 10001:10001 $V
docker compose up -d
```

Remove the old `-wal`/`-shm` files before copying a database in: SQLite would apply them to the
restored file.

## 12. Checks after start-up
From a computer outside the LAN (a phone's hotspot) where it says so: SWAG ignores the LAN for
bans.

1. **Headers**, once each, with HSTS:
   ```sh
   curl -sS -o /dev/null -D - https://findaline.example.org/
   ```
   One each of `content-security-policy`, `x-frame-options: DENY`, `referrer-policy`,
   `x-content-type-options`, `permissions-policy`, `cross-origin-opener-policy`,
   `cross-origin-resource-policy`, `Strict-Transport-Security: max-age=63072000`. (Use GET as
   above: the app answers `HEAD /` with 405.) Then `https://securityheaders.com` and SSL Labs.
2. **gzip**:
   ```sh
   curl -sS -o /dev/null -D - -H 'Accept-Encoding: gzip' https://findaline.example.org/app/app.js | grep -i content-encoding
   ```
   says `Content-Encoding: gzip`.
3. **The masked log**: open a share link (*Runs*, *share link*), then
   `docker exec swag tail -n 5 /config/log/nginx/findaline.access.log` shows
   `GET /s/<token>/map.html`, never the token.
4. **The event stream arrives live**: start a search and watch its progress move in *Queue*
   without reloading; or copy the value of the cookie `__Host-findaline` from the browser's
   developer tools and
   `curl -sN -H 'Cookie: __Host-findaline=<value>' https://findaline.example.org/api/events`
   prints `: ping` every 15 seconds.
5. **Rate limits and the jail** (outside the LAN):
   ```sh
   seq 400 | xargs -P 16 -I{} curl -s -o /dev/null -w '%{http_code}\n' https://findaline.example.org/api/schema | sort | uniq -c
   ```
   about 200 answers `401`, the rest `429`; `docker exec swag fail2ban-client status findaline`
   then lists the address as banned, and the site no longer answers it for an hour. Unban:
   `docker exec swag fail2ban-client set findaline unbanip <address>`.
6. **Memory**: start a search as large as the limits allow (an area of 50 km² at 1 m) and watch
   `docker stats findaline-worker-1`; afterwards
   `docker inspect -f '{{.State.OOMKilled}}' findaline-worker-1` says `false` and the search
   finished (the search sizes its tiles from the container's memory limit).
7. **Sign-in** with the real GitHub app (and Google): the account of an administrator lands in
   the app, another one on *waiting for approval*; approve it in *Admin*.
8. **The firewall test** of section 5, and a **restore test** of section 11.

## 13. Updates
1. Check the CI first: the commit you are about to deploy must have passed GitHub Actions (tests
   on Linux and Windows, the audits and the Docker job), on the repository's *Actions* page or
   `gh run list --limit 5`.
2. Then:
   ```sh
   cd /srv/find-a-line
   git pull --ff-only
   docker compose build --pull
   docker compose up -d
   docker compose ps
   docker compose logs --tail 30
   docker image prune -f
   ```
   A search running at that moment is stopped by the restart of the worker and marked
   interrupted (a large area resumes when it is run again).
3. Read the commit messages for changes to `deploy/` (copy the SWAG files again, `nginx -t`,
   `docker restart swag`), to `findaline.env.example` and `.env.example` (new settings) and for
   migrations of stored data (they run automatically at start; each commit says so).

## 14. Troubleshooting
- **502 Bad Gateway**: the web service is not running or SWAG does not find it.
  `docker compose ps` (healthy?), `docker exec swag getent hosts findaline-web` (must give an
  address in 172.30.0.128/25), `cat /srv/swag/nginx/resolver.conf` (must name `127.0.0.11`).
- **400 Bad Request ("Invalid host header")**: the address in the browser is not the host of
  `FINDALINE_PUBLIC_URL`.
- **`nginx -t`: `unknown log format "findaline"`**: `00-findaline-http.conf` is missing from
  `site-confs/` or renamed so it sorts after `default.conf`. **`duplicate ...`**: a directive of
  `proxy.conf` repeated in the server block of the site.
- **SWAG does not start after joining the network: `Address already in use`**: a container took
  172.30.0.2 (the network was created without `--ip-range`). `docker compose down`, remove and
  create the network again (section 3), start SWAG first.
- **Everyone shares one rate limit, or sign-in refuses with "too many sign-in attempts"**: the
  app does not believe SWAG's `X-Forwarded-For`: `FINDALINE_TRUSTED_PROXY` must be SWAG's
  address in `findaline-proxy` (`docker inspect swag`), then `docker compose up -d`.
- **Sign-in fails with `redirect_uri_mismatch` / "The redirect_uri is not associated with this
  application"**: the callback in the provider's settings differs from
  `https://findaline.example.org/auth/<provider>` (scheme, host, no trailing slash).
- **`--public needs these settings`** in `docker compose logs web`: a setting or secret is
  missing or wrong (the message names it); **`Permission denied`** on a secret: its owner
  (section 8).
- **The page says the worker is not running**: `docker compose logs worker`.
- **Progress appears only after reloading the page**: something buffers `/api/events`: another
  proxy in front of SWAG (Cloudflare), or a changed location in the SWAG conf.
- **Searches fail with DNS or connection errors**: the firewall (section 5): DNS to the router
  while `CONTAINER_DNS_1` is the router, or a public resolver blocked by your network. `docker
  compose exec worker python -c "import socket; print(socket.gethostbyname('nominatim.openstreetmap.org'))"`.
- **A search ends as killed, `OOMKilled` true**: raise `WORKER_MEMORY` in `.env` (and
  `docker compose up -d`), or lower the limits of searches.
- **You banned yourself**: `docker exec swag fail2ban-client set findaline unbanip <address>`.
- **`sqlite3: unable to open database` in the backup**: the volume's path (`FINDALINE_VOLUME` in
  `/etc/findaline-backup.env` if your Docker root is not `/var/lib/docker`).
