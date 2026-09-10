# syntax=docker/dockerfile:1.7

# Build the exact SeaweedFS release archive with a vulnerability-remediated Go
# toolchain and checksum-locked module overrides. Keep the runtime shell- and
# package-free.
FROM golang:1.26.6-alpine3.24@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS builder

ARG SEAWEEDFS_VERSION=4.45
ARG SEAWEEDFS_SOURCE_SHA256=800a91693e2a4e974ef0e2a157b6a04eff9f4df507303b1cff31b0601081fc30
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=4b25078bdc80af5a2ed724eb8f8c86724cee26fb75d861d1603bfa28e294509f
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
      -require=google.golang.org/grpc@v1.85.0-dev.0.20260825072537-93e31b48545e \
      -require=cel.dev/expr@v0.25.3 \
      -require=github.com/GoogleCloudPlatform/opentelemetry-operations-go/detectors/gcp@v1.35.0 \
      -require=github.com/envoyproxy/go-control-plane/envoy@v1.39.0 \
      -require=github.com/go-logr/logr@v1.4.4 \
      -require=github.com/googleapis/enterprise-certificate-proxy@v0.3.21 \
      -require=go.opentelemetry.io/contrib/detectors/gcp@v1.45.0 \
      -require=go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp@v0.70.0 \
      -require=go.opentelemetry.io/otel@v1.45.0 \
      -require=go.opentelemetry.io/otel/metric@v1.45.0 \
      -require=go.opentelemetry.io/otel/sdk@v1.45.0 \
      -require=go.opentelemetry.io/otel/sdk/metric@v1.45.0 \
      -require=go.opentelemetry.io/otel/trace@v1.45.0 \
      -require=google.golang.org/genproto/googleapis/api@v0.0.0-20260817212433-ac3dfec99bb1 \
    && cat /tmp/security-overrides.sum >> go.sum \
    && go mod download \
      github.com/apache/thrift@v0.24.0 \
      go.etcd.io/etcd/client/pkg/v3@v3.7.1 \
      golang.org/x/crypto@v0.56.0 \
      golang.org/x/image@v0.45.0 \
      golang.org/x/text@v0.41.0 \
      google.golang.org/grpc@v1.85.0-dev.0.20260825072537-93e31b48545e \
      cel.dev/expr@v0.25.3 \
      github.com/GoogleCloudPlatform/opentelemetry-operations-go/detectors/gcp@v1.35.0 \
      github.com/envoyproxy/go-control-plane/envoy@v1.39.0 \
      github.com/go-logr/logr@v1.4.4 \
      github.com/googleapis/enterprise-certificate-proxy@v0.3.21 \
      go.opentelemetry.io/contrib/detectors/gcp@v1.45.0 \
      go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp@v0.70.0 \
      go.opentelemetry.io/otel@v1.45.0 \
      go.opentelemetry.io/otel/metric@v1.45.0 \
      go.opentelemetry.io/otel/sdk@v1.45.0 \
      go.opentelemetry.io/otel/sdk/metric@v1.45.0 \
      go.opentelemetry.io/otel/trace@v1.45.0 \
      google.golang.org/genproto/googleapis/api@v0.0.0-20260817212433-ac3dfec99bb1 \
    && test "$(go list -m -mod=readonly -f '{{if .Replace}}{{.Replace.Version}}{{else}}{{.Version}}{{end}}' github.com/apache/thrift)" = "v0.24.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' go.etcd.io/etcd/client/pkg/v3)" = "v3.7.1" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/crypto)" = "v0.56.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/image)" = "v0.45.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' golang.org/x/text)" = "v0.41.0" \
    && test "$(go list -m -mod=readonly -f '{{.Version}}' google.golang.org/grpc)" = "v1.85.0-dev.0.20260825072537-93e31b48545e" \
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
ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256=4b25078bdc80af5a2ed724eb8f8c86724cee26fb75d861d1603bfa28e294509f
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
      io.custombuild.go-x-text.version="0.41.0" \
      io.custombuild.go-grpc.version="v1.85.0-dev.0.20260825072537-93e31b48545e"

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
