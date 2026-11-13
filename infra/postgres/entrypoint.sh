#!/bin/sh
set -eu
case "${1:-}" in postgres|-*) ;; *) exec docker-entrypoint.sh "$@" ;; esac
root=/var/lib/postgresql
layout="$root/18/docker"
reject() {
    echo 'Custombuild refused this PostgreSQL data volume before starting the server. Preserve it. Logically restore the coordinated backup to an empty PostgreSQL 18 Alpine volume; see docs/OPERATIONS.md.' >&2
    exit 78
}
[ "${PGDATA:-}" = "$layout" ] || reject
[ ! -L "$root/18" ] && [ ! -L "$layout" ] || reject
# Accept only our versioned layout, including a new empty image-provisioned
# directory. Never initialize a new cluster beside data from an older image.
foreign=$(find "$root" -mindepth 1 -maxdepth 1 ! -name 18 -print -quit) || reject
[ -z "$foreign" ] || reject
if [ -d "$root/18" ]; then
    foreign=$(find "$root/18" -mindepth 1 -maxdepth 1 ! -name docker -print -quit) || reject
    [ -z "$foreign" ] || reject
fi
contents=
if [ -d "$layout" ]; then
    contents=$(find "$layout" -mindepth 1 -print -quit) || reject
fi
if [ -n "$contents" ]; then
    [ -f "$layout/PG_VERSION" ] && [ "$(cat "$layout/PG_VERSION")" = 18 ] || reject
    marker="$layout/.custombuild-postgres-layout"
    [ -f "$marker" ] && [ ! -L "$marker" ] && [ "$(cat "$marker")" = 'postgres18-alpine-v1' ] || reject
fi
exec docker-entrypoint.sh "$@"
