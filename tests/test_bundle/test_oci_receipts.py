"""Tests for the persisted OCI selector identity."""

from hashlib import sha256

import pytest

from conductor.bundle.oci import parse_oci_reference
from conductor.bundle.oci_receipts import OciReceiptError, resolve_receipt
from conductor.bundle.store import bundle_store_base

_DIGEST = "sha256:" + "a" * 64
_REF = f"oci://registry.example/team/bundle@{_DIGEST}"


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [
        (
            f'{{"version":true,"ref":"{_REF}",'
            f'"bundle_digest":"{_DIGEST}","entrypoint":"tree/w.yaml"}}',
            "Cannot read",
        ),
        (
            f'{{"version":1,"version":1,"ref":"{_REF}",'
            f'"bundle_digest":"{_DIGEST}","entrypoint":"tree/w.yaml"}}',
            "Cannot read",
        ),
        (
            f'{{"version":1,"ref":"{_REF.replace("registry", "REGISTRY")}",'
            f'"bundle_digest":"{_DIGEST}","entrypoint":"tree/w.yaml"}}',
            "reference does not match",
        ),
        (
            f'{{"version":1,"ref":"{_REF}",'
            '"bundle_digest":"../outside","entrypoint":"tree/w.yaml"}',
            "Cannot read",
        ),
    ],
)
def test_digest_receipt_rejects_malformed_or_miskeyed_records(
    payload: str, diagnostic: str
) -> None:
    # Requirement: offline rejects corrupted records rather than trusting the filename hash.
    directory = bundle_store_base() / "oci-receipts"
    directory.mkdir(parents=True)
    (directory / f"{sha256(_REF.encode()).hexdigest()}.json").write_text(payload, encoding="utf-8")

    with pytest.raises(OciReceiptError, match=diagnostic):
        resolve_receipt(parse_oci_reference(_REF))
