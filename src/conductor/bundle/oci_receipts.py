"""Local OCI selectors for RunBundles already verified in the CAS."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, suppress
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from conductor.bundle import store
from conductor.bundle.errors import BundleError
from conductor.bundle.importer import verify_bundle
from conductor.bundle.oci import OciConfig, OciReference, _load, _root_entrypoint

_OFFLINE_LIMIT: Final = 4 * 1024 * 1024


class OciReceiptError(BundleError):
    """The local OCI selector could not be recorded or verified."""


class _Selector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Annotated[int, Field(ge=1, le=1)]
    ref: str


class OciReceipt(_Selector):
    """Immutable mapping from a repository manifest digest to verified content."""

    bundle_digest: str
    entrypoint: str


class _TagPointer(_Selector):
    manifest_digest: str


def _directory(*, create: bool = False) -> Path:
    directory = store._checked_store_base(create=create) / "oci-receipts"
    if create:
        with suppress(FileExistsError):
            directory.mkdir()
    if not stat.S_ISDIR(store._plain_stat(directory).st_mode):
        raise OciReceiptError("OCI receipt directory is not a directory")
    return directory


def _key(directory: Path, ref: str) -> Path:
    return directory / f"{sha256(ref.encode('utf-8')).hexdigest()}.json"


def _read[T: _Selector](directory: Path, ref: str, model: type[T]) -> T:
    path = _key(directory, ref)
    if not stat.S_ISREG(store._plain_stat(path).st_mode):
        raise OciReceiptError("OCI selector is not a regular file")
    with path.open("rb") as contents:
        result = model.model_validate(_load(contents.read(_OFFLINE_LIMIT + 1), _OFFLINE_LIMIT))
    if result.ref != ref:
        raise OciReceiptError("OCI selector reference does not match its key")
    return result


def _write(directory: Path, selector: _Selector) -> None:
    # Close the file before replacement on Windows and clean up on interruption.
    with ExitStack() as cleanup:
        with NamedTemporaryFile(dir=directory, delete=False) as temporary:
            path = Path(temporary.name)
            cleanup.callback(path.unlink, missing_ok=True)
            temporary.write(selector.model_dump_json().encode("utf-8"))
        os.replace(path, _key(directory, selector.ref))
        cleanup.pop_all()


def record_receipt(reference: OciReference, manifest_digest: str, config: OciConfig) -> OciReceipt:
    """Record a successful transfer only after the caller verifies its CAS entry."""
    store.bundle_store_key(manifest_digest)
    if reference.is_digest and reference.selector != manifest_digest:
        raise OciReceiptError("OCI receipt digest does not match the requested digest")
    immutable = str(replace(reference, selector=manifest_digest))
    receipt = OciReceipt(
        version=1, ref=immutable, bundle_digest=config.bundle_digest, entrypoint=config.entrypoint
    )
    directory = _directory(create=True)
    _write(directory, receipt)
    if not reference.is_digest:
        pointer = _TagPointer(version=1, ref=str(reference), manifest_digest=manifest_digest)
        try:
            _write(directory, pointer)
        except OSError as exc:
            raise OciReceiptError(
                f"Could not update the local tag pointer: {exc}\n"
                f"The verified bundle remains available offline as:\n{immutable}"
            ) from exc
    return receipt


def resolve_receipt(reference: OciReference) -> OciReceipt:
    """Resolve a local selector and verify its existing CAS without repairing it."""
    try:
        directory = _directory()
        immutable = str(reference)
        if not reference.is_digest:
            pointer = _read(directory, immutable, _TagPointer)
            store.bundle_store_key(pointer.manifest_digest)
            immutable = str(replace(reference, selector=pointer.manifest_digest))
        receipt = _read(directory, immutable, OciReceipt)
        store.bundle_store_key(receipt.bundle_digest)
    except FileNotFoundError:
        raise OciReceiptError(
            f"No local OCI receipt for {reference}. Run bundle pull without --offline to fetch it."
        ) from None
    except (OSError, ValueError) as exc:
        raise OciReceiptError(f"Cannot read local OCI receipt for {reference}: {exc}") from exc

    manifest = verify_bundle(receipt.bundle_digest)
    if receipt.entrypoint != _root_entrypoint(manifest):
        raise OciReceiptError("OCI receipt entrypoint does not match the verified bundle")
    return receipt
