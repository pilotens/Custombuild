# syntax=docker/dockerfile:1.7

# Build the exact SeaweedFS release archive with a vulnerability-remediated Go
# toolchain and checksum-locked module overrides. Keep the runtime shell- and
# package-free.
FROM golang:1.26.6-alpine3.24@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS builder

ARG SEAWEEDFS_VERSION=4.41
ARG SEAWEEDFS_SOURCE_SHA256=6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=f787879847f3b5e1eaba89ee03a43997f11787ece23c4aab0cce4978f0942b50
ENV GOTOOLCHAIN=local \
    GOSUMDB=sum.golang.org

WORKDIR /src
ADD --checksum=sha256:6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd \
    https://github.com/seaweedfs/seaweedfs/archive/refs/tags/${SEAWEEDFS_VERSION}.tar.gz \
    /tmp/seaweedfs.tar.gz
COPY infra/seaweedfs/security-overrides.sum /tmp/security-overrides.sum
RUN tar --extract --gzip --file=/tmp/seaweedfs.tar.gz --strip-components=1 \
    && test "$(sha256sum /tmp/seaweedfs.tar.gz | cut -d ' ' -f 1)" = "${SEAWEEDFS_SOURCE_SHA256}" \
    && test "$(sha256sum /tmp/security-overrides.sum | cut -d ' ' -f 1)" = "${SEAWEEDFS_SECURITY_OVERRIDES_SHA256}" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' go.etcd.io/etcd/client/pkg/v3)" = "v3.6.12" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/crypto)" = "v0.54.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/image)" = "v0.44.0" \
    && go mod edit \
      -require=go.etcd.io/etcd/client/pkg/v3@v3.6.14 \
      -require=golang.org/x/crypto@v0.55.0 \
      -require=golang.org/x/image@v0.45.0 \
      -require=golang.org/x/text@v0.41.0 \
    && cat /tmp/security-overrides.sum >> go.sum \
    && go mod download \
      go.etcd.io/etcd/client/pkg/v3@v3.6.14 \
      golang.org/x/crypto@v0.55.0 \
      golang.org/x/image@v0.45.0 \
      golang.org/x/text@v0.41.0 \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' go.etcd.io/etcd/client/pkg/v3)" = "v3.6.14" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/crypto)" = "v0.55.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/image)" = "v0.45.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/text)" = "v0.41.0" \
    && go mod verify \
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
    && mkdir --parents /out/data /out/tmp \
    && chown 1000:1000 /out/data /out/tmp \
    && chmod 1777 /out/tmp

FROM scratch AS runtime

ARG SEAWEEDFS_VERSION=4.41
ARG SEAWEEDFS_SOURCE_SHA256=6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=f787879847f3b5e1eaba89ee03a43997f11787ece23c4aab0cce4978f0942b50
LABEL org.opencontainers.image.title="Custombuild SeaweedFS" \
      org.opencontainers.image.description="Minimal source-verified SeaweedFS S3 runtime" \
      org.opencontainers.image.version="${SEAWEEDFS_VERSION}" \
      org.opencontainers.image.source="https://github.com/seaweedfs/seaweedfs" \
      org.opencontainers.image.revision="${SEAWEEDFS_VERSION}" \
      io.custombuild.upstream-source.sha256="${SEAWEEDFS_SOURCE_SHA256}" \
      io.custombuild.security-overrides.sha256="${SEAWEEDFS_SECURITY_OVERRIDES_SHA256}" \
      io.custombuild.go.version="1.26.6" \
      io.custombuild.go-etcd-client-pkg.version="3.6.14" \
      io.custombuild.go-x-crypto.version="0.55.0" \
      io.custombuild.go-x-image.version="0.45.0" \
      io.custombuild.go-x-text.version="0.41.0"

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder --chown=1000:1000 /out/data /data
COPY --from=builder --chown=1000:1000 /out/tmp /tmp
COPY --from=builder /out/weed /usr/local/bin/weed
COPY --from=builder /out/healthcheck /usr/local/bin/healthcheck

ENV TMPDIR=/tmp
USER 1000:1000
VOLUME ["/data"]
EXPOSE 8333 8888 9333
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
  CMD ["/usr/local/bin/healthcheck"]
ENTRYPOINT ["/usr/local/bin/weed"]
