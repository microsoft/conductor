"""Canonical references and the local validation boundary for OCI RunBundle v1."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Literal, Self, TypeGuard

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from conductor.bundle.model import BundleManifest, compute_bundle_digest
from conductor.digest import canonical_json

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
ARTIFACT_TYPE = "application/vnd.microsoft.conductor.runbundle.v1"
CONFIG_MEDIA_TYPE = "application/vnd.microsoft.conductor.runbundle.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.microsoft.conductor.runbundle.layer.v1.tar+gzip"
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_CONFIG_BYTES = MAX_INNER_MANIFEST_BYTES = 64 * 1024**2
MAX_LAYER_BYTES = MAX_TAR_BYTES = 640 * 1024**2
MAX_PAYLOAD_BYTES = 512 * 1024**2
MAX_ENTRIES = 10_000
MAX_NODES = 50_000
MAX_PATH_BYTES = 1024

_SHA256 = r"sha256:[0-9a-f]{64}"
_TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REFERENCE = re.compile(
    rf"oci://(?P<host>\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)"
    rf"(?::(?P<port>[0-9]{{1,5}}))?/(?P<repo>{_COMPONENT}(?:/{_COMPONENT})*)"
    rf"(?::(?P<tag>{_TAG})|@(?P<digest>{_SHA256}))"
)


class OciFormatError(ValueError):
    """An invalid OCI RunBundle reference or wire document."""


@dataclass(frozen=True, slots=True)
class OciReference:
    """Parsed registry authority, repository and case-sensitive selector."""

    authority: str
    repository: str
    selector: str

    @property
    def is_digest(self) -> bool:
        return self.selector.startswith("sha256:")

    @property
    def repository_with_selector(self) -> str:
        separator = "@" if self.is_digest else ":"
        return f"{self.repository}{separator}{self.selector}"

    def __str__(self) -> str:
        return f"oci://{self.authority}/{self.repository_with_selector}"


def parse_oci_reference(value: str) -> OciReference:
    """Parse an explicit tag or digest, without applying any Docker defaults."""
    match = _REFERENCE.fullmatch(value)
    if match is None:
        raise OciFormatError("Expected oci://HOST/REPOSITORY:TAG or oci://HOST/REPOSITORY@sha256:…")
    host, raw_port, repository, tag, digest = match.groups()
    host = host.lower()
    try:
        if host.startswith("["):
            host = f"[{IPv6Address(host[1:-1])}]"
        elif "." in host and all(part.isdigit() for part in host.split(".")):
            host = str(IPv4Address(host))
        elif len(host) > 253 or not all(re.fullmatch(_DNS_LABEL, part) for part in host.split(".")):
            raise ValueError("Invalid DNS name")
    except ValueError as exc:
        raise OciFormatError("Invalid OCI registry host") from exc
    port = int(raw_port) if raw_port is not None else 443
    if not 1 <= port <= 65535:
        raise OciFormatError("Invalid OCI registry port")
    authority = host if port == 443 else f"{host}:{port}"
    return OciReference(authority, repository, tag if digest is None else digest)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OciFormatError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise OciFormatError("Non-finite JSON number")


def _load(raw: bytes, limit: int) -> object:
    if len(raw) > limit:
        raise OciFormatError("OCI JSON exceeds its byte limit")
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except OciFormatError:
        raise
    except (ValueError, RecursionError) as exc:
        raise OciFormatError("Invalid OCI JSON") from exc


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    # Only used on values from json.loads, whose object keys are always strings.
    return isinstance(value, dict)


def _manifest_input(value: object) -> object:
    if not _is_json_object(value):
        raise ValueError("Expected a bundle manifest object")
    version = value.get("version")
    entries = value.get("entries")
    if type(version) is not int or version != 1:
        raise ValueError("Invalid bundle manifest version")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid bundle entry list or entry count")
    # The wire array is JSON, but the shared strict model stores a tuple.
    return {**value, "entries": tuple(entries)}


def _root_entrypoint(manifest: BundleManifest) -> str:
    roots = [
        entry.logical_path
        for entry in manifest.entries
        if entry.kind == "file"
        and entry.origin_kind == "workflow"
        and entry.origin_detail == "workflow:root"
    ]
    if len(roots) != 1 or not roots[0].startswith("tree/"):
        raise OciFormatError("Expected exactly one regular root workflow under tree/")
    return roots[0]


class OciConfig(BaseModel):
    """Checked RunBundle config, with its original shared content model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Annotated[int, Field(ge=1, le=1)]
    bundle_digest: Annotated[str, Field(pattern=rf"^{_SHA256}$")]
    entrypoint: str
    manifest: Annotated[BundleManifest, BeforeValidator(_manifest_input)]

    @model_validator(mode="after")
    def _check_content(self) -> Self:
        entries = self.manifest.entries
        previous = ""
        nodes: set[str] = set()
        payload_size = 0
        for entry in entries:
            path = entry.logical_path
            if path <= previous:
                raise ValueError("Bundle entries must have strictly increasing logical paths")
            previous = path
            for name in (path, entry.link_target):
                if name is not None and len(name.encode("utf-8")) > MAX_PATH_BYTES:
                    raise ValueError("Bundle path or link target exceeds its UTF-8 byte limit")
            if entry.size < 0 or not re.fullmatch(_SHA256, entry.digest):
                raise ValueError("Invalid bundle entry size or digest")
            if entry.kind == "file":
                payload_size += entry.size
                if payload_size > MAX_PAYLOAD_BYTES:
                    raise ValueError("Bundle file payload exceeds its byte limit")
            nodes.add(path)
            while "/" in path:
                path = path.rsplit("/", 1)[0]
                if path in nodes:
                    break
                nodes.add(path)
                if len(nodes) > MAX_NODES:
                    raise ValueError("Bundle exceeds its node limit")
            if len(nodes) > MAX_NODES:
                raise ValueError("Bundle exceeds its node limit")

        if self.entrypoint != _root_entrypoint(self.manifest):
            raise ValueError("Bundle entrypoint is not the regular root workflow")
        digest = compute_bundle_digest(
            entries, self.manifest.skills_topology, self.manifest.plugins_topology
        )
        if self.bundle_digest != digest or self.manifest.bundle_digest != digest:
            raise ValueError("Bundle content digest mismatch")
        return self


