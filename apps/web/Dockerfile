# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=24.22.2
ARG PNPM_VERSION=11.7.0

FROM node:24.22.2-trixie-slim@sha256:0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d AS base

ARG NODE_VERSION
ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:${PATH} \
    NEXT_TELEMETRY_DISABLED=1

RUN test "${NODE_VERSION}" = "24.22.2" \
    && corepack enable \
    && corepack prepare "pnpm@${PNPM_VERSION}" --activate
WORKDIR /workspace


FROM base AS dependencies

ARG FRONTEND_LOCK_SHA256=unknown
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json

RUN if [ "$FRONTEND_LOCK_SHA256" != "unknown" ]; then \
      test "$(sha256sum pnpm-lock.yaml | cut -d ' ' -f 1)" = "$FRONTEND_LOCK_SHA256"; \
    fi
RUN --mount=type=cache,id=custombuild-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && \
    pnpm install --frozen-lockfile --filter @custombuild/web...


FROM dependencies AS builder

ENV NODE_ENV=production

COPY apps/web apps/web
COPY packages/contracts packages/contracts
COPY packages/scene-geometry packages/scene-geometry
COPY examples/furniture/walnut-shelving-4340x2540x280.json examples/furniture/
COPY examples/furniture/bookcase-4340x2540x280.json examples/furniture/
RUN mkdir -p apps/web/public && pnpm --dir apps/web build


FROM cgr.dev/chainguard/glibc-dynamic:latest@sha256:aab5cd0b4cc5edd9c87b6e7aa9ddaa3fb6ffcec559b9e5473c55bbc937b65399 AS runtime-base


FROM cgr.dev/chainguard/python:latest-dev@sha256:64f414bffa25ca9ff518f35975ad153a921b2e4850c4a84c31a96885fdb86a02 AS runtime-assembler

USER root
COPY --from=runtime-base / /base-chroot
RUN apk add --no-cache --no-scripts --root /base-chroot \
      c-ares=1.34.9-r0 \
      ca-certificates-bundle=20261020-r0 \
      glibc-2.44-locale-posix=2.44-r14 \
      glibc-2.44=2.44-r14 \
      icu79-data-full=79.1-r1 \
      ld-linux-2.44=2.44-r14 \
      libbrotlicommon1=1.2.1-r3 \
      libbrotlidec1=1.2.1-r3 \
      libbrotlienc1=1.2.1-r3 \
      libgcc=16.2.0-r2 \
      libicu79=79.1-r1 \
      libnghttp2-14=1.71.0-r0 \
      libstdc++=16.2.0-r2 \
      libuv=1.53.0-r0 \
      nodejs-24=24.22.2-r2 \
      openssl-4.0-libcrypto=4.0.3-r8 \
      openssl-4.0-libssl=4.0.3-r8 \
      openssl-provider-legacy-allowed=4.1.0-r0 \
      wolfi-baselayout=20230201-r31 \
      zlib=1.3.2.1_rc20260921-r2 \
    && ldconfig -r /base-chroot \
    && awk 'BEGIN { RS=""; FS="\n" } { name=""; version=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) name=substr($i,3); if ($i ~ /^V:/) version=substr($i,3) } if (name != "") print name "=" version }' \
      /base-chroot/lib/apk/db/installed | sort > /tmp/runtime-package-closure \
    && test "$(wc -l < /tmp/runtime-package-closure)" = "20" \
    && test "$(sha256sum /tmp/runtime-package-closure | cut -d ' ' -f 1)" = "c63f4ad041b38a0f30df9f4a18e8dfac4ad2411840ff114d32dc57ce2d8ce4ec" \
    && PATH=/usr/bin:/usr/sbin:/sbin:/bin \
      chroot /base-chroot /usr/bin/node -e \
      "const fs = require('node:fs'); require('node:inspector'); if (process.version !== 'v24.22.2') process.exit(1); for (const path of ['/usr/local/bin/node', '/usr/bin/npm', '/usr/bin/pnpm', '/usr/bin/corepack']) if (fs.existsSync(path)) process.exit(1)"


FROM runtime-base AS runner

ARG APP_VERSION=0.1.0-local
ARG VCS_REF=uncommitted
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
ARG SOURCE_MANIFEST_SHA256=unknown
ARG FRONTEND_LOCK_SHA256=unknown
LABEL org.opencontainers.image.title="Custombuild Web" \
      org.opencontainers.image.description="Swedish parametric furniture production workspace" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.custombuild.source-manifest.sha256="${SOURCE_MANIFEST_SHA256}" \
      io.custombuild.frontend-lock.sha256="${FRONTEND_LOCK_SHA256}" \
      org.opencontainers.image.source-integrity="5"

ENV NODE_ENV=production \
    APP_ENV=production \
    APP_VERSION=${APP_VERSION} \
    VCS_REF=${VCS_REF} \
    BUILD_DATE=${BUILD_DATE} \
    SOURCE_URL=${SOURCE_URL} \
    SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} \
    FRONTEND_LOCK_SHA256=${FRONTEND_LOCK_SHA256} \
    CUSTOMBUILD_WEB_API_URL="" \
    CUSTOMBUILD_WEB_DEMO_TOKEN="" \
    CUSTOMBUILD_WEB_OIDC_ISSUER="" \
    CUSTOMBUILD_WEB_OIDC_CLIENT_ID="" \
    CUSTOMBUILD_WEB_OIDC_REDIRECT_URI="" \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

COPY --link --from=runtime-assembler /base-chroot /
COPY --from=builder --chown=65532:65532 /workspace/apps/web/.next/standalone ./
COPY --from=builder --chown=65532:65532 /workspace/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=65532:65532 /workspace/apps/web/public ./apps/web/public

USER 65532:65532
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/usr/bin/node", "-e", "fetch('http://127.0.0.1:' + process.env.PORT + '/').then(r => { if (!r.ok) process.exit(1) }).catch(() => process.exit(1))"]

ENTRYPOINT ["/usr/bin/node"]
CMD ["apps/web/server.js"]
