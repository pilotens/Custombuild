# syntax=docker/dockerfile:1.7

FROM cgr.dev/chainguard/glibc-dynamic:latest@sha256:205572d5e48117e14b44b42627890fa8d3e8e65bb37a80abb3317e5151e7f35b AS runtime-base


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
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN if [ "$DEPENDENCY_LOCK_SHA256" != "unknown" ]; then \
      test "$(sha256sum uv.lock | cut -d ' ' -f 1)" = "$DEPENDENCY_LOCK_SHA256"; \
    fi \
    && pip install --no-cache-dir --upgrade pip==26.2.1 uv==0.12.0 \
    && uv sync --locked --no-install-project --group cad --no-dev \
    && uv pip check \
    && python -c "import cadquery; import OCP; import vtkmodules.vtkCommonCore" \
    && uv cache clean

COPY packages /app/packages
COPY cad /app/cad
COPY cam /app/cam
COPY postprocessors /app/postprocessors
COPY services /app/services

RUN python -m compileall -f -q packages cad cam postprocessors services \
    && rm -rf /usr/local/lib/python3.13/ensurepip \
      /usr/local/lib/python3.13/site-packages/pip \
      /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.13/site-packages/uv \
      /usr/local/lib/python3.13/site-packages/uv-*.dist-info \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
      /usr/local/bin/uv /usr/local/bin/uvx


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
      mesa-gl=26.2.1-r0 \
      libx11=1.8.13-r5 \
      libxau=1.0.12-r7 \
      libxdmcp=1.1.5-r9 \
      libbsd=0.12.2-r7 \
      libmd=1.2.0-r2 \
      libgomp=16.2.0-r1 \
      libxcb=1.17.0-r15 \
      libglvnd=1.7.0-r10 \
      libxml2=2.15.0-r0 \
      libLLVM-22=22.1.8-r2 \
      libpciaccess=0.19-r2 \
      libdrm=2.4.134-r0 \
      libzstd1=1.5.7-r8 \
      libelf=0.195-r2 \
      libxshmfence=1.3.3-r2 \
      mesa-libgallium=26.2.1-r0 \
      mesa-gbm=26.2.1-r0 \
      libudev=261.2-r1 \
      wayland-libs-client=1.26.0-r0 \
      wayland-protocols=1.49-r0 \
      mesa=26.2.1-r0 \
      libxext=1.3.7-r0 \
      libxxf86vm=1.1.7-r2 \
      mesa-glx=26.2.1-r0 \
    && ldconfig -r /base-chroot \
    && awk 'BEGIN { RS=""; FS="\n" } { name=""; version=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) name=substr($i,3); if ($i ~ /^V:/) version=substr($i,3) } if (name != "") print name "=" version }' \
      /base-chroot/lib/apk/db/installed | sort > /tmp/runtime-package-closure \
    && test "$(wc -l < /tmp/runtime-package-closure)" = "49" \
    && test "$(sha256sum /tmp/runtime-package-closure | cut -d ' ' -f 1)" = "7d48b979148c77fbd4383671b6f0a79156fd85518840f217ef564091230f596e"

COPY --from=builder /opt/custombuild-venv /base-chroot/opt/custombuild-venv
COPY --from=builder /app/packages /base-chroot/app/packages
COPY --from=builder /app/cad /base-chroot/app/cad
COPY --from=builder /app/cam /base-chroot/app/cam
COPY --from=builder /app/postprocessors /base-chroot/app/postprocessors
COPY --from=builder /app/services /base-chroot/app/services
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
      /base-chroot/usr/local \
    && PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker \
      PATH=/opt/custombuild-venv/bin:/usr/bin:/usr/sbin:/sbin:/bin \
      chroot /base-chroot /opt/custombuild-venv/bin/python -c \
      "import importlib.util, pathlib, shutil, ssl, sys; assert sys.version_info[:3] == (3, 13, 15); import celery, cadquery, OCP, vtkmodules.vtkCommonCore; import custombuild_worker.tasks; solid = cadquery.Workplane('XY').box(10, 20, 30); assert solid.val().Volume() > 0; assert ssl.create_default_context().get_ca_certs(); assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('uv') is None; assert importlib.util.find_spec('FreeCAD') is None; assert shutil.which('pip') is None; assert shutil.which('uv') is None; assert shutil.which('FreeCAD') is None; assert not pathlib.Path('/usr/local').exists()" \
    && test ! -e /base-chroot/opt/custombuild-venv/bin/pip \
    && test ! -e /base-chroot/opt/custombuild-venv/bin/uv \
    && test ! -e /base-chroot/usr/bin/FreeCAD


FROM scratch AS runtime

ARG APP_VERSION=0.1.0-local
ARG VCS_REF=uncommitted
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
ARG SOURCE_MANIFEST_SHA256=unknown
ARG DEPENDENCY_LOCK_SHA256=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    APP_VERSION=${APP_VERSION} \
    VCS_REF=${VCS_REF} \
    BUILD_DATE=${BUILD_DATE} \
    SOURCE_URL=${SOURCE_URL} \
    SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} \
    DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256} \
    UV_PROJECT_ENVIRONMENT=/opt/custombuild-venv \
    PATH=/opt/custombuild-venv/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker

LABEL org.opencontainers.image.title="Custombuild Manufacturing Worker" \
      org.opencontainers.image.description="Isolated CAD, DFM and validation-only CAM worker" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.custombuild.source-manifest.sha256="${SOURCE_MANIFEST_SHA256}" \
      io.custombuild.dependency-lock.sha256="${DEPENDENCY_LOCK_SHA256}" \
      io.custombuild.python.version="3.13.15" \
      io.custombuild.freecad.runtime="not-installed-fail-closed" \
      org.opencontainers.image.source-integrity="6"

COPY --link --from=runtime-assembler /base-chroot /

WORKDIR /app

ENTRYPOINT []
USER 65532:65532
CMD ["python", "-m", "custombuild_worker.generation_startup", "--loglevel=INFO", "--concurrency=2", "--queues=generation"]
