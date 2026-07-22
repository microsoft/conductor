# syntax=docker/dockerfile:1.7
#
# Official base image for `conductor-agent-runner`, the in-container HTTP
# server the `aca` provider drives inside an Azure Container Apps dynamic-
# sessions custom-container pool (epic E5, issue #284).
#
# This is the "zero-config fast path" half of the design's *Image ownership*
# answer (docs/projects/aca/aca-provider.design.md, "Open Questions"): a
# pinned Conductor install (which ships the `conductor.aca_runner` server
# in-package, epic E4) plus the common stdio MCP binaries most workflows rely
# on, so `az containerapp sessionpool create --container-type CustomContainer
# --image conductor-agent-runner:<tag> ...` works out of the box. The other
# half — the `/execute` + `/health` HTTP contract and NDJSON event-frame
# schema — is documented separately (aca-provider.design.md "API Contracts")
# as an escape hatch for a fully custom, non-extending image.
#
# Build (context is this directory):
#   docker build -t conductor-agent-runner:<tag> docker/aca-runner
#
# Extend with extra MCP servers / language toolchains/ system deps:
#   FROM conductor-agent-runner:<tag>
#   RUN pip install --no-cache-dir my-extra-mcp-server
#
# `CONDUCTOR_VERSION` is any git ref (tag or commit SHA) accepted by
# `pip install pkg @ git+https://...@<ref>` that must include the
# `conductor.aca_runner` module added by epic E4 — pin it explicitly (rather
# than trusting a floating default) for reproducible builds; override at
# build time with `--build-arg CONDUCTOR_VERSION=<ref>`.
# Conductor has no PyPI package; this mirrors install.sh's own install
# mechanism (`uv tool install git+https://github.com/<repo>.git@<tag>`).
#
# No released tag contains `conductor.aca_runner` yet (epic E4 hasn't shipped
# in a `microsoft/conductor` release as of this writing), so the default
# below is pinned to the immutable commit SHA that introduced it rather than
# a floating tag — a floating default here would silently produce a
# `ModuleNotFoundError: No module named 'conductor.aca_runner'` container that
# builds cleanly but crashes on `CMD`. Verified: `pip install "conductor-cli @
# git+https://github.com/microsoft/conductor.git@d6db5c817f5c0ae8146fdb7a1e8d3d48b486ef3d"`
# installs a package exposing `python -m conductor.aca_runner`, and running it
# answers `GET /health`. Once a real release tag ships with the runner,
# update this default to that tag (`vX.Y.Z`) — see
# docs/projects/aca/aca-provider.plan.md (epic E5).
ARG CONDUCTOR_VERSION=d6db5c817f5c0ae8146fdb7a1e8d3d48b486ef3d

# Session port used for ingress traffic — must match the pool's
# `--target-port` (see scripts/aca/provision-pool.sh). Overridable at build
# time with `--build-arg TARGET_PORT=<port>`.
ARG TARGET_PORT=8080

FROM python:3.12-slim

# Fail the nodesource install pipe below on either side, not just the last
# command in the pipeline.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# git: baked in per the runner-image contract — the coding-agent pattern in
# docs/projects/aca/aca-provider-example.yaml clones/edits/tests entirely
# in-sandbox. Node.js/npm: the transport the common stdio MCP servers use
# (`command: npx`, see docs/mcp-tools.md) — a declared-but-absent stdio
# binary is a *runtime error* under the runner-image contract
# (`_check_stdio_binaries` in src/conductor/aca_runner/server.py), so baking
# these in covers the common case out of the box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

ARG CONDUCTOR_VERSION
# The runner ships in-package with conductor-cli (epic E4 decision — no
# separate runner package to install).
RUN pip install --no-cache-dir \
    "conductor-cli @ git+https://github.com/microsoft/conductor.git@${CONDUCTOR_VERSION}"

ARG TARGET_PORT
ENV ACA_RUNNER_HOST=0.0.0.0
ENV ACA_RUNNER_PORT=${TARGET_PORT}

# Container-relative working directory (the `sandbox.working_dir` semantics —
# see the Open Questions answer in aca-provider.plan.md); owned by a non-root
# user so agent-driven file/git operations don't require root.
RUN useradd --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /workspace \
    && chown runner:runner /workspace
WORKDIR /workspace
USER runner

EXPOSE ${TARGET_PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${ACA_RUNNER_PORT}/health" || exit 1

CMD ["python", "-m", "conductor.aca_runner"]
