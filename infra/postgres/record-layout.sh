#!/bin/sh
# The upstream initializer invokes this only after Custombuild's role bootstrap.
set -eu
test "$PGDATA" = /var/lib/postgresql/18/docker
test "$(cat "$PGDATA/PG_VERSION")" = 18
umask 077
printf '%s\n' postgres18-alpine-v1 > "$PGDATA/.custombuild-postgres-layout.tmp"
mv "$PGDATA/.custombuild-postgres-layout.tmp" "$PGDATA/.custombuild-postgres-layout"
