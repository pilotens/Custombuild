# syntax=docker/dockerfile:1.7
FROM alpine:3.24.4@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a AS packages
ADD --checksum=sha256:7b05bc778b8b25cd880835b40114182bd4d1581717504d0129c810c0a14291cb https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/zlib-1.4-r0.apk /packages/zlib.apk
ADD --checksum=sha256:16469f2246465aae50489adabd6c5f1fbd5e0f02f75b1ca0be9feaefd7a8dd3e https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/gosu-1.19-r6.apk /packages/gosu.apk

FROM postgres:18-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395 AS prepared
RUN --mount=type=bind,from=packages,source=/packages,target=/packages \
    apk --no-network add /packages/*.apk \
    && test "$(apk list --installed zlib | cut -d' ' -f1)" = "zlib-1.4-r0" \
    && test "$(apk list --installed gosu | cut -d' ' -f1)" = "gosu-1.19-r6" \
    && rm /usr/local/bin/gosu && ln -s /usr/bin/gosu /usr/local/bin/gosu \
    && gosu postgres sh -ec 'test "$(id -un)" = postgres' \
    && test "$(postgres --version)" = "postgres (PostgreSQL) 18.6"
COPY --chmod=755 infra/postgres/entrypoint.sh /usr/local/bin/custombuild-postgres-entrypoint
COPY --chmod=755 infra/postgres/record-layout.sh /docker-entrypoint-initdb.d/99-record-layout.sh
# Copy the resolved filesystem so the superseded gosu executable is absent
# from the distributed layers as well as the running server.
FROM scratch
COPY --from=prepared / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=en_US.utf8 \
    PG_MAJOR=18 PG_VERSION=18.6 PGDATA=/var/lib/postgresql/18/docker
VOLUME /var/lib/postgresql
EXPOSE 5432
STOPSIGNAL SIGINT
ARG APP_VERSION=0.1.0-local
ARG VCS_REF=uncommitted
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
ARG SOURCE_MANIFEST_SHA256=unknown
ARG DEPENDENCY_LOCK_SHA256=unknown
ENV APP_VERSION=${APP_VERSION} VCS_REF=${VCS_REF} BUILD_DATE=${BUILD_DATE} SOURCE_URL=${SOURCE_URL} \
    SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256}
LABEL org.opencontainers.image.version=${APP_VERSION} org.opencontainers.image.revision=${VCS_REF} \
      org.opencontainers.image.created=${BUILD_DATE} org.opencontainers.image.source=${SOURCE_URL} \
      io.custombuild.source-manifest.sha256=${SOURCE_MANIFEST_SHA256} \
      io.custombuild.dependency-lock.sha256=${DEPENDENCY_LOCK_SHA256}
ENTRYPOINT ["custombuild-postgres-entrypoint"]
CMD ["postgres"]
