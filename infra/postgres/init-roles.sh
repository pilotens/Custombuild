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
  if [ "${#secret_value}" -lt 24 ]; then
    printf '%s\n' "Refusing short production secret: $secret_name" >&2
    exit 64
  fi
}

# The official image supplies POSTGRES_USER/POSTGRES_PASSWORD. Keep local
# development ergonomic while making the application migration role distinct
# from the cluster-owning bootstrap role.
MIGRATOR_DATABASE_USER=${MIGRATOR_DATABASE_USER:-custombuild_migrator}
MIGRATOR_DATABASE_PASSWORD=${MIGRATOR_DATABASE_PASSWORD:-change-me-migrator}
API_DATABASE_PASSWORD=${API_DATABASE_PASSWORD:-change-me-api}
WORKER_DATABASE_PASSWORD=${WORKER_DATABASE_PASSWORD:-change-me-worker}
export MIGRATOR_DATABASE_USER MIGRATOR_DATABASE_PASSWORD
export API_DATABASE_PASSWORD WORKER_DATABASE_PASSWORD

if [ "${APP_ENV:-development}" = "production" ]; then
  if [ "${POSTGRES_USER:-}" != "custombuild_bootstrap" ]; then
    printf '%s\n' "POSTGRES_USER must be the fixed bootstrap role" >&2
    exit 64
  fi
  if [ "${MIGRATOR_DATABASE_USER:-}" != "custombuild_migrator" ]; then
    printf '%s\n' "MIGRATOR_DATABASE_USER must be the fixed migrator role" >&2
    exit 64
  fi
  assert_production_secret POSTGRES_PASSWORD "${POSTGRES_PASSWORD:-}"
  assert_production_secret MIGRATOR_DATABASE_PASSWORD "${MIGRATOR_DATABASE_PASSWORD:-}"
  assert_production_secret API_DATABASE_PASSWORD "${API_DATABASE_PASSWORD:-}"
  assert_production_secret WORKER_DATABASE_PASSWORD "${WORKER_DATABASE_PASSWORD:-}"
fi

psql \
  --set ON_ERROR_STOP=on \
  --set database_name="$POSTGRES_DB" \
  --set bootstrap_name="$POSTGRES_USER" \
  --set migrator_name="$MIGRATOR_DATABASE_USER" \
  --set migrator_password="$MIGRATOR_DATABASE_PASSWORD" \
  --set api_password="$API_DATABASE_PASSWORD" \
  --set worker_password="$WORKER_DATABASE_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'SQL'
SELECT format(
  'CREATE ROLE custombuild_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'migrator_password'
)
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'custombuild_migrator');
\gexec

SELECT format(
  'CREATE ROLE custombuild_api LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
  :'api_password'
)
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'custombuild_api');
\gexec

SELECT format(
  'CREATE ROLE custombuild_worker LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'worker_password'
)
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'custombuild_worker');
\gexec

SELECT format(
  'ALTER ROLE custombuild_migrator WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'migrator_password'
);
\gexec
SELECT format(
  'ALTER ROLE custombuild_api WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'api_password'
);
\gexec
SELECT format(
  'ALTER ROLE custombuild_worker WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'worker_password'
);
\gexec

REVOKE custombuild_api, custombuild_worker FROM custombuild_migrator;
REVOKE custombuild_worker FROM custombuild_api;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name"
  TO custombuild_migrator, custombuild_api, custombuild_worker;
GRANT CREATE ON DATABASE :"database_name" TO custombuild_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO custombuild_migrator;
GRANT USAGE ON SCHEMA public TO custombuild_api, custombuild_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public
  REVOKE ALL PRIVILEGES ON TABLES FROM custombuild_api, custombuild_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM custombuild_api, custombuild_worker;
SQL
