# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=24.19.0
ARG PNPM_VERSION=11.7.0

FROM node:24.19.0-trixie-slim@sha256:ab3eebe934147fee049b5eb83c570f68c849a13c930bdfa482de99fcdfa3b3de AS base

ARG NODE_VERSION
ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:${PATH} \
    NEXT_TELEMETRY_DISABLED=1

RUN test "${NODE_VERSION}" = "24.19.0" \
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

ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_DEMO_TOKEN=""
ARG NEXT_PUBLIC_OIDC_ISSUER=""
ARG NEXT_PUBLIC_OIDC_CLIENT_ID=""
ARG NEXT_PUBLIC_OIDC_REDIRECT_URI=""
ENV NODE_ENV=production \
    NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_DEMO_TOKEN=${NEXT_PUBLIC_DEMO_TOKEN} \
    NEXT_PUBLIC_OIDC_ISSUER=${NEXT_PUBLIC_OIDC_ISSUER} \
    NEXT_PUBLIC_OIDC_CLIENT_ID=${NEXT_PUBLIC_OIDC_CLIENT_ID} \
    NEXT_PUBLIC_OIDC_REDIRECT_URI=${NEXT_PUBLIC_OIDC_REDIRECT_URI}

COPY apps/web apps/web
COPY packages/contracts packages/contracts
RUN mkdir -p apps/web/public && pnpm --dir apps/web build


FROM cgr.dev/chainguard/glibc-dynamic:latest@sha256:205572d5e48117e14b44b42627890fa8d3e8e65bb37a80abb3317e5151e7f35b AS runtime-base


FROM cgr.dev/chainguard/python:latest-dev@sha256:f6d6485f11a65ca81d8a2d01eae564fa88937e7d19c1cf216cdb1142980c51bd AS runtime-assembler

USER root
COPY --from=runtime-base / /base-chroot
RUN apk add --no-cache --no-scripts --root /base-chroot \
      c-ares=1.34.8-r1 \
      libbrotlicommon1=1.2.0-r3 \
      libbrotlidec1=1.2.0-r3 \
      libbrotlienc1=1.2.0-r3 \
      libcrypto3=3.6.4-r0 \
      icu78-data-full=78.3-r2 \
      libicu78=78.3-r2 \
      libnghttp2-14=1.70.0-r2 \
      libssl3=3.6.4-r0 \
      libuv=1.52.1-r1 \
      zlib=1.3.2-r4 \
      nodejs-24-minimal=24.19.0-r0 \
    && ldconfig -r /base-chroot \
    && awk 'BEGIN { RS=""; FS="\n" } { name=""; version=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) name=substr($i,3); if ($i ~ /^V:/) version=substr($i,3) } if (name != "") print name "=" version }' \
      /base-chroot/lib/apk/db/installed | sort > /tmp/runtime-package-closure \
    && test "$(wc -l < /tmp/runtime-package-closure)" = "19" \
    && test "$(sha256sum /tmp/runtime-package-closure | cut -d ' ' -f 1)" = "c1dd3f34a4e609705a82320975cb9bbac035fd9969f592d52fe58158689d4b4c" \
    && PATH=/usr/bin:/usr/sbin:/sbin:/bin \
      chroot /base-chroot /usr/bin/node -e \
      "const fs = require('node:fs'); if (process.version !== 'v24.19.0') process.exit(1); for (const path of ['/usr/local/bin/node', '/usr/bin/npm', '/usr/bin/pnpm', '/usr/bin/corepack']) if (fs.existsSync(path)) process.exit(1)"


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
    APP_VERSION=${APP_VERSION} \
    VCS_REF=${VCS_REF} \
    BUILD_DATE=${BUILD_DATE} \
    SOURCE_URL=${SOURCE_URL} \
    SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} \
    FRONTEND_LOCK_SHA256=${FRONTEND_LOCK_SHA256} \
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
