# syntax=docker/dockerfile:1.7

# SeaweedFS 4.41 was originally published with a Go toolchain affected by
# CVE-2026-33818. Build the exact upstream release archive with Go 1.25.14,
# which contains the upstream fix, and keep the runtime shell- and package-free.
FROM golang:1.25.14-alpine3.24@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS builder

ARG SEAWEEDFS_VERSION=4.41
ARG SEAWEEDFS_SOURCE_SHA256=6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd
ENV GOTOOLCHAIN=local

WORKDIR /src
ADD --checksum=sha256:6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd \
    https://github.com/seaweedfs/seaweedfs/archive/refs/tags/${SEAWEEDFS_VERSION}.tar.gz \
    /tmp/seaweedfs.tar.gz
RUN tar --extract --gzip --file=/tmp/seaweedfs.tar.gz --strip-components=1 \
    && test "$(sha256sum /tmp/seaweedfs.tar.gz | cut -d ' ' -f 1)" = "${SEAWEEDFS_SOURCE_SHA256}" \
    && CGO_ENABLED=0 go build \
      -buildvcs=false \
      -mod=readonly \
      -trimpath \
      -ldflags="-s -w -X github.com/seaweedfs/seaweedfs/weed/util.COMMIT=release-${SEAWEEDFS_VERSION}" \
      -o /out/weed \
      ./weed

COPY infra/seaweedfs/healthcheck.go /tmp/healthcheck.go
RUN CGO_ENABLED=0 go build \
      -buildvcs=false \
      -mod=readonly \
      -trimpath \
      -ldflags="-s -w" \
      -o /out/healthcheck \
      /tmp/healthcheck.go \
    && mkdir --parents /out/data \
    && chown 1000:1000 /out/data

FROM scratch AS runtime

ARG SEAWEEDFS_VERSION=4.41
ARG SEAWEEDFS_SOURCE_SHA256=6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd
LABEL org.opencontainers.image.title="Custombuild SeaweedFS" \
      org.opencontainers.image.description="Minimal source-verified SeaweedFS S3 runtime" \
      org.opencontainers.image.version="${SEAWEEDFS_VERSION}" \
      org.opencontainers.image.source="https://github.com/seaweedfs/seaweedfs" \
      org.opencontainers.image.revision="${SEAWEEDFS_VERSION}" \
      io.custombuild.upstream-source.sha256="${SEAWEEDFS_SOURCE_SHA256}" \
      io.custombuild.go.version="1.25.14"

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder --chown=1000:1000 /out/data /data
COPY --from=builder /out/weed /usr/local/bin/weed
COPY --from=builder /out/healthcheck /usr/local/bin/healthcheck

USER 1000:1000
VOLUME ["/data"]
EXPOSE 8333 8888 9333
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
  CMD ["/usr/local/bin/healthcheck"]
ENTRYPOINT ["/usr/local/bin/weed"]
