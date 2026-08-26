#!/bin/sh
set -eu

test "${POSTGRES_USER:-}" = "custombuild_bootstrap"
test "${MIGRATOR_DATABASE_USER:-}" = "custombuild_migrator"

pg_isready \
  --quiet \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB"

result=$(
  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --set ON_ERROR_STOP=on \
    --command "
      SELECT
        (SELECT rolsuper AND rolcanlogin FROM pg_roles WHERE rolname = 'custombuild_bootstrap')
        AND (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                    AND NOT rolreplication AND NOT rolbypassrls
             FROM pg_roles WHERE rolname = 'custombuild_migrator')
        AND (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                    AND NOT rolreplication AND NOT rolbypassrls
             FROM pg_roles WHERE rolname = 'custombuild_api')
        AND (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                    AND NOT rolreplication AND NOT rolbypassrls
             FROM pg_roles WHERE rolname = 'custombuild_worker')
        AND NOT pg_has_role('custombuild_migrator', 'custombuild_api', 'MEMBER')
        AND NOT pg_has_role('custombuild_migrator', 'custombuild_worker', 'MEMBER')
        AND NOT pg_has_role('custombuild_api', 'custombuild_worker', 'MEMBER')
        AND (SELECT pg_get_userbyid(datdba) = 'custombuild_bootstrap'
             FROM pg_database WHERE datname = current_database());
    "
)

test "$result" = "t"
