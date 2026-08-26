FROM python:3.13.15-slim-trixie@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4 AS runtime

ARG DEBIAN_SNAPSHOT=20260825T000000Z
ARG APP_VERSION=0.1.0-local
ARG VCS_REF=uncommitted
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
ARG SOURCE_MANIFEST_SHA256=unknown
ARG DEPENDENCY_LOCK_SHA256=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} \
    VCS_REF=${VCS_REF} \
    BUILD_DATE=${BUILD_DATE} \
    SOURCE_URL=${SOURCE_URL} \
    SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} \
    DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256} \
    UV_PROJECT_ENVIRONMENT=/opt/custombuild-venv \
    PATH=/opt/custombuild-venv/bin:$PATH \
    PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker

RUN printf '%s\n' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      'Suites: trixie trixie-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      'Suites: trixie-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 custombuild
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN if [ "$DEPENDENCY_LOCK_SHA256" != "unknown" ]; then \
      test "$(sha256sum uv.lock | cut -d ' ' -f 1)" = "$DEPENDENCY_LOCK_SHA256"; \
    fi \
    && pip install --no-cache-dir --upgrade pip==26.2.1 uv==0.12.0 \
    && uv sync --locked --no-dev --no-install-project \
    && uv cache clean

LABEL org.opencontainers.image.title="Custombuild API" \
      org.opencontainers.image.description="Server-authoritative furniture design and review API" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.custombuild.source-manifest.sha256="${SOURCE_MANIFEST_SHA256}" \
      io.custombuild.dependency-lock.sha256="${DEPENDENCY_LOCK_SHA256}" \
      org.opencontainers.image.source-integrity="5"
COPY packages /app/packages
COPY cad /app/cad
COPY cam /app/cam
COPY postprocessors /app/postprocessors
COPY services/api /app/services/api
COPY scripts /app/scripts
RUN python -m compileall -f -q packages cad cam postprocessors services/api scripts

USER custombuild
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
