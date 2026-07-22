#!/usr/bin/env bash
# Provisioning example for the `conductor-agent-runner` custom-container
# session pool (epic E5, issue #284; DD6 "Bring-your-own pool" —
# docs/projects/aca/aca-provider.design.md).
#
# Conductor does NOT provision ACA infrastructure itself (DD6) — this script
# is a documented, runnable EXAMPLE of the two-step deploy the design calls
# for:
#   1. Build/push the conductor-agent-runner image to Azure Container
#      Registry (via `az acr build`, which builds in the cloud — no local
#      Docker daemon required).
#   2. Create the dynamic-sessions custom-container pool from that image,
#      then grant the caller (or a service principal/managed identity) the
#      *Session Executor* RBAC role the `aca` provider's `auth:
#      azure_default` (DefaultAzureCredential) strategy requires (FR6).
#
# The resulting pool's management endpoint is printed at the end — copy it
# into your workflow's `runtime.provider.pool_endpoint` (see
# docs/projects/aca/aca-provider-example.yaml).
#
# Prerequisites (not created by this script — bring-your-own, per DD6):
#   - An Azure resource group.
#   - A *workload-profiles-enabled* Container Apps environment
#     ($CONTAINERAPP_ENVIRONMENT) in that resource group.
#   - An Azure Container Registry ($ACR_NAME).
#
# This script DOES create one piece of supporting infrastructure: a
# user-assigned managed identity dedicated to ACR pulls
# ($REGISTRY_IDENTITY_NAME), because `az containerapp sessionpool create
# --registry-identity <id>` requires that identity to already have `acrpull`
# on the registry *before* the pool is created (the identity can't grant
# itself the role after the fact) — see
# https://learn.microsoft.com/en-us/cli/azure/containerapp/sessionpool.
#
# Usage:
#   az login
#   RESOURCE_GROUP=my-rg \
#   CONTAINERAPP_ENVIRONMENT=my-aca-env \
#   ACR_NAME=myacr \
#   POOL_NAME=my-agent-pool \
#     ./scripts/aca/provision-pool.sh
#
# All configuration is via environment variables (with defaults below) so
# the script can be sourced into CI without argument parsing; see the
# "Configuration" section for the full list.
#
# This script is an EXAMPLE, not a supported Conductor CLI command — review
# every flag (region, SKU, cooldown, egress) against your own security and
# cost requirements before running it against a real subscription.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------

RESOURCE_GROUP="${RESOURCE_GROUP:?RESOURCE_GROUP is required, e.g. RESOURCE_GROUP=my-rg}"
LOCATION="${LOCATION:-westus2}"

# Pre-existing, workload-profiles-enabled Container Apps environment (BYO —
# see Dependencies > External in aca-provider.design.md).
CONTAINERAPP_ENVIRONMENT="${CONTAINERAPP_ENVIRONMENT:?CONTAINERAPP_ENVIRONMENT is required}"

# Pre-existing Azure Container Registry (BYO).
ACR_NAME="${ACR_NAME:?ACR_NAME is required, e.g. ACR_NAME=myacr}"
IMAGE_NAME="${IMAGE_NAME:-conductor-agent-runner}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKERFILE_DIR="${DOCKERFILE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../docker/aca-runner" && pwd)}"
# Conductor release tag baked into the image (docker/aca-runner/Dockerfile's
# CONDUCTOR_VERSION build arg). Empty = use the Dockerfile's own default.
CONDUCTOR_VERSION="${CONDUCTOR_VERSION:-}"

POOL_NAME="${POOL_NAME:-conductor-agent-pool}"
TARGET_PORT="${TARGET_PORT:-8080}"
CPU="${CPU:-0.5}"
MEMORY="${MEMORY:-1.0Gi}"
MAX_SESSIONS="${MAX_SESSIONS:-20}"
READY_SESSIONS="${READY_SESSIONS:-2}"

# User-assigned managed identity dedicated to pulling the image from
# $ACR_NAME (see the Prerequisites note above). Created by this script if it
# doesn't already exist.
REGISTRY_IDENTITY_NAME="${REGISTRY_IDENTITY_NAME:-${POOL_NAME}-acrpull}"

# Advisory mirrors — these must match the `egress:` / `lifecycle:` fields an
# operator sets under `runtime.provider` in the workflow YAML
# (ProviderSettings.egress / .lifecycle, config/schema.py). The pool itself
# is the source of truth; the workflow-side fields only inform
# `conductor validate` / dashboards of the expected posture.
#   EGRESS=disabled  -> --network-status EgressDisabled (default; safest —
#                        see Security Considerations: no per-destination
#                        egress allowlist exists, only on/off)
#   EGRESS=enabled   -> --network-status EgressEnabled
EGRESS="${EGRESS:-disabled}"
#   LIFECYCLE=timed            -> --lifecycle-type Timed (default)
#   LIFECYCLE=on_container_exit -> --lifecycle-type OnContainerExit
LIFECYCLE="${LIFECYCLE:-timed}"
COOLDOWN_PERIOD="${COOLDOWN_PERIOD:-300}"
MAX_ALIVE_PERIOD="${MAX_ALIVE_PERIOD:-3600}"

# Principal to grant the Session Executor role to (defaults to the caller
# running this script, via `az ad signed-in-user show`). Set ASSIGNEE to a
# service principal or managed identity's object ID for non-interactive use
# (e.g. a CI pipeline that will later call the pool on Conductor's behalf).
ASSIGNEE="${ASSIGNEE:-}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info() { printf '  \033[1;34m->\033[0m %s\n' "$1"; }
success() { printf '  \033[1;32m OK\033[0m %s\n' "$1"; }

case "$EGRESS" in
    disabled) network_status="EgressDisabled" ;;
    enabled) network_status="EgressEnabled" ;;
    *)
        echo "EGRESS must be 'disabled' or 'enabled', got: $EGRESS" >&2
        exit 1
        ;;
