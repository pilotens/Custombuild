#!/usr/bin/env bash
# Disposable database for the host-side migration and RLS suites.
set -euo pipefail
tag="custombuild-postgres:ci-${GITHUB_SHA:?}"
docker build --file infra/postgres/Dockerfile --tag "$tag" .
container=$(docker run -d --rm -p 127.0.0.1:5432:5432 \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=custombuild "$tag")
for attempt in {1..90}; do
    if docker exec -e PGPASSWORD=postgres "$container" \
        psql -h 127.0.0.1 -U postgres -d custombuild -Atqc 'SELECT 1' 2>/dev/null | grep -qx 1; then
        exit 0
    fi
    sleep 1
done
docker logs "$container" >&2
exit 1
