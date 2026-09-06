# syntax=docker/dockerfile:1.7

# Build the exact SeaweedFS release archive with a vulnerability-remediated Go
# toolchain and checksum-locked module overrides. Keep the runtime shell- and
# package-free.
FROM golang:1.26.6-alpine3.24@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS builder

ARG SEAWEEDFS_VERSION=4.45
ARG SEAWEEDFS_SOURCE_SHA256=800a91693e2a4e974ef0e2a157b6a04eff9f4df507303b1cff31b0601081fc30
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=3ae484c86166bb6cd88f24398577d99ed06ea8bf5c9384d5288094d7d7048d05
ENV GOTOOLCHAIN=local \
    GOSUMDB=sum.golang.org

WORKDIR /src
ADD --checksum=sha256:800a91693e2a4e974ef0e2a157b6a04eff9f4df507303b1cff31b0601081fc30 \
    https://github.com/seaweedfs/seaweedfs/archive/refs/tags/${SEAWEEDFS_VERSION}.tar.gz \
    /tmp/seaweedfs.tar.gz
COPY infra/seaweedfs/security-overrides.sum /tmp/security-overrides.sum
RUN tar --extract --gzip --file=/tmp/seaweedfs.tar.gz --strip-components=1 \
    && test "$(sha256sum /tmp/seaweedfs.tar.gz | cut -d ' ' -f 1)" = "${SEAWEEDFS_SOURCE_SHA256}" \
    && test "$(sha256sum /tmp/security-overrides.sum | cut -d ' ' -f 1)" = "${SEAWEEDFS_SECURITY_OVERRIDES_SHA256}" \
    && test "$(go list -m -mod=readonly -f '{{if .Replace}}{{.Replace.Version}}{{else}}{{.Version}}{{end}}' github.com/apache/thrift)" = "v0.23.1-0.20260429145742-d2acd3c49e58" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' go.etcd.io/etcd/client/pkg/v3)" = "v3.7.1" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/crypto)" = "v0.55.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/image)" = "v0.44.0" \
    && go mod edit -go=1.26.0 \
    && go mod edit -dropreplace=github.com/apache/thrift \
    && go mod edit \
      -require=github.com/apache/thrift@v0.24.0 \
      -require=golang.org/x/crypto@v0.56.0 \
      -require=golang.org/x/image@v0.45.0 \
    && cat /tmp/security-overrides.sum >> go.sum \
    && go mod download \
      github.com/apache/thrift@v0.24.0 \
      go.etcd.io/etcd/client/pkg/v3@v3.7.1 \
      golang.org/x/crypto@v0.56.0 \
      golang.org/x/image@v0.45.0 \
      golang.org/x/text@v0.41.0 \
    && test "$(go list -m -mod=readonly -f '{{if .Replace}}{{.Replace.Version}}{{else}}{{.Version}}{{end}}' github.com/apache/thrift)" = "v0.24.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' go.etcd.io/etcd/client/pkg/v3)" = "v3.7.1" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/crypto)" = "v0.56.0" \
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

ARG SEAWEEDFS_VERSION=4.45
ARG SEAWEEDFS_SOURCE_SHA256=800a91693e2a4e974ef0e2a157b6a04eff9f4df507303b1cff31b0601081fc30
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=3ae484c86166bb6cd88f24398577d99ed06ea8bf5c9384d5288094d7d7048d05
LABEL org.opencontainers.image.title="Custombuild SeaweedFS" \
      org.opencontainers.image.description="Minimal source-verified SeaweedFS S3 runtime" \
      org.opencontainers.image.version="${SEAWEEDFS_VERSION}" \
      org.opencontainers.image.source="https://github.com/seaweedfs/seaweedfs" \
      org.opencontainers.image.revision="${SEAWEEDFS_VERSION}" \
      io.custombuild.upstream-source.sha256="${SEAWEEDFS_SOURCE_SHA256}" \
      io.custombuild.security-overrides.sha256="${SEAWEEDFS_SECURITY_OVERRIDES_SHA256}" \
      io.custombuild.go.version="1.26.6" \
      io.custombuild.go-apache-thrift.version="0.24.0" \
      io.custombuild.go-etcd-client-pkg.version="3.7.1" \
      io.custombuild.go-x-crypto.version="0.56.0" \
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
