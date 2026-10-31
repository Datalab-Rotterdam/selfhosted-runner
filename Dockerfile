FROM ubuntu:24.04

ARG TARGETARCH
ARG RUNNER_VERSION=2.338.0
# sha256 of the linux-x64 tarball; set RUNNER_SHA256_ARM64 to verify arm64 builds
ARG RUNNER_SHA256_X64=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
ARG RUNNER_SHA256_ARM64=

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq sudo tini unzip \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner

USER runner
WORKDIR /home/runner/actions-runner

RUN case "${TARGETARCH:-amd64}" in \
        amd64) ARCH=x64;   SHA="${RUNNER_SHA256_X64}" ;; \
        arm64) ARCH=arm64; SHA="${RUNNER_SHA256_ARM64}" ;; \
        *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && FILE="actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz" \
    && curl -fsSL -o "${FILE}" "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${FILE}" \
    && if [ -n "${SHA}" ]; then echo "${SHA}  ${FILE}" | sha256sum -c -; else echo "WARN: no sha256 for ${ARCH}, skipping verify"; fi \
    && tar xzf "${FILE}" \
    && rm "${FILE}" \
    && sudo ./bin/installdependencies.sh \
    && sudo rm -rf /var/lib/apt/lists/*

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh
RUN chmod +x /home/runner/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/home/runner/entrypoint.sh"]
