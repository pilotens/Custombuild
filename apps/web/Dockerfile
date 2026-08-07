# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=24.14.0
ARG PNPM_VERSION=11.7.0

FROM node:${NODE_VERSION}-bookworm-slim AS base

ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:${PATH} \
    NEXT_TELEMETRY_DISABLED=1

RUN corepack enable && corepack prepare "pnpm@${PNPM_VERSION}" --activate
WORKDIR /workspace


FROM base AS dependencies

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json

RUN --mount=type=cache,id=custombuild-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && \
    pnpm install --frozen-lockfile --filter @custombuild/web...


FROM dependencies AS builder

ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_DEMO_TOKEN=""
ENV NODE_ENV=production \
    NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_DEMO_TOKEN=${NEXT_PUBLIC_DEMO_TOKEN}

COPY apps/web apps/web
RUN mkdir -p apps/web/public && pnpm --dir apps/web build


FROM node:${NODE_VERSION}-bookworm-slim AS runner

ARG APP_VERSION=0.1.0
LABEL org.opencontainers.image.title="Custombuild Web" \
      org.opencontainers.image.description="Swedish parametric furniture production workspace" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

RUN apt-get update && \
    apt-get install --yes --no-install-recommends dumb-init && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 1001 nodejs && \
    useradd --system --uid 1001 --gid nodejs --home-dir /app nextjs

WORKDIR /app

COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web/public ./apps/web/public

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:' + process.env.PORT + '/').then(r => { if (!r.ok) process.exit(1) }).catch(() => process.exit(1))"]

ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "apps/web/server.js"]
