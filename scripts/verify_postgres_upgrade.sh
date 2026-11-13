#!/usr/bin/env bash
# Prove the one-time logical migration and the refusal of the old data layout.
set -euo pipefail
image=${1:?Pass the locally built PostgreSQL image}
legacy='cgr.dev/chainguard/postgres:latest@sha256:f131db66371391e404c80cef7dd40f31b1b54eb264a6cb032925d9a3859f93f4'
old_volume=$(docker volume create)
new_volume=$(docker volume create)
name="pg-transition-${old_volume:0:16}"
tmp=$(mktemp -d)
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$old_volume" "$new_volume" >/dev/null
    rm -rf "$tmp"
}
trap cleanup EXIT
db() {
    docker exec -i -e PGPASSWORD=fixture-bootstrap "$name" psql -X -tAq \
        -v ON_ERROR_STOP=1 -h 127.0.0.1 -U custombuild_bootstrap -d custombuild "$@"
}
start() {
    local runtime=$1 volume=$2 destination=$3 initdir=$4
    docker run -d --name "$name" --network none \
        --mount "type=volume,src=$volume,dst=$destination,volume-nocopy" \
        --mount "type=bind,src=$PWD/infra/postgres/init-roles.sh,dst=$initdir/10-init-roles.sh,readonly" \
        -e APP_ENV=test -e POSTGRES_USER=custombuild_bootstrap \
        -e POSTGRES_DB=custombuild -e POSTGRES_PASSWORD=fixture-bootstrap \
        -e MIGRATOR_DATABASE_USER=custombuild_migrator -e MIGRATOR_DATABASE_PASSWORD=fixture-migrator \
        -e API_DATABASE_PASSWORD=fixture-api -e WORKER_DATABASE_PASSWORD=fixture-worker \
        -e CAPACITY_ATTESTOR_DATABASE_USER=custombuild_storage_attestor \
        -e CAPACITY_ATTESTOR_DATABASE_PASSWORD=fixture-attestor "$runtime" >/dev/null
    for attempt in {1..90}; do
        if [[ $(db -c "SELECT count(*) FROM pg_roles WHERE rolname = 'custombuild_api'" 2>/dev/null) == 1 ]]; then return; fi
        sleep 1
    done
    docker logs "$name" >&2
    return 1
}
stop() { docker stop "$name" >/dev/null && docker rm "$name" >/dev/null; }
probe() {
    db <<'SQL'
SET ROLE custombuild_api;
SET custombuild.probe_tenant = 'one';
SELECT revision || ':' || (design->>'material') FROM migration_probe.jobs ORDER BY revision;
SELECT relforcerowsecurity FROM pg_class WHERE oid = 'migration_probe.jobs'::regclass;
SQL
}
start "$legacy" "$old_volume" /var/lib/postgresql/data /var/lib/postgres/initdb
db <<'SQL'
CREATE SCHEMA migration_probe AUTHORIZATION custombuild_migrator;
SET ROLE custombuild_migrator;
CREATE TABLE migration_probe.jobs(tenant text, revision int, design jsonb, PRIMARY KEY(tenant, revision));
INSERT INTO migration_probe.jobs VALUES ('one',1,'{"material":"valnöt"}'),('one',2,'{"material":"björk"}'),('two',1,'{"material":"får inte visas"}');
ALTER TABLE migration_probe.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE migration_probe.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_only ON migration_probe.jobs USING (tenant = current_setting('custombuild.probe_tenant'));
GRANT USAGE ON SCHEMA migration_probe TO custombuild_api;
GRANT SELECT ON migration_probe.jobs TO custombuild_api;
SQL
probe > "$tmp/before"
printf '1:valnöt\n2:björk\nt\n' | cmp - "$tmp/before"
docker exec -e PGPASSWORD=fixture-bootstrap "$name" pg_dump -h 127.0.0.1 \
    -U custombuild_bootstrap -d custombuild -Fc --schema=migration_probe > "$tmp/probe.dump"
stop
set +e
docker run --rm --network none --mount "type=volume,src=$old_volume,dst=/var/lib/postgresql,readonly" \
    "$image" postgres >"$tmp/refusal" 2>&1
refused=$?
set -e
test "$refused" -eq 78
grep -Fq 'refused this PostgreSQL data volume' "$tmp/refusal"

start "$image" "$new_volume" /var/lib/postgresql /docker-entrypoint-initdb.d
docker exec -i -e PGPASSWORD=fixture-migrator "$name" pg_restore --exit-on-error --no-owner \
    -h 127.0.0.1 -U custombuild_migrator -d custombuild < "$tmp/probe.dump"
probe | cmp - "$tmp/before"
stop
start "$image" "$new_volume" /var/lib/postgresql /docker-entrypoint-initdb.d
probe | cmp - "$tmp/before"
stop
start "$legacy" "$old_volume" /var/lib/postgresql/data /var/lib/postgres/initdb
probe | cmp - "$tmp/before"