esac

case "$LIFECYCLE" in
    timed) lifecycle_type="Timed" ;;
    on_container_exit) lifecycle_type="OnContainerExit" ;;
    *)
        echo "LIFECYCLE must be 'timed' or 'on_container_exit', got: $LIFECYCLE" >&2
        exit 1
        ;;
esac

# `--cooldown-period` and `--max-alive-period` are mutually exclusive in the
# `az containerapp sessionpool create` API — each only applies to one
# lifecycle type, and passing both errors out. Build the flag list for
# whichever one matches $lifecycle_type.
lifecycle_args=(--lifecycle-type "$lifecycle_type")
if [ "$lifecycle_type" = "Timed" ]; then
    lifecycle_args+=(--cooldown-period "$COOLDOWN_PERIOD")
else
    lifecycle_args+=(--max-alive-period "$MAX_ALIVE_PERIOD")
fi

# ---------------------------------------------------------------------------
# Step 1: build + push the runner image to ACR
# ---------------------------------------------------------------------------

info "Building and pushing ${IMAGE_NAME}:${IMAGE_TAG} via 'az acr build' (cloud build, no local Docker required)..."
acr_build_args=(
    --registry "$ACR_NAME"
    --image "${IMAGE_NAME}:${IMAGE_TAG}"
    --file "${DOCKERFILE_DIR}/Dockerfile"
    # The pool's --target-port (below) must match what the image actually
    # listens on — forward it so the two never drift apart.
    --build-arg "TARGET_PORT=${TARGET_PORT}"
)
if [ -n "$CONDUCTOR_VERSION" ]; then
    acr_build_args+=(--build-arg "CONDUCTOR_VERSION=${CONDUCTOR_VERSION}")
fi
az acr build "${acr_build_args[@]}" "$DOCKERFILE_DIR"
success "Image pushed to ${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# Step 2: create the user-assigned identity and grant it acrpull BEFORE the
# pool is created — `--registry-identity` must already have `acrpull` on the
# registry at pool-creation time (Azure does not retroactively grant it), and
# a user-assigned identity (rather than the pool's own system-assigned
# identity) lets it be provisioned ahead of the pool that will reference it.
# ---------------------------------------------------------------------------

info "Ensuring user-assigned identity '${REGISTRY_IDENTITY_NAME}' exists..."
az identity create \
    --name "$REGISTRY_IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none
registry_identity_id="$(az identity show \
    --name "$REGISTRY_IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query id \
    --output tsv)"
registry_identity_principal_id="$(az identity show \
    --name "$REGISTRY_IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query principalId \
    --output tsv)"
success "Identity ready: ${registry_identity_id}"

info "Granting '${REGISTRY_IDENTITY_NAME}' the 'acrpull' role on ${ACR_NAME}..."
acr_resource_id="$(az acr show --name "$ACR_NAME" --query id --output tsv)"
az role assignment create \
    --role "acrpull" \
    --assignee "$registry_identity_principal_id" \
    --scope "$acr_resource_id" \
    --output none
success "acrpull granted."

# ---------------------------------------------------------------------------
# Step 3: create the custom-container session pool from that image
# ---------------------------------------------------------------------------

info "Creating session pool '${POOL_NAME}' (container-type CustomContainer)..."
az containerapp sessionpool create \
    --name "$POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --environment "$CONTAINERAPP_ENVIRONMENT" \
    --container-type CustomContainer \
    --image "${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}" \
    --registry-server "${ACR_NAME}.azurecr.io" \
    --registry-identity "$registry_identity_id" \
    --target-port "$TARGET_PORT" \
    --cpu "$CPU" \
    --memory "$MEMORY" \
    --max-sessions "$MAX_SESSIONS" \
    --ready-sessions "$READY_SESSIONS" \
    --network-status "$network_status" \
    "${lifecycle_args[@]}"
success "Session pool '${POOL_NAME}' created."

# ---------------------------------------------------------------------------
# Step 4: grant the Session Executor role (FR6 — required for
# `auth: azure_default` / DefaultAzureCredential on the host)
# ---------------------------------------------------------------------------

if [ -z "$ASSIGNEE" ]; then
    info "ASSIGNEE not set; defaulting to the signed-in user (az ad signed-in-user show)."
    ASSIGNEE="$(az ad signed-in-user show --query id --output tsv)"
fi

session_pool_resource_id="$(az containerapp sessionpool show \
    --name "$POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query id \
    --output tsv)"

info "Granting 'Azure ContainerApps Session Executor' on the pool to ${ASSIGNEE}..."
az role assignment create \
    --role "Azure ContainerApps Session Executor" \
    --assignee "$ASSIGNEE" \
    --scope "$session_pool_resource_id"
success "Session Executor role granted."

# ---------------------------------------------------------------------------
# Done — print the pool_endpoint for runtime.provider.pool_endpoint
# ---------------------------------------------------------------------------

pool_endpoint="$(az containerapp sessionpool show \
    --name "$POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query "properties.poolManagementEndpoint" \
    --output tsv)"

echo
success "Pool ready. Set this as runtime.provider.pool_endpoint in your workflow YAML:"
echo "  ${pool_endpoint}"
