#!/usr/bin/env python3
"""Test-only ``az`` shim used by ``test_aca_provision_pool.py``.

Copied into a per-test bin directory (as ``az``) and prepended onto ``PATH``
so every ``az ...`` invocation inside ``scripts/aca/provision-pool.sh``
resolves here instead of hitting real Azure. Records the full argv of every
invocation (one JSON array per line) to the file named by
``MOCK_AZ_LOG``, then returns canned output for the handful of ``--query``
lookups the script depends on to keep executing — driven by
``MOCK_ACR_ROLE_ASSIGNMENT_MODE`` so tests can flip the ABAC-vs-legacy
registry branch without needing a real ACR.

Filename starts with ``_`` so pytest does not collect it as a test module.
"""

from __future__ import annotations

import json
import os
import sys

args = sys.argv[1:]

log_path = os.environ["MOCK_AZ_LOG"]
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")


def _query_value() -> str | None:
    for i, arg in enumerate(args):
        if arg == "--query" and i + 1 < len(args):
            return args[i + 1]
    return None


query = _query_value()
joined = " ".join(args)

output: str | None = None

if joined.startswith("identity show"):
    if query == "id":
        output = (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
            "providers/Microsoft.ManagedIdentity/userAssignedIdentities/mock-identity"
        )
    elif query == "principalId":
        output = "11111111-1111-1111-1111-111111111111"
elif joined.startswith("acr show"):
    if query == "id":
        output = (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
            "providers/Microsoft.ContainerRegistry/registries/mockacr"
        )
    elif query == "roleAssignmentMode":
        output = os.environ.get("MOCK_ACR_ROLE_ASSIGNMENT_MODE", "LegacyRegistryPermissions")
elif joined.startswith("containerapp sessionpool show"):
    if query == "id":
        output = (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
            "providers/Microsoft.App/sessionPools/mock-pool"
        )
    elif query == "properties.poolManagementEndpoint":
        output = "https://mock-pool.example.azurecontainerapps.io"
elif joined.startswith("ad signed-in-user show"):
    output = "22222222-2222-2222-2222-222222222222"

if output is not None:
    print(output)

sys.exit(0)
