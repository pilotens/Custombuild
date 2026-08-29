.PHONY: install test test-web coverage-gates lint typecheck build check openapi contracts golden e2e-list encoding environment-contract external-production-contract release-readiness source-manifest backup-freshness

PYTHONPATHS := packages/domain/src:packages/rule-engine/src:packages/manufacturing/src:packages/template-sdk/src:cad/src:cam/src:postprocessors/src:services/api:services/worker

install:
	uv sync --locked --group dev --group cad
	pnpm install --frozen-lockfile

test:
	uv run coverage erase
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=80

test-web:
	pnpm --dir apps/web test

coverage-gates:
	uv run coverage erase
	uv run pytest tests/unit -q --cov=packages/domain/src/custombuild_domain --cov=cad/src/custombuild_cad --cov-fail-under=90
	uv run coverage erase
	uv run pytest tests/unit -q --cov=packages/rule-engine/src/custombuild_rules --cov-fail-under=90
	uv run coverage erase
	uv run pytest tests/unit -q --cov=packages/manufacturing/src/custombuild_manufacturing --cov=cam/src/custombuild_cam --cov=postprocessors/src/custombuild_postprocessors --cov-fail-under=90

lint:
	uv run ruff check .
	pnpm --dir apps/web lint

typecheck:
	uv run mypy packages services
	uv run mypy scripts/storage_capacity_development.py
	uv run mypy scripts/storage_capacity_preflight.py
	uv run mypy scripts/storage_capacity_refresh.py
	uv run mypy scripts/storage_recovery.py
	pnpm --dir apps/web typecheck

build:
	pnpm --dir apps/web build

openapi:
	PYTHONPATH=$(PYTHONPATHS) uv run python scripts/export_openapi.py

contracts: openapi
	pnpm --dir apps/web generate:api
	git diff --exit-code -- packages/contracts/openapi.json apps/web/lib/api-schema.d.ts

golden:
	PYTHONPATH=$(PYTHONPATHS) uv run python scripts/regenerate_golden.py --check

e2e-list:
	pnpm --dir apps/web exec playwright test --list

encoding:
	uv run python scripts/check_text_encoding.py .

environment-contract:
	uv run python scripts/check_environment_isolation.py compose.yml

external-production-contract:
	uv run python scripts/check_external_production.py --repo .

release-readiness:
	uv run python scripts/release_readiness.py

source-manifest:
	uv run python scripts/source_manifest.py --repo .

BACKUP_ROOT ?= test-results/backups

backup-freshness:
	uv run python scripts/backup_freshness.py --root $(BACKUP_ROOT) --max-age-hours 24 --verify-payloads

check: lint typecheck test test-web coverage-gates golden contracts build e2e-list encoding environment-contract source-manifest
