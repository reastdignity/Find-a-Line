# Deployment on a VPS

How to run the public site of Find-a-Line (`findaline.py web --public` and `worker --public`, in
Docker) on a virtual server with Ubuntu 26.04 LTS that runs nothing else on the web: nginx from
Ubuntu's packages on the host is the reverse proxy (with certbot for the certificate and fail2ban),
and the two services of compose.yaml run behind it. Follow the sections in order; every command is
meant to be copied.

Throughout, replace:
- `findaline.example.org` with the address of the site (set `SITE=` once in the shell where it is
  used: section 4);
- `/srv/find-a-line` with the folder of the checkout;
- `203.0.113.7` with a fixed address of your own (your home or office), where it is optional.

What the site needs from the outside, and what stays closed:

| From | To | What |
| --- | --- | --- |
| the internet | nginx on the host (80, 443) | the site; nginx sends it to `web` on `127.0.0.1:8765` |
| the internet | the host (22) | SSH, with a key only |
| `web` | the internet (HTTPS) | sign-in: GitHub's and Google's token endpoints, Google's OpenID metadata and keys |
| `worker` | the internet (HTTP/HTTPS) | the data services, Nominatim, Overpass, OpenStreetMap extracts |
| `web`, `worker` | the host, private addresses (the provider's private network, its metadata service), other containers | nothing: the firewall rules of section 2 |
| `worker` | `web` | nothing: they share no network (they talk through the data volume) |

**The script.** `deploy/vps.sh` runs the commands of these sections on the server; the sections
say what each step does and why, and remain the way to do it by hand. Left to you: the server
and, if the provider has them, a block volume for Docker (section 1), the DNS record (3), the sign-in apps at GitHub and
Google (7), the restic repository (11) and the checks that need a browser or another network
(12). After cloning the checkout (section 1):

```sh
cd /srv/find-a-line
sudo deploy/vps.sh setup --site findaline.example.org --email you@example.org --my-ip 203.0.113.7
```

Run it with `sudo` as your own user, not as root: SSH passwords are turned off only once this
user has a key in `~/.ssh/authorized_keys` and has logged in with it in the last week. `setup`
installs Docker from Docker's repository and the packages (section 1), turns SSH passwords and
root's login off and sets up `ufw` with the containers' rules (2), checks the DNS record (3),
installs nginx and, once the A record points at the server, the certificate (4), fail2ban (5)
and the automatic updates (6), writes `findaline.env`, `.env` (the worker's cores and, on a
server with less than 10 GB, its memory) and the secrets (8; in a terminal it asks for the
sign-in apps' client ids and secrets and for the GitHub login of the administrator), builds and
starts the site (9), installs the daily backup once `/etc/findaline-backup.env` exists (11) and
runs the checks of `deploy/vps.sh check`. Each step looks at what is already there, so run it
again after it stops: it ends with a list of what is still missing (the DNS record, a secret, a
reboot). Options:
- `--site`, the site's name (afterwards read from `FINDALINE_PUBLIC_URL`);
- `--email`, for Let's Encrypt's expiry notices and, without `--contact`, `FINDALINE_CONTACT`;
- `--my-ip`, a fixed address of your own: SSH from it only (`allow OpenSSH` is removed; the
  script asks first unless an SSH connection from it is open now) and never banned by fail2ban
  (`/etc/fail2ban/jail.d/00-ignore.local`); leave it out without a fixed address;
- `--data-root /srv/docker`, Docker's data on a volume of its own (section 1, *Disk*; only
  before the site's volumes exist);
- `--disk-image 200G`, without a block volume: a file of that size on the system disk with a file
  system of its own, mounted at `--data-root` (default `/srv/docker`) and made Docker's data root
  (section 1, *Disk*; only before the site's volumes exist);
- `--yes`, yes to its questions.

Later: `sudo deploy/vps.sh update` (section 13) and `sudo deploy/vps.sh check` (sections 2 and
12: the answers of nginx, the firewall from inside the containers, the jail, the certificate's
timer, SSH, the free disk).

## 1. Prerequisites
- A VPS with Ubuntu 26.04 LTS and a public IPv4 address, with Docker Engine and the Compose plugin
  from Docker's repository (docs.docker.com/engine/install/ubuntu; `docker compose version`), at
  least Docker Engine 28 (`docker version`): since 28.0 Docker drops packets that other machines
  route to the ports a container publishes on `127.0.0.1` (on a VPS, those of other customers in
  the provider's network could otherwise reach the web service past nginx). Also git, `sqlite3`,
  `restic`: `sudo apt install git sqlite3 restic`.
- **cgroup v2** (the default of Ubuntu): `stat -fc %T /sys/fs/cgroup` must say `cgroup2fs`, and
  `docker info` `Cgroup Version: 2`. The worker reads its container's memory and CPU limits from
  cgroup v2 to size the searches; under cgroup v1 it cannot, and warns at start (`docker compose
  logs worker`): a search could then plan for the whole machine and be killed at the memory limit.
- Cores and memory for the searches: the worker takes `WORKER_CPUS` cores (all but one) and
  `WORKER_MEMORY` (8 GB by default), the web service 1 core and 1 GB; nginx needs little.
- **Disk.** The data volume holds the cache of elevation data, orthophotos and OpenStreetMap
  extracts and the saved runs; it grows with every new area searched. The site accepts no new
  searches below 20 GB free (`FINDALINE_LIMIT_FREE_DISK_GB_MIN`), but the cache has no automatic
  eviction yet (planned: docs/roadmap.md), and a search or download already running may still
  fill the disk. On the system disk a full disk takes the system down with it (logs, nginx,
  Docker itself). Give the site a filesystem of its own before the first `docker compose up`,
  mounted (e.g. at `/srv/docker`) and made Docker's data root (`"data-root": "/srv/docker"` in
  `/etc/docker/daemon.json`, then `sudo systemctl restart docker`):
  - a block volume of the provider (it can usually grow later), formatted and mounted in
    `/etc/fstab` with `nofail` (`--data-root /srv/docker` of the script);
  - without one, a disk image: a file on the system disk with an ext4 of its own
    (`--disk-image 200G` of the script). The searches can fill the image, never the rest of the
    disk; leave the system at least 10 GB beside it. By hand:
    ```sh
    sudo fallocate -l 200G /srv/findaline-docker.img   # allocated whole, not a sparse file
    sudo chmod 600 /srv/findaline-docker.img
    sudo mkfs.ext4 -E nodiscard -L findaline-docker /srv/findaline-docker.img
    sudo mkdir -p /srv/docker
    echo '/srv/findaline-docker.img /srv/docker ext4 loop,nofail,X-fstrim.notrim 0 0' | sudo tee -a /etc/fstab
    sudo systemctl daemon-reload && sudo mount /srv/docker
    ```
    The image must stay allocated whole: a hole gives its space back to the system disk, and once
    that is full, writes into the image fail. `mkfs` would punch holes without `nodiscard`, and
    Ubuntu's weekly `fstrim.timer` without `X-fstrim.notrim`; never run `fstrim` on it by hand
    (`deploy/vps.sh check` finds holes; `sudo fallocate -l 200G /srv/findaline-docker.img` fills
    them again). To grow it later, online: `sudo fallocate -l 300G /srv/findaline-docker.img`,
    then with `D=$(findmnt -no SOURCE /srv/docker)`: `sudo losetup -c $D && sudo resize2fs $D`.

  With `nofail` the server boots without the file system, so Docker must wait for it, or it would
  start on the empty folder below it with no volumes:
  `/etc/systemd/system/docker.service.d/findaline-data-root.conf` with `[Unit]` and
  `RequiresMountsFor=/srv/docker`, then `sudo systemctl daemon-reload` (the script writes it
  whenever Docker's data root is a mount point). Watch the disk: `df -h /srv/docker` (or
  `/var/lib/docker`) and the free disk in the *Admin* tab. To free space by hand, delete old
  folders under the volume's `cache/` while no search runs (they are downloaded again when
  needed).
- A domain whose DNS you manage (section 3).
- An e-mail address or URL where the operators of Nominatim, Overpass and the data services can
  reach you (`FINDALINE_CONTACT`, sent in the User-Agent).
- The checkout: `sudo git clone https://github.com/<you>/Find-a-Line.git /srv/find-a-line`
  (or your own remote), owned by your user: `sudo chown -R $USER: /srv/find-a-line`.

## 2. SSH and the firewall
A VPS is on the internet from its first minute: do this first.

**SSH with a key only.** Log in as a user with `sudo` and your public key in
`~/.ssh/authorized_keys` (the provider's image often starts with `root` and your key: create the
user with `sudo adduser <name>`, `sudo usermod -aG sudo <name>`, copy the key to it). Check that
you can log in with your key (`ssh -o PasswordAuthentication=no <name>@<server>` from another
terminal), then turn passwords and root's login off:

```sh
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' \
  | sudo tee /etc/ssh/sshd_config.d/10-key-only.conf
sudo sshd -t && sudo systemctl reload ssh
```

and, keeping the current session open, log in again from another terminal before going on.

**The host** (incoming traffic denied by default; SSH, HTTP and HTTPS open):

```sh
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

With a fixed address of your own, open SSH to it only: `sudo ufw allow from 203.0.113.7 to any
port 22 proto tcp` instead of `allow OpenSSH` (keep the provider's web console at hand in case
that address changes). fail2ban's `sshd` jail (section 5) bans addresses that keep failing to log
in. If the provider has a firewall of its own in front of the server, allow 22, 80 and 443
there too (and nothing else): a second layer that does not depend on the host.

nginx runs on the host, so `ufw` applies to its ports as to any other service. Docker publishes
the web service on `127.0.0.1:8765` only (compose.yaml), which nothing outside the host can reach
(Docker 28+, section 1); `ufw` does not see Docker's own rules, which is why the containers have
the rules below.

**The containers** may reach the internet only. They sit on the bridges `br-fal-web` and
`br-fal-worker` (named in compose.yaml). Two sets of rules:
- forwarded traffic (`DOCKER-USER`): nothing from these bridges to 10.0.0.0/8, 172.16.0.0/12
  (other Docker networks; their own bridge stays allowed), 192.168.0.0/16 (the provider's private
  networks are usually in these three), 169.254.0.0/16 (link local: the provider's metadata
  service at 169.254.169.254, which may hand out the server's own credentials or startup data) and
  100.64.0.0/10 (carrier-grade NAT, Tailscale);
- traffic from these bridges to the host itself (`INPUT`, `ufw`): nothing, its public address
  included (SSH, nginx). A `ufw deny in on br-fal-...` rule alone is not enough: the stock
  `/etc/ufw/before.rules` accepts ICMP echo, the DHCP client, mDNS and SSDP (UPnP) before any user
  rule. The rule of `deploy/firewall/ufw-before.rules` drops everything from the bridges to the host
  ahead of those accepts (only replies to connections the host opened pass: nginx's connections to
  the web service); it goes into `before.rules`, which `ufw` loads at every start.

```sh
sudo cp /etc/ufw/after.rules /etc/ufw/after.rules.bak
sudo cp /etc/ufw/before.rules /etc/ufw/before.rules.bak
cat /srv/find-a-line/deploy/firewall/ufw-after.rules | sudo tee -a /etc/ufw/after.rules
sudo sed -i '/^# ok icmp codes for INPUT/e cat /srv/find-a-line/deploy/firewall/ufw-before.rules' /etc/ufw/before.rules
sudo grep -n -B3 -A1 'br-fal' /etc/ufw/before.rules
sudo ufw insert 1 deny in on br-fal-web
sudo ufw insert 1 deny in on br-fal-worker
sudo ufw enable        # answer y: the SSH rule above keeps your session
sudo ufw reload
sudo iptables -S DOCKER-USER
sudo iptables -S ufw-before-input | head -8
sudo ufw status numbered
```

`grep` must show the rule after the `INVALID` lines and before `# ok icmp codes for INPUT`
(if that line is missing in your `before.rules`, put the rule by hand after the line
`-A ufw-before-input -m conntrack --ctstate INVALID -j DROP`); `iptables -S ufw-before-input`
lists `-A ufw-before-input -i br-fal-+ -j DROP` before the first `-p icmp` rule.

`iptables -S DOCKER-USER` must list the rules of `ufw-after.rules` (if Docker is not installed yet,
the chain appears once it is, and `ufw reload` fills it). The block empties the chain on every
reload; rules of your own in it go into the block. The rules persist: `ufw` loads them at boot,
before Docker starts, and Docker leaves `DOCKER-USER` alone. They need Docker's default iptables
firewall (not the experimental nftables one, `docker info | grep -i firewall`).

**DNS.** Docker's embedded DNS server (127.0.0.11 in every container) forwards names it does not
know to the host's resolvers, and it sends those queries from the container's own network, so
they pass the rules above (with systemd-resolved's `127.0.0.53`, Ubuntu's default, it uses the
servers behind it: often the provider's, on a private or link-local address that the rules
block). compose.yaml therefore gives both services public resolvers (`dns:` 9.9.9.9 and 1.1.1.1,
`CONTAINER_DNS_1`/`_2` in `.env` to change them). To use the provider's resolver anyway, set
`CONTAINER_DNS_1` to its address and uncomment the two port-53 lines in `/etc/ufw/after.rules`
(with that address), then `sudo ufw reload`.

**IPv6.** The networks of compose.yaml have no IPv6 (`enable_ipv6: false`), so the containers have
no IPv6 address and no IPv6 route. If you ever enable it, add the same block for IPv6 to
`/etc/ufw/after6.rules` (`fc00::/7`, `fe80::/10` instead of the IPv4 ranges). The host's own IPv6
is covered by `ufw` as above (it applies the same rules to IPv6 by default).

**Test from inside the containers** (after section 9, while the site runs):

```sh
cd /srv/find-a-line
PUBLIC=$(curl -4s https://ifconfig.me)   # the server's public address
for service in web worker; do docker compose exec -e PUBLIC=$PUBLIC $service python -c "
import os, socket, urllib.request
for host, port in [(os.environ['PUBLIC'], 22), (os.environ['PUBLIC'], 443), ('172.30.0.1', 8765), ('172.17.0.1', 22),
                   ('169.254.169.254', 80), ('10.0.0.1', 80), ('192.168.0.1', 80)]:
    try:
        socket.create_connection((host, port), 3); print(host, port, 'REACHABLE: check the rules')
    except OSError as e:
        print(host, port, 'blocked', type(e).__name__)
print('internet:', urllib.request.urlopen('https://nominatim.openstreetmap.org/status', timeout=10).status)
"; done
```

Add the addresses of the provider's private network, if the server has one (`ip -4 addr`). Every
address must say `blocked` with `TimeoutError` (a `ConnectionRefusedError` means the packet reached
a host that answered), the internet `200`. Also: `docker compose exec worker python -c "import
socket; socket.create_connection(('web', 8765), 3)"` must fail (`gaierror`: the worker does not
even see the name).

**IPv6 for the site later** (an option, not needed to start). The site starts with an A record
only (section 3). nginx on the host sees IPv6 visitors with their own addresses, but one visitor
usually holds a whole /64: before an AAAA record is published, the limit zones should count per
/64 rather than per address (a `map` of the address in `findaline-http.conf`), and fail2ban
should ban the /64. This is planned (docs/roadmap.md), not part of the files here.

## 3. DNS
Create an A record for `findaline.example.org` with the server's public IPv4 address, and **no
AAAA record** (section 2, *IPv6 for the site later*). A CNAME to a name that has an AAAA record
brings the AAAA along: use an A record then. Check: `dig +short A findaline.example.org` prints the
server's address and `dig +short AAAA findaline.example.org` nothing.

If your DNS provider proxies traffic (Cloudflare's orange cloud), turn the proxy off for this name:
otherwise every request reaches nginx from the provider's addresses, the per-address limits and
fail2ban would hit everyone at once, and the provider would buffer the event stream.

## 4. nginx and the certificate
The files are in `deploy/nginx/` of the checkout:
- `findaline-http.conf` → `/etc/nginx/conf.d/`: the upstream (`127.0.0.1:8765`), the rate limit
  zones, the masked log format, the timeouts for slow clients. Ubuntu's `nginx.conf` includes
  `conf.d/*.conf` inside its `http` block before `sites-enabled/*`;
- `findaline-tls.conf` → `/etc/nginx/snippets/`: the TLS settings (Mozilla's "intermediate":
  TLS 1.2 and 1.3);
- `findaline-acme.conf` → `/etc/nginx/sites-available/findaline-acme`: port 80, certbot's
  challenges and a redirect to https; works before the certificate exists;
- `findaline.conf` → `/etc/nginx/sites-available/findaline`: the site on port 443, enabled once
  the certificate exists.

The two site files name `findaline.example.org`; `sed` puts your name in while copying.

```sh
sudo apt install nginx certbot
SITE=findaline.example.org
cd /srv/find-a-line
sudo cp deploy/nginx/findaline-http.conf /etc/nginx/conf.d/
sudo cp deploy/nginx/findaline-tls.conf /etc/nginx/snippets/
sed "s/findaline\.example\.org/$SITE/g" deploy/nginx/findaline-acme.conf | sudo tee /etc/nginx/sites-available/findaline-acme >/dev/null
sed "s/findaline\.example\.org/$SITE/g" deploy/nginx/findaline.conf | sudo tee /etc/nginx/sites-available/findaline >/dev/null
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -s /etc/nginx/sites-available/findaline-acme /etc/nginx/sites-enabled/
sudo mkdir -p /var/www/letsencrypt
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` must say `syntax is ok` and `test is successful`. Then the certificate from Let's
Encrypt (your e-mail for expiry notices), the site, and a test of the renewal:

```sh
sudo certbot certonly --webroot -w /var/www/letsencrypt -d $SITE -m you@example.org --agree-tos \
  --no-eff-email --deploy-hook 'systemctl reload nginx'
sudo ln -s /etc/nginx/sites-available/findaline /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew --dry-run
systemctl list-timers certbot.timer
```

certbot keeps the settings (webroot, the deploy hook) in `/etc/letsencrypt/renewal/`, and its
timer renews the certificate when it nears its end, then reloads nginx. Until the site runs
(section 9), nginx answers 502 for it.

What the configuration sets: `client_max_body_size 2m`; 10 s instead of nginx's 60 s for a slow
client's request headers and body (in the `http` block, so for every server: the headers of an
HTTP/1.1 request are read before nginx knows which site they are for); 400 for any request with an
`Upgrade` header (the app has no WebSockets); per client address 20 requests/s with a burst of 200
(a map view loads dozens of orthophoto tiles at once) and 40 connections, 10 requests/minute with a
burst of 10 for `/login/` and `/auth/` (beyond them: 429); `/api/events` (the live event stream)
without buffering and with a read timeout of an hour, other requests 620 s (an estimate may wait
for the worker's quick lane); gzip for scripts, JSON, CSS and CSV (a run's `data.js` shrinks about
4.7 times); HTTP/2; HSTS; no nginx version in the `Server` header. A request for another name (or
for the bare address of the server) gets no answer: port 80 closes the connection, port 443
refuses the TLS handshake, so scanners of the address never see the site's certificate.

**Security headers.** The app sends its own (Content-Security-Policy, X-Frame-Options,
Referrer-Policy, X-Content-Type-Options, Permissions-Policy, Cross-Origin-Opener-Policy and
-Resource-Policy); nginx adds `Strict-Transport-Security` at the server level of
`findaline.conf`. nginx passes `add_header` of the server level to a location only if the location
has none of its own: a header added to one location must be added to the others there too, and
HSTS repeated in it.

**What the logs of this site contain** (`/var/log/nginx/`, readable by root and the group `adm`;
Ubuntu's logrotate rotates them daily and keeps 14, compressed):
- `findaline.access.log`, one line per request: the client's address, the time, the request line
  with the token of a share link replaced by `<token>` and the query of `/auth/...` replaced by
  `<query>` (other queries stay: the app's own carry no secrets), the status, the size, the
  Referer masked the same way and the User-Agent. No cookies, no other headers. The requests of
  port 80 (redirects) are logged the same way.
- `findaline.error.log`, level `error` and above (not nginx's default `warn`): failures of the
  upstream (the web service down, refusing, timing out, an invalid answer), with the client's
  address, the request line and the Referer **unmasked**. Requests under `/s/` (share links),
  `/login/` and `/auth/` (sign-in), and `/cache/` (orthophoto tiles: a shared map asks for them with
  its own address, token included, as the Referer) log only `crit`, which a proxied request never
  reaches, so their addresses never appear there. A request of another path made from a shared
  map's page would show the token in the Referer; the viewer makes none, but a browser may ask for
  `/favicon.ico` with it. Warnings (an answer buffered to a temporary file) and the refusals of the
  limits (`info`) are not logged.
- `access.log` gets the requests for other names (masked the same way), `error.log` what nginx
  logs before a request is matched to a site (TLS handshakes, malformed requests).

## 5. fail2ban
```sh
sudo apt install fail2ban
sudo cp /srv/find-a-line/deploy/fail2ban/findaline.conf /etc/fail2ban/filter.d/
sudo cp /srv/find-a-line/deploy/fail2ban/findaline.local /etc/fail2ban/jail.d/
sudo systemctl restart fail2ban
sudo fail2ban-client status
sudo fail2ban-client status findaline
```

The jail `findaline` reads the site's own log and bans an address with more than 100 refusals
(401 sign-in required, 403 forbidden, 429 too many requests) in 10 minutes for an hour, on ports 80
and 443 of the host (Ubuntu's ban action, nftables; SSH stays open to it). That is far above normal
use: an expired session or an account waiting for approval gives a few 401/403 per page load, the
app's own limits a few 429 a minute. Ubuntu's package also enables the jail `sshd` (failed SSH
logins, read from the journal). Only the host itself is never banned: to keep your own fixed
address out of both jails, uncomment `ignoreip` in `/etc/fail2ban/jail.d/findaline.local` (with
your address) for the site, or set it under `[DEFAULT]` in a file of your own,
`/etc/fail2ban/jail.d/00-ignore.local`, for all jails, then `sudo systemctl restart fail2ban`.

## 6. Automatic security updates
```sh
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```
(answer *Yes*). The security updates of the system, nginx, certbot and fail2ban included, then
install daily. Docker's own packages come from Docker's repository; to include them, add
`"origin=Docker";` to `Unattended-Upgrade::Origins-Pattern` in
`/etc/apt/apt.conf.d/50unattended-upgrades` (an upgrade restarts the daemon, and the containers
restart with it). Some updates (the kernel) need a reboot: `cat /var/run/reboot-required` says so,
or set `Unattended-Upgrade::Automatic-Reboot "true";` there (with `-Time "04:00"`). The image of
Find-a-Line is updated by section 13.

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
  `FINDALINE_TRUSTED_PROXY=172.30.0.1` (as in the example: the gateway of the network `web` of
  compose.yaml, the address nginx's connections arrive from; only its `X-Forwarded-For` is
  believed), `FINDALINE_CONTACT`, `FINDALINE_GITHUB_CLIENT_ID` and/or
  `FINDALINE_GOOGLE_CLIENT_ID`, `FINDALINE_ADMINS` (below), `FINDALINE_SIGNUP` (`approval` by
  default: new accounts wait for an administrator), the limits (README, *Limits of the web app*).
  The public site has time limits by default: an hour for a search's download, two hours for
  its computing and for an OpenStreetMap extract, two minutes for an estimate or a profile
  (`FINDALINE_LIMIT_DOWNLOAD_SECONDS=3600`, `..._COMPUTE_SECONDS=7200`, `..._OSM_SECONDS=7200`,
  `..._QUICK_SECONDS=120`); set one in `findaline.env` only to change it (`none` removes it).
  The size of a search and each user's quotas have public values the same way: the largest
  radius, resolution, area and bounding box (`..._RADIUS_MAX=3500`, `..._RESOLUTION_MAX=30`,
  `..._AREA_KM2_MAX=50`, `..._BBOX_KM_MAX=75`), searches, km² and MB a day per user
  (`..._DOWNLOAD_MB_DAY_MAX=10000`) and the request rates (`..._JOB_RATE=10/minute;60/hour`);
  README's table lists all of them.
- `.env` (compose itself): `WORKER_CPUS` (the number of cores minus one; `nproc` tells them),
  `WORKER_MEMORY`, `FINDALINE_MIRROR_DIR` (section 10), `CONTAINER_DNS_1`/`_2` (section 2),
  `WEB_PORT` (the host's port of the web service, 8765; the upstream in `findaline-http.conf`
  names it too).
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
for row in sqlite3.connect('/accounts/accounts.sqlite').execute('SELECT id, provider, subject, name, status FROM users'):
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
on /data`. From the host, `curl -s -o /dev/null -w '%{http_code}\n' -H "Host: $SITE"
http://127.0.0.1:8765/` says `200`. Open `https://findaline.example.org/`: the sign-in page. Sign
in as the administrator: the *Admin* tab shows the worker, the free disk and the queue; in it,
*OSM* downloads the OpenStreetMap extracts the site needs (Poland, Malta, Spain, Switzerland,
Slovakia), so the coast and cliffs come without Overpass.

The containers' logs are rotated by Docker (10 MB, 5 files each: compose.yaml, `logging`).

## 10. The volumes and the Slovak mirror
What the site keeps is in two named volumes (under `/var/lib/docker/volumes/` on the host):
- `findaline_data`, both services, at `/data`: `cache/` (downloaded data, re-downloadable),
  `runs/` (the users' runs), `work/` (temporary), `jobs.sqlite` (the queue, through which the
  two services talk), `audit.sqlite`;
- `findaline_accounts`, the web service only, at `/accounts`: `accounts.sqlite` (the users and
  their sessions).

Both services run as uid 10001; SQLite in WAL mode needs the volumes on a local file system (no
NFS or SMB).

**What each service can write.** The worker runs the searches: it parses data from many outside
services, so it is the likelier one to be compromised by a bug in a parser; the web service
faces the visitors. Neither may plant what the other trusts:
- the accounts and sessions are not in the shared volume: a compromised worker cannot add a
  user, an administrator or a session (the web service alone mounts `findaline_accounts`);
- numba's compiled code (pickles, loaded by every search process), the home folder and
  matplotlib's settings are in each service's own `/tmp` (a tmpfs, `HOME`, `NUMBA_CACHE_DIR`,
  `MPLCONFIGDIR` in compose.yaml): a compromised web service cannot leave code for the worker to
  load, and the worker compiles its code again after each start of its container (a few
  seconds, in the first search).

An accepted residual risk: the worker writes the runs, including each run's `data.js`, which
its map page loads as a script. A compromised worker can therefore change what the map of a run
shows or does in the visitor's browser (its own origin, with the page's Content-Security-Policy),
and change or delete the runs, the cache and the queue. The viewer itself (its scripts and
styles) comes from the image, not from the volume. Keeping the worker from being compromised
(the image's updates, section 13) and its network isolation (section 2) are the defence here.

The Slovak tiles (`tools/sk_tiles.py`) are not downloaded by the site: copy the local mirror
(the folder above `slovakia/`) to the server, e.g. `/srv/findaline-mirror`, make it readable
(`sudo chmod -R a+rX /srv/findaline-mirror`), set `FINDALINE_MIRROR_DIR=/srv/findaline-mirror`
in `.env` and `docker compose up -d`. The worker sees it read-only at `/mirror`; without it
`--source auto` takes `europe` for Slovakia.

## 11. Backups
`deploy/backup.sh` (run as root):
1. copies each database with SQLite's backup API (consistent while the site runs) into
   `/var/backups/findaline/` and checks each copy (`PRAGMA integrity_check`): from the data
   volume `jobs.sqlite`, `audit.sqlite` and `cache/osm/nominatim.sqlite` (its answers save
   requests to Nominatim), with `cache/LAYOUT`, into `data/`; from the accounts volume
   `accounts.sqlite`, into `accounts/`. The copies are made in a throwaway container of the
   site's image (`findaline:local`) that mounts both volumes, runs as uid 10001 and has no
   network: on the host that uid cannot even enter Docker's root folder (`/var/lib/docker` is
   `0710 root`), and SQLite run as root could leave `-wal`/`-shm` files in the volumes that the
   site can no longer open;
2. backs up these copies and `runs/` (read by root from the data volume's folder) with
   [restic](https://restic.net/) (encrypted, deduplicated) to a repository outside the VPS,
   then forgets old snapshots (7 daily, 5 weekly, 12 monthly) and checks the repository.

Any failure (a copy that fails its check, restic) ends the script with an error before the next
step, and the systemd unit below reports it.

The cache is not backed up (it is downloaded again), nor the Slovak mirror.

**The repository** (one of restic's backends: an SFTP server, S3-compatible storage such as
Backblaze B2, a rest-server at home...), at another provider than the VPS: a backup must survive
the loss of the server and of its provider's account. Snapshots of the whole server by the
provider are a useful addition (a quick way back after a failed upgrade), not a replacement: they
are not consistent copies of the databases and live with the same provider. Settings in `/etc/findaline-backup.env`:

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

**Daily, with a systemd timer**, and a message when it fails: a backup that stops working is
noticed only when it is needed, so `OnFailure=` starts another unit when a run fails. The
example sends it with [ntfy](https://ntfy.sh) (a push message to your phone; pick a topic name
nobody can guess, or use your own ntfy server, and `sudo apt install curl`); any command works
the same way in its `ExecStart` (`mail` if the host can send mail, a webhook of your chat):

```sh
sudo tee /etc/systemd/system/notify-failure@.service >/dev/null <<'EOF'
[Unit]
Description=Report the failure of %i

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -fsS -m 30 -H "Title: %H: %i failed" -d "journalctl -u %i on %H" https://ntfy.sh/<your secret topic>
EOF
sudo tee /etc/systemd/system/findaline-backup.service >/dev/null <<'EOF'
[Unit]
Description=Find-a-Line backup (SQLite copies + restic)
Wants=network-online.target
After=network-online.target docker.service
OnFailure=notify-failure@%N.service

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
sudo systemctl start notify-failure@findaline-backup.service   # a test message
sudo systemctl enable --now findaline-backup.timer
systemctl list-timers findaline-backup.timer
```

`journalctl -u findaline-backup` shows each run. The same `OnFailure=` line fits any other unit
of yours. (With cron instead: `30 3 * * * root set -a; . /etc/findaline-backup.env;
/srv/find-a-line/deploy/backup.sh` in `/etc/cron.d/findaline-backup`; cron mails a failed run's
output to root only if the host can send mail.)

**Test a restore** (once now, then every few months):

```sh
sudo bash -c 'set -a; . /etc/findaline-backup.env; restic snapshots; restic restore latest --target /tmp/findaline-restore'
sudo find /tmp/findaline-restore/var/backups/findaline -name '*.sqlite' -exec sh -c 'echo "$1: $(sqlite3 "$1" "PRAGMA integrity_check")"' _ {} \;
sudo ls /tmp/findaline-restore/var/lib/docker/volumes/findaline_data/_data/runs | head
```

Each database must say `ok`, and `runs` must list the runs.

**Restore for real** (the site stops meanwhile):

```sh
cd /srv/find-a-line
docker compose stop
V=/var/lib/docker/volumes/findaline_data/_data
A=/var/lib/docker/volumes/findaline_accounts/_data
R=/tmp/findaline-restore
B=$R/var/backups/findaline
sudo rm -f $V/jobs.sqlite* $V/audit.sqlite* $A/accounts.sqlite*   # with their -wal/-shm
sudo cp $B/data/jobs.sqlite $B/data/audit.sqlite $V/
sudo cp $B/accounts/accounts.sqlite $A/
sudo mkdir -p $V/cache/osm
sudo rm -f $V/cache/osm/nominatim.sqlite*
sudo cp $B/data/cache/osm/nominatim.sqlite $V/cache/osm/
sudo cp -n $B/data/cache/LAYOUT $V/cache/
sudo rsync -a $R$V/runs/ $V/runs/
sudo chown -R 10001:10001 $V $A
sudo chmod 644 $V/*.sqlite $V/cache/osm/nominatim.sqlite $A/accounts.sqlite
docker compose up -d
```

(Paths of a Docker root other than `/var/lib/docker`: `docker volume inspect findaline_data
--format '{{.Mountpoint}}'`.)

Remove the old `-wal`/`-shm` files before copying a database in: SQLite would apply them to the
restored file.


## 12. Checks after start-up
From a computer outside the server, on a network whose address is not in `ignoreip` where it says
so (section 5).

1. **Headers**, once each, with HSTS:
   ```sh
   curl -sS -o /dev/null -D - https://findaline.example.org/
   ```
   `HTTP/2 200`, one each of `content-security-policy`, `x-frame-options: DENY`,
   `referrer-policy`, `x-content-type-options`, `permissions-policy`, `cross-origin-opener-policy`,
   `cross-origin-resource-policy`, `strict-transport-security: max-age=63072000`, and `server: nginx`
   without a version. (Use GET as above: the app answers `HEAD /` with 405.) Then
   `https://securityheaders.com` and SSL Labs (grade A expected).
2. **gzip**:
   ```sh
   curl -sS -o /dev/null -D - -H 'Accept-Encoding: gzip' https://findaline.example.org/app/app.js | grep -i content-encoding
   ```
   says `content-encoding: gzip`.
3. **The masked log**: open a share link (*Runs*, *share link*), then
   `sudo tail -n 5 /var/log/nginx/findaline.access.log` shows `GET /s/<token>/map.html`, never the
   token.
4. **The event stream arrives live**: start a search and watch its progress move in *Queue*
   without reloading; or copy the value of the cookie `__Host-findaline` from the browser's
   developer tools and
   `curl -sN -H 'Cookie: __Host-findaline=<value>' https://findaline.example.org/api/events`
   prints `: ping` every 15 seconds.
5. **Rate limits and the jail** (from an address not in `ignoreip`):
   ```sh
   seq 400 | xargs -P 16 -I{} curl -s -o /dev/null -w '%{http_code}\n' https://findaline.example.org/api/schema | sort | uniq -c
   ```
   about 200 answers `401`, the rest `429`; `sudo fail2ban-client status findaline` then lists the
   address as banned, and the site no longer answers it for an hour (SSH still does). Unban:
   `sudo fail2ban-client set findaline unbanip <address>`.
6. **Memory**: start a search as large as the limits allow (an area of 50 km² at 1 m) and watch
   `docker stats findaline-worker-1`; afterwards
   `docker inspect -f '{{.State.OOMKilled}}' findaline-worker-1` says `false` and the search
   finished (the search sizes its tiles from the container's memory limit).
7. **Sign-in** with the real GitHub app (and Google): the account of an administrator lands in
   the app, another one on *waiting for approval*; approve it in *Admin*.
8. **The firewall test** of section 2, and a **restore test** of section 11.
9. **The visitors' real addresses**: open the site from a phone on mobile data, look up the
   phone's public address (a "what is my IP" page), then
   `sudo tail -n 5 /var/log/nginx/findaline.access.log`: the lines start with that address. The app
   sees the same address only if it trusts nginx: `FINDALINE_TRUSTED_PROXY` must be the gateway of
   the network `web` (section 14, *Everyone shares one rate limit*).

## 13. Updates
`sudo deploy/vps.sh update` does the steps below: it checks the CI of the new commits (GitHub's
check runs of the newest one; a private repository's are not readable without a token, so it
asks, and `--skip-ci` leaves the check out), pulls them, installs the files of `deploy/` again
where they changed (nginx reloaded, fail2ban restarted, the blocks of `ufw`'s files replaced and
`ufw` reloaded), builds the image, restarts the services and warns when the examples of the
settings changed. By hand:

1. Check the CI first: the commit you are about to deploy must have passed GitHub Actions (tests
   on Linux and Windows, the audits and the Docker job, which also runs nginx and fail2ban with
   these files), on the repository's *Actions* page or `gh run list --limit 5`.
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
3. Read the commit messages for changes to `deploy/` (`deploy/vps.sh update` installs them) (copy the nginx files again with the commands
   of section 4 from `SITE=` on, without the certificate, then `sudo nginx -t && sudo systemctl
   reload nginx`; the fail2ban files of section 5 and `sudo systemctl restart fail2ban`; the
   firewall files of section 2: restore the `.bak` copies and add the block and the rule again),
   to `findaline.env.example` and `.env.example` (new settings) and for migrations of stored data
   (they run automatically at start; each commit says so).

## 14. Troubleshooting
- **502 Bad Gateway**: the web service is not running or does not answer on `127.0.0.1:8765`.
  `docker compose ps` (healthy?), `curl -s -o /dev/null -w '%{http_code}\n' -H "Host: $SITE"
  http://127.0.0.1:8765/` on the host (must say 200), `sudo tail /var/log/nginx/findaline.error.log`.
  With `WEB_PORT` in `.env`, the upstream of `findaline-http.conf` must name the same port.
- **400 Bad Request ("Invalid host header")**: the address in the browser is not the host of
  `FINDALINE_PUBLIC_URL`.
- **`nginx -t`: `unknown log format "findaline"`** or **`no "ssl_certificate" is defined`**:
  `conf.d/findaline-http.conf` is missing, or the site of port 443 was enabled before the
  certificate existed (`sudo certbot certificates`). **`"..." directive is duplicate`**: a
  directive that Ubuntu's `nginx.conf` sets in its `http` block (`server_tokens`, `ssl_protocols`,
  `gzip`) was added to `conf.d/`; set it per server.
- **certbot fails** (`Timeout during connect`, `unauthorized`): the A record does not point at the
  server yet (`dig +short A $SITE`), port 80 is closed (`ufw`, the provider's firewall), or
  `findaline-acme` is not enabled (`curl http://$SITE/.well-known/acme-challenge/x` must say 404
  from nginx, not time out).
- **Everyone shares one rate limit, or sign-in refuses with "too many sign-in attempts"**: the
  app does not believe nginx's `X-Forwarded-For`. `FINDALINE_TRUSTED_PROXY` must be the gateway of
  the network `web`: `docker network inspect findaline_web --format '{{(index .IPAM.Config 0).Gateway}}'`
  says `172.30.0.1` (a network created by an older compose.yaml keeps its old subnet: `docker
  compose down` and `up -d` create it again), then `docker compose up -d`.
- **Sign-in fails with `redirect_uri_mismatch` / "The redirect_uri is not associated with this
  application"**: the callback in the provider's settings differs from
  `https://findaline.example.org/auth/<provider>` (scheme, host, no trailing slash).
- **`--public needs these settings`** in `docker compose logs web`: a setting or secret is
  missing or wrong (the message names it); **`Permission denied`** on a secret: its owner
  (section 8).
- **The page says the worker is not running**: `docker compose logs worker`.
- **Progress appears only after reloading the page**: something buffers `/api/events`: another
  proxy in front of nginx (Cloudflare), or a changed location in `findaline.conf`.
- **Searches fail with DNS or connection errors**: the firewall (section 2): DNS to the provider's
  resolver while `CONTAINER_DNS_1` is that resolver, or a public resolver blocked by the provider.
  `docker compose exec worker python -c "import socket; print(socket.gethostbyname('nominatim.openstreetmap.org'))"`.
- **A search ends as killed, `OOMKilled` true**: raise `WORKER_MEMORY` in `.env` (and
  `docker compose up -d`), or lower the limits of searches.
- **You banned yourself**: over SSH, `sudo fail2ban-client set findaline unbanip <address>`; then
  `ignoreip` (section 5).
- **`no Docker volume findaline_...` in the backup**: the volumes are named after the folder of
  compose.yaml's project (`name: findaline`); `docker volume ls` lists them, and
  `FINDALINE_DATA_VOLUME`/`FINDALINE_ACCOUNTS_VOLUME` in `/etc/findaline-backup.env` name others.
  **`Unable to find image 'findaline:local'`**: the site was never built on this host
  (section 9), or `FINDALINE_IMAGE` names another image.
