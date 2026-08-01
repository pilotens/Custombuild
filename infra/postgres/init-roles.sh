#!/bin/sh
set -eu

# Role names are part of migration 0001's explicit grants and are therefore
# production-affecting identifiers, not free-form environment input. Passwords
# are passed as psql literal variables so quotes and other characters cannot
# become SQL syntax.
assert_production_secret() {
  secret_name="$1"
  secret_value="$2"
  normalised_value=$(printf '%s' "$secret_value" | tr '[:upper:]' '[:lower:]')
  compact_value=$(printf '%s' "$normalised_value" | tr -d '[:space:]')
  case "$normalised_value" in
    *change-me*|*development*|custombuild|minioadmin|password|postgres)
      printf '%s\n' "Refusing insecure production secret: $secret_name" >&2
      exit 64
      ;;
  esac
  if [ -z "$compact_value" ]; then
    printf '%s\n' "Refusing empty production secret: $secret_name" >&2
    exit 64
  fi
}

if [ "${APP_ENV:-development}" = "production" ]; then
  assert_production_secret POSTGRES_PASSWORD "${POSTGRES_PASSWORD:-}"
  assert_production_secret API_DATABASE_PASSWORD "${API_DATABASE_PASSWORD:-}"
  assert_production_secret WORKER_DATABASE_PASSWORD "${WORKER_DATABASE_PASSWORD:-}"
fi

psql \
  --set ON_ERROR_STOP=on \
  --set database_name="$POSTGRES_DB" \
  --set migrator_name="$POSTGRES_USER" \
  --set api_password="$API_DATABASE_PASSWORD" \
  --set worker_password="$WORKER_DATABASE_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'SQL'
SELECT format(
  'CREATE ROLE custombuild_api LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
  :'api_password'
)
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'custombuild_api');
\gexec

SELECT format(
  'CREATE ROLE custombuild_worker LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS',
  :'worker_password'
)
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'custombuild_worker');
\gexec

GRANT CONNECT ON DATABASE :"database_name" TO custombuild_api, custombuild_worker;
GRANT USAGE, CREATE ON SCHEMA public TO :"migrator_name";
SQL