class OciDescriptor(BaseModel):
    """Digest and byte size for one config or compressed layer blob."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mediaType: Literal[
        "application/vnd.microsoft.conductor.runbundle.config.v1+json",
        "application/vnd.microsoft.conductor.runbundle.layer.v1.tar+gzip",
    ]
    digest: Annotated[str, Field(pattern=rf"^{_SHA256}$")]
    size: Annotated[int, Field(ge=0)]


class OciManifest(BaseModel):
    """OCI image manifest for one RunBundle config and one layer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schemaVersion: Annotated[int, Field(ge=2, le=2)]
    mediaType: Literal["application/vnd.oci.image.manifest.v1+json"]
    artifactType: Literal["application/vnd.microsoft.conductor.runbundle.v1"]
    config: OciDescriptor
    layers: Annotated[tuple[OciDescriptor, ...], Field(min_length=1, max_length=1)]

    @model_validator(mode="after")
    def _check_descriptors(self) -> Self:
        if self.config.mediaType != CONFIG_MEDIA_TYPE or self.config.size > MAX_CONFIG_BYTES:
            raise ValueError("Invalid OCI config descriptor")
        layer = self.layers[0]
        if layer.mediaType != LAYER_MEDIA_TYPE or layer.size > MAX_LAYER_BYTES:
            raise ValueError("Invalid OCI layer descriptor")
        return self


def parse_oci_config(raw: bytes) -> OciConfig:
    """Validate bounded config bytes and recompute the shared bundle digest."""
    try:
        # A strict parent config alone does not make the shared nested model strict.
        return OciConfig.model_validate(_load(raw, MAX_CONFIG_BYTES), strict=True)
    except OciFormatError:
        raise
    except ValueError as exc:
        raise OciFormatError("Invalid OCI RunBundle config") from exc


def encode_oci_config(manifest: BundleManifest) -> bytes:
    """Encode a checked config, ordering entries without changing their identity."""
    payload = manifest.model_dump(mode="json")
    payload["entries"] = sorted(payload["entries"], key=lambda entry: entry["logical_path"])
    raw = canonical_json(
        {
            "version": 1,
            "bundle_digest": manifest.bundle_digest,
            "entrypoint": _root_entrypoint(manifest),
            "manifest": payload,
        }
    ).encode("utf-8")
    parse_oci_config(raw)
    return raw


def parse_oci_manifest(raw: bytes) -> OciManifest:
    """Validate bounded manifest bytes; callers hash the original bytes for M."""
    payload = _load(raw, MAX_MANIFEST_BYTES)
    if not _is_json_object(payload) or not isinstance(layers := payload.get("layers"), list):
        raise OciFormatError("Expected an OCI manifest object with a layers array")
    try:
        return OciManifest.model_validate({**payload, "layers": tuple(layers)})
    except ValueError as exc:
        raise OciFormatError("Invalid OCI RunBundle manifest") from exc


def encode_oci_manifest(config: OciDescriptor, layer: OciDescriptor) -> bytes:
    """Build exact manifest bytes to hash and transmit unchanged."""
    manifest = OciManifest(
        schemaVersion=2,
        mediaType=MANIFEST_MEDIA_TYPE,
        artifactType=ARTIFACT_TYPE,
        config=config,
        layers=(layer,),
    )
    return canonical_json(manifest.model_dump(mode="json")).encode("utf-8")
