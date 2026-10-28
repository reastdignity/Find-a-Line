#!/usr/bin/env bash
# Find-a-Line: back up the public site (docs/deployment.md, "Backups"). Run as root on the host,
# by hand or from findaline-backup.timer, with the settings of /etc/findaline-backup.env:
#   RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE (and the credentials of the repository's service)
#   FINDALINE_VOLUME   the data volume on the host (default: the volume findaline_data)
#   BACKUP_STAGE       where the consistent copies of the databases go (default below)
#
# 1. A consistent copy of each SQLite database with sqlite3's .backup, which is safe while the
#    site runs (WAL mode), made as the containers' user (uid 10001): sqlite3 run as root could
#    leave -wal/-shm files the app can no longer open. Each copy is checked (integrity_check).
# 2. restic: the copies and runs/ into the repository (encrypted, deduplicated, off-site),
#    then the old snapshots are forgotten and the repository checked.
# The rest of the cache is downloaded again when needed, the Slovak mirror is a copy anyway.
set -euo pipefail

DATA=${FINDALINE_VOLUME:-/var/lib/docker/volumes/findaline_data/_data}
STAGE=${BACKUP_STAGE:-/var/backups/findaline}
DATABASES=(jobs.sqlite accounts.sqlite audit.sqlite cache/osm/nominatim.sqlite)
: "${RESTIC_REPOSITORY:?set it in /etc/findaline-backup.env}"
: "${RESTIC_PASSWORD_FILE:?set it in /etc/findaline-backup.env}"

as_app() { setpriv --reuid=10001 --regid=10001 --clear-groups "$@"; }

install -d -m 700 -o 10001 -g 10001 "$STAGE" "$STAGE/cache" "$STAGE/cache/osm"
for db in "${DATABASES[@]}"; do
    [ -f "$DATA/$db" ] || continue
    rm -f "$STAGE/$db"
    as_app sqlite3 "$DATA/$db" ".backup '$STAGE/$db'"
    check=$(as_app sqlite3 "$STAGE/$db" "PRAGMA integrity_check")
    [ "$check" = ok ] || { echo "integrity_check of the copy of $db: $check" >&2; exit 1; }
done
# The cache's layout marker: without it a restored cache/ (nominatim.sqlite) would be refused
if [ -f "$DATA/cache/LAYOUT" ]; then
    install -m 644 -o 10001 -g 10001 "$DATA/cache/LAYOUT" "$STAGE/cache/LAYOUT"
fi

restic backup --tag findaline --exclude "$DATA/runs/_viewer" "$STAGE" "$DATA/runs"
restic forget --tag findaline --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
restic check
