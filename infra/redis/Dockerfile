# syntax=docker/dockerfile:1.7
FROM redis:7.2.17-alpine@sha256:3d2b46efa3280299fe6e117a33fccdfeb495c39098f95d076fadd12a88005b0a AS upstream
FROM alpine:3.24.4@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a AS packages
ADD --checksum=sha256:7b05bc778b8b25cd880835b40114182bd4d1581717504d0129c810c0a14291cb https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/zlib-1.4-r0.apk /packages/zlib.apk
ADD --checksum=sha256:6a6290ca1f0fb61d0a47e15ed921dd44cb54cf15981c26923dcdf79095c7aff8 https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/setpriv-2.42.3-r1.apk /packages/setpriv.apk
ADD --checksum=sha256:feeb65b050ec75766057ce8f3e9611eef3c03d9eeed17d31a900ab900ed2bcf2 https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libcap-ng-0.8.5-r2.apk /packages/libcap-ng.apk
ADD --checksum=sha256:2a791c7957961f830cf31b04f35f814a63afcf633badc9052425ff1cf2f64380 https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libeconf-0.8.3-r0.apk /packages/libeconf.apk

FROM alpine:3.24.4@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a
RUN --mount=type=bind,from=packages,source=/packages,target=/packages \
    apk --no-network add /packages/*.apk \
    && test "$(apk list --installed zlib | cut -d' ' -f1)" = "zlib-1.4-r0" \
    && ln -s /bin/setpriv /usr/bin/setpriv \
    && addgroup -g 1000 redis && adduser -D -H -u 999 -G redis redis \
    && mkdir /data && chown redis:redis /data
COPY --from=upstream /usr/local/bin/ /usr/local/bin/
RUN redis-server --version | grep -F 'v=7.2.17 ' \
    && test "$(redis-cli --version)" = 'redis-cli 7.2.17' \
    && setpriv --reuid redis --regid redis --clear-groups sh -ec 'test -w /data'
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
WORKDIR /data
VOLUME /data
EXPOSE 6379
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["redis-server"]
