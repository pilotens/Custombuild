# syntax=docker/dockerfile:1.7

FROM python:3.13.15-slim-trixie@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4 AS builder

ARG DEBIAN_SNAPSHOT=20260825T000000Z
ARG DEPENDENCY_LOCK_SHA256=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN if [ "$DEPENDENCY_LOCK_SHA256" != "unknown" ]; then \
      test "$(sha256sum uv.lock | cut -d ' ' -f 1)" = "$DEPENDENCY_LOCK_SHA256"; \
    fi \
    && pip install --no-cache-dir --upgrade pip==26.2.1 uv==0.12.0 \
    && uv sync --locked --no-dev --no-install-project \
    && uv cache clean

COPY packages /app/packages
COPY cad /app/cad
COPY cam /app/cam
COPY postprocessors /app/postprocessors
COPY services/api /app/services/api
COPY scripts /app/scripts

RUN python -m compileall -f -q packages cad cam postprocessors services/api scripts \
    && rm -rf /usr/local/lib/python3.13/ensurepip \
      /usr/local/lib/python3.13/site-packages/pip \
      /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.13/site-packages/uv \
      /usr/local/lib/python3.13/site-packages/uv-*.dist-info \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
      /usr/local/bin/uv /usr/local/bin/uvx


FROM cgr.dev/chainguard/glibc-dynamic:latest@sha256:205572d5e48117e14b44b42627890fa8d3e8e65bb37a80abb3317e5151e7f35b AS runtime-base


FROM cgr.dev/chainguard/python:latest-dev@sha256:f6d6485f11a65ca81d8a2d01eae564fa88937e7d19c1cf216cdb1142980c51bd AS runtime-assembler

USER root
COPY --from=runtime-base / /base-chroot
RUN apk add --no-cache --no-scripts --root /base-chroot \
      py3-pip-wheel=26.2.1-r0 \
      libbz2-1=1.0.8-r23 \
      libcrypto3=3.6.4-r0 \
      libexpat1=2.8.3-r0 \
      libffi=3.8.0-r0 \
      gdbm=1.26-r5 \
      xz=5.8.3-r2 \
      mpdecimal=4.0.1-r3 \
      ncurses-terminfo-base=6.6.20260822-r0 \
      ncurses=6.6.20260822-r0 \
      readline=8.3-r2 \
      sqlite-libs=3.53.4-r0 \
      libssl3=3.6.4-r0 \
      libuuid=2.42.2-r3 \
      zlib=1.3.2-r4 \
      python-3.13-base=3.13.15-r2 \
      python-3.13=3.13.15-r2 \
    && ldconfig -r /base-chroot \
    && awk 'BEGIN { RS=""; FS="\n" } { name=""; version=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) name=substr($i,3); if ($i ~ /^V:/) version=substr($i,3) } if (name != "") print name "=" version }' \
      /base-chroot/lib/apk/db/installed | sort > /tmp/runtime-package-closure \
    && test "$(wc -l < /tmp/runtime-package-closure)" = "24" \
    && test "$(sha256sum /tmp/runtime-package-closure | cut -d ' ' -f 1)" = "b877ad764a4b02b376bf61c2b62ebfc5044c1b1b70fb4a3a5ffca089be06bffb"

COPY --from=builder /opt/custombuild-venv /base-chroot/opt/custombuild-venv
COPY --from=builder /app/packages /base-chroot/app/packages
COPY --from=builder /app/cad /base-chroot/app/cad
COPY --from=builder /app/cam /base-chroot/app/cam
COPY --from=builder /app/postprocessors /base-chroot/app/postprocessors
COPY --from=builder /app/services/api /base-chroot/app/services/api
COPY --from=builder /app/scripts /base-chroot/app/scripts
COPY --from=builder /app/uv.lock /base-chroot/app/uv.lock

RUN rm -f \
      /base-chroot/opt/custombuild-venv/bin/python \
      /base-chroot/opt/custombuild-venv/bin/python3 \
      /base-chroot/opt/custombuild-venv/bin/python3.13 \
    && ln -s /usr/bin/python3.13 /base-chroot/opt/custombuild-venv/bin/python \
    && ln -s python /base-chroot/opt/custombuild-venv/bin/python3 \
    && ln -s python /base-chroot/opt/custombuild-venv/bin/python3.13 \
    && sed -i 's|^home = .*|home = /usr/bin|' /base-chroot/opt/custombuild-venv/pyvenv.cfg \
    && rm -rf /base-chroot/usr/lib/python3.13/ensurepip \
      /base-chroot/usr/share/python-wheels \
    && PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker \
      PATH=/opt/custombuild-venv/bin:/usr/bin:/usr/sbin:/sbin:/bin \
      chroot /base-chroot /opt/custombuild-venv/bin/python -c \
      "import importlib.util, pathlib, shutil, sys; assert sys.version_info[:3] == (3, 13, 15); import uvicorn; import app.main; assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('uv') is None; assert shutil.which('pip') is None; assert shutil.which('uv') is None; assert shutil.which('FreeCAD') is None; assert not any(path.is_file() or path.is_symlink() for path in pathlib.Path('/usr/local').rglob('*'))" \
    && test ! -e /base-chroot/opt/custombuild-venv/bin/pip \
    && test ! -e /base-chroot/opt/custombuild-venv/bin/uv \
    && test ! -e /base-chroot/usr/bin/FreeCAD


FROM runtime-base AS runtime

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
    PATH=/opt/custombuild-venv/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker

LABEL org.opencontainers.image.title="Custombuild API" \
      org.opencontainers.image.description="Server-authoritative furniture design and review API" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.custombuild.source-manifest.sha256="${SOURCE_MANIFEST_SHA256}" \
      io.custombuild.dependency-lock.sha256="${DEPENDENCY_LOCK_SHA256}" \
      io.custombuild.python.version="3.13.15" \
      org.opencontainers.image.source-integrity="6"

WORKDIR /app

COPY --link --from=runtime-assembler /base-chroot /

ENTRYPOINT []
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
