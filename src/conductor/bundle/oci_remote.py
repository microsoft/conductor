"""Transfer exact RunBundle representations through the public ORAS SDK."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, BinaryIO, Final, Literal

from conductor.bundle.errors import BundleError
from conductor.bundle.oci import (
    CONFIG_MEDIA_TYPE,
    LAYER_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    MAX_LAYER_BYTES,
    MAX_MANIFEST_BYTES,
    OciConfig,
    OciDescriptor,
    OciReference,
    encode_oci_config,
    encode_oci_manifest,
    parse_oci_config,
    parse_oci_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from requests import Response

    from conductor.bundle.model import BundleManifest

_CHUNK: Final = 64 * 1024
AuthBackend = Literal["token", "basic"]


class OciRemoteError(BundleError):
    """An OCI registry operation did not produce a verified result."""


@dataclass(frozen=True, slots=True)
class PulledBundle:
    """Verified wire result; the layer path is valid within the pull context."""

    manifest_digest: str
    config: OciConfig
    layer_path: Path


@contextmanager
def _translate_failure(operation: str) -> Iterator[None]:
    from subprocess import SubprocessError

    from oras.auth import AuthenticationException
    from urllib3.exceptions import HTTPError

    try:
        yield
    except (
        AuthenticationException,
        OSError,
        ValueError,
        HTTPError,
        SubprocessError,
        KeyError,
        TypeError,
    ):
        raise OciRemoteError(f"OCI {operation} failed") from None


def _stream(response: Response, sink: BinaryIO, limit: int) -> tuple[str, int]:
    if response.status_code != 200:
        raise OciRemoteError(f"OCI download returned HTTP {response.status_code}")
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise OciRemoteError("OCI download has unsupported content encoding")
    digest = sha256()
    size = 0
    while chunk := response.raw.read(min(_CHUNK, limit - size + 1), decode_content=False):
        size += len(chunk)
        if size > limit:
            raise OciRemoteError("OCI download exceeds its byte limit")
        digest.update(chunk)
        sink.write(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _check_manifest_header(response: Response, digest: str) -> None:
    if response.headers.get("Docker-Content-Digest", digest) != digest:
        raise OciRemoteError("OCI manifest digest header mismatch")


class OciRemote:
    """One online command's registry client and verified repository reference."""

    def __init__(self, reference: OciReference, auth_backend: AuthBackend = "token") -> None:
        try:
            from oras.container import Container
            from oras.provider import Registry
        except ImportError:
            raise OciRemoteError("OCI registry support requires ORAS") from None

        self._reference = reference
        with _translate_failure("registry setup"):
            self._container = Container(
                reference.repository_with_selector, registry=reference.authority
            )
            if (
                self._container.registry != reference.authority
                or self._container.api_prefix != reference.repository
                or self._container.uri != str(reference).removeprefix("oci://")
            ):
                raise OciRemoteError("ORAS changed the OCI reference")
            self._registry = Registry(
                hostname=reference.authority,
                insecure=False,
                tls_verify=True,
                auth_backend=auth_backend,
            )
            self._registry.auth.load_configs(self._container)
            self._registry.set_header("Accept-Encoding", "identity")
        self._manifest_url = f"https://{self._container.manifest_url()}"

    def push(self, manifest: BundleManifest, layer_path: Path) -> str:
        """Publish a locally prepared archive and return the exact manifest digest."""
        from oras.oci import NewLayer

        if self._reference.is_digest:
            raise OciRemoteError("OCI push requires a tag")
        with _translate_failure("push"), TemporaryDirectory() as directory:
            if layer_path.stat().st_size > MAX_LAYER_BYTES:
                raise OciRemoteError("OCI layer exceeds its byte limit")
            config_path = Path(directory) / "config.json"
            config_path.write_bytes(encode_oci_config(manifest))
            config = NewLayer(str(config_path), media_type=CONFIG_MEDIA_TYPE)
            layer = NewLayer(str(layer_path), media_type=LAYER_MEDIA_TYPE)
            raw = encode_oci_manifest(
                OciDescriptor.model_validate(config), OciDescriptor.model_validate(layer)
            )
            digest = f"sha256:{sha256(raw).hexdigest()}"
            # A preliminary HEAD can cache a pull-only token in ORAS 0.2.44.
            for path, descriptor in ((config_path, config), (layer_path, layer)):
                with self._registry.put_upload(str(path), self._container, descriptor) as response:
                    if response.status_code != 201:
                        raise OciRemoteError(
                            f"OCI blob upload returned HTTP {response.status_code}"
                        )
            with self._registry.do_request(
                self._manifest_url,
                "PUT",
                data=raw,
                headers={"Content-Type": MANIFEST_MEDIA_TYPE},
            ) as response:
                if response.status_code != 201:
                    raise OciRemoteError(
                        f"OCI manifest upload returned HTTP {response.status_code}"
                    )
                _check_manifest_header(response, digest)
        return digest

    def _download_blob(self, descriptor: OciDescriptor, sink: BinaryIO) -> None:
        with self._registry.get_blob(self._container, descriptor.digest, stream=True) as response:
            digest, size = _stream(response, sink, descriptor.size)
        if size != descriptor.size or digest != descriptor.digest:
            raise OciRemoteError("OCI blob does not match its descriptor")

    @contextmanager
    def pull(self) -> Iterator[PulledBundle]:
        """Download and verify M/C/L before giving the caller a temporary layer."""
        with TemporaryDirectory() as directory:
            with _translate_failure("pull"):
                with (
                    BytesIO() as manifest_bytes,
                    self._registry.do_request(
                        self._manifest_url,
                        "GET",
                        headers={
                            "Accept": MANIFEST_MEDIA_TYPE,
                            "Accept-Encoding": "identity",
                        },
                        stream=True,
                    ) as response,
                ):
                    digest, _ = _stream(response, manifest_bytes, MAX_MANIFEST_BYTES)
                    _check_manifest_header(response, digest)
                    if self._reference.is_digest and self._reference.selector != digest:
                        raise OciRemoteError("OCI manifest does not match the requested digest")
                    manifest = parse_oci_manifest(manifest_bytes.getvalue())
                with BytesIO() as config_bytes:
                    self._download_blob(manifest.config, config_bytes)
                    config = parse_oci_config(config_bytes.getvalue())
                layer_path = Path(directory) / "layer.tar.gz"
                with layer_path.open("wb") as layer_file:
                    self._download_blob(manifest.layers[0], layer_file)
            yield PulledBundle(digest, config, layer_path)
