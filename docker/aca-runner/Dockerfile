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
# below is pinned to an immutable commit SHA rather than a floating tag — a
# floating default here would silently produce a `ModuleNotFoundError: No
# module named 'conductor.aca_runner'` container that builds cleanly but
# crashes on `CMD`.
#
# IMPORTANT (review fix, issue #284): the pin MUST be a commit that includes
# epic E9's runner fix (`_InnerProviderCache.get()` popping `github_token`
# before constructing `ProviderSettings`), not just E4's original
# `conductor.aca_runner` module. The host's *default* credential path
# (epic E8/E10 — export `COPILOT_GITHUB_TOKEN`, set no
# `COPILOT_PROVIDER_BASE_URL`) forwards `{"github_token": <secret>}` in the
# `/execute` request body; a pre-E9 runner instead does
# `ProviderSettings(name="copilot", **inner_provider_settings)` directly,
# which raises a Pydantic `extra="forbid"` `ValidationError` on that exact
# body — the documented Quick Start's happy path fails end-to-end against
# an E4-only image. `tests/test_aca_runner/test_server.py::
# TestHostRunnerCredentialContract` pins this contract in-repo so a future
# regression fails CI instead of only failing at image build/run time.
#
# The pinned ref MUST also actually exist on the `microsoft/conductor`
# remote (reachable by `pip install`'s `git+https` fetch — a commit that
# only exists on a local clone is NOT a usable pin, since the default
# GitHub install has nothing to resolve). Once a real release tag ships
# with the runner, update this default to that tag (`vX.Y.Z`) — see
# docs/projects/aca/aca-provider.plan.md (epic E5). Until then, whoever
# bumps this pin is responsible for pushing the referenced commit to
# `microsoft/conductor` first and re-verifying:
#   pip install "conductor-cli @ git+https://github.com/microsoft/conductor.git@<ref>"
# installs a package exposing `python -m conductor.aca_runner`, answering
# `GET /health`, and accepting a `github_token`-only `inner_provider_settings`
# body at `/execute`.
ARG CONDUCTOR_VERSION=e0a4214e30327c7c0fee2852ae0feb81e2051b00

# Session port used for ingress traffic — must match the pool's
# `--target-port` (see scripts/aca/provision-pool.sh). Overridable at build
# time with `--build-arg TARGET_PORT=<port>`.
ARG TARGET_PORT=8080

FROM python:3.12-slim

# Fail the nodesource install pipe below on either side, not just the last
# command in the pipeline.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# git: baked in per the runner-image contract — the coding-agent pattern in
# examples/aca-coding-agent.yaml clones/edits/tests entirely
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

# git-mcp-server: the stdio MCP binary examples/aca-coding-agent.yaml
# declares (`mcp_servers.git.command: git-mcp-server`) — bake it in so that
# example (and any workflow relying on the same binary name) works out of the
# box even with pool egress disabled, per the runner-image contract's "common
# stdio MCP binaries" promise. Pinned (not `@latest`) for reproducible builds;
# `git-mcp-server` is the npm package's own `bin` name
# (https://www.npmjs.com/package/@cyanheads/git-mcp-server).
RUN npm install -g --no-fund --no-audit @cyanheads/git-mcp-server@2.15.1

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
