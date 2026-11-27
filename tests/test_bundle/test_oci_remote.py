"""Registry transport tests without network or credential access."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from traceback import format_exception

import pytest
from oras.container import Container
from oras.provider import Registry
from requests import Response
from urllib3.response import HTTPResponse

from conductor.bundle import oci, oci_remote
from conductor.bundle.model import BundleEntry, BundleManifest, compute_bundle_digest

REF = "oci://localhost:5443/team/run:Release"
LAYER = b"fixture layer representation"


def digest(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


def response(data: bytes = b"", status: int = 200) -> Response:
    result = Response()
    result.status_code = status
    result.raw = HTTPResponse(body=BytesIO(data), preload_content=False)
    return result


@pytest.fixture
def manifest() -> BundleManifest:
    entries = (
        BundleEntry(
            logical_path="tree/workflow.yaml",
            kind="file",
            digest=digest(b""),
            size=0,
            executable=False,
            link_target=None,
            origin_kind="workflow",
            origin_detail="workflow:root",
        ),
    )
    return BundleManifest(
        version=1,
        entries=entries,
        bundle_digest=compute_bundle_digest(entries, {}, {}),
        skills_topology={},
        plugins_topology={},
    )


@pytest.fixture(autouse=True)
def isolate_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Unexpected SDK network or credential access")

    monkeypatch.setattr("requests.sessions.Session.request", unexpected)
    monkeypatch.setattr("oras.auth.TokenAuth.load_configs", lambda *_: None)


@pytest.mark.parametrize(
    ("status", "match"), [(201, ""), (403, "HTTP 403"), (201, "header mismatch")]
)
def test_push_uses_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest: BundleManifest,
    status: int,
    match: str,
) -> None:
    # Push uploads both blobs directly and publishes the exact manifest only on success.
    uploads: list[tuple[Path, bytes, str]] = []
    manifests: list[bytes] = []

    def put_upload(
        _: Registry, path: str, container: Container, descriptor: dict[str, str | int]
    ) -> Response:
        assert container.uri == REF.removeprefix("oci://")
        file = Path(path)
        body = file.read_bytes()
        assert descriptor["size"] == len(body)
        assert descriptor["digest"] == digest(body)
        uploads.append((file, body, str(descriptor["mediaType"])))
        return response(status=status)

    def put_manifest(
        _: Registry, url: str, method: str, *, data: bytes, headers: dict[str, str]
    ) -> Response:
        assert method == "PUT"
        assert url == "https://localhost:5443/v2/team/run/manifests/Release"
        assert headers == {"Content-Type": oci.MANIFEST_MEDIA_TYPE}
        manifests.append(data)
        result = response(status=201)
        result.headers["Docker-Content-Digest"] = digest(b"stale" if match else data)
        return result

    monkeypatch.setattr(Registry, "put_upload", put_upload)
    monkeypatch.setattr(Registry, "do_request", put_manifest)
    archive = tmp_path / "layer.tar.gz"
    archive.write_bytes(LAYER)
    remote = oci_remote.OciRemote(oci.parse_oci_reference(REF))
    if match:
        with pytest.raises(oci_remote.OciRemoteError, match=match):
            remote.push(manifest, archive)
        assert len(uploads) == (1 if status == 403 else 2)
        assert len(manifests) == (0 if status == 403 else 1)
    else:
        assert remote.push(manifest, archive) == digest(manifests[0])
        assert [(body, media) for _, body, media in uploads] == [
            (oci.encode_oci_config(manifest), oci.CONFIG_MEDIA_TYPE),
            (LAYER, oci.LAYER_MEDIA_TYPE),
        ]
        wire = oci.parse_oci_manifest(manifests[0])
        assert wire.config.digest == digest(uploads[0][1])
        assert wire.layers[0].digest == digest(LAYER)
    assert not uploads[0][0].exists()
    assert archive.read_bytes() == LAYER


@pytest.mark.parametrize(
    ("fault", "message", "blob_requests"),
    [
        ("none", "", 2),
        ("header", "header mismatch", 0),
        ("selector", "requested digest", 0),
        ("encoding", "content encoding", 0),
        ("config", "does not match", 1),
        ("layer", "does not match", 2),
        ("overflow", "byte limit", 2),
        ("exception", "pull failed", 0),
    ],
)
def test_pull_verifies_wire_before_handoff(
    monkeypatch: pytest.MonkeyPatch,
    manifest: BundleManifest,
    fault: str,
    message: str,
    blob_requests: int,
) -> None:
    # Only verified original bytes may reach the caller, regardless of header claims.
    config = oci.encode_oci_config(manifest)
    wire = (
        b" "
        + oci.encode_oci_manifest(
            oci.OciDescriptor(
                mediaType=oci.CONFIG_MEDIA_TYPE, digest=digest(config), size=len(config)
            ),
            oci.OciDescriptor(
                mediaType=oci.LAYER_MEDIA_TYPE, digest=digest(LAYER), size=len(LAYER)
            ),
        )
        + b"\n"
    )
    fetched: list[Response] = []
    selectors: list[str] = []
    blobs: list[str] = []
    ref = oci.parse_oci_reference(
        f"{REF.removesuffix(':Release')}@{digest(b'stale')}" if fault == "selector" else REF
    )

    def get_manifest(
        _: Registry, url: str, method: str, *, headers: dict[str, str], stream: bool
    ) -> Response:
        assert method == "GET" and stream
        assert headers == {"Accept": oci.MANIFEST_MEDIA_TYPE, "Accept-Encoding": "identity"}
        selectors.append(url.rsplit("/", 1)[-1])
        if fault == "exception":
            raise OSError("synthetic secret")
        result = response(wire)
        result.headers["Docker-Content-Digest"] = digest(b"stale" if fault == "header" else wire)
        if fault == "encoding":
            result.headers["Content-Encoding"] = "gzip"
        fetched.append(result)
        return result

    def get_blob(_: Registry, container: Container, key: str, *, stream: bool) -> Response:
        assert stream and container.uri == REF.removeprefix("oci://")
        blobs.append(key)
        if key == digest(config):
            result = response(b"x" + config[1:] if fault == "config" else config)
        else:
            assert key == digest(LAYER)
            body = b"x" + LAYER[1:] if fault == "layer" else LAYER
            result = response(body + b"x" if fault == "overflow" else body)
            result.headers["Content-Length"] = str(len(LAYER))
        fetched.append(result)
        return result

    monkeypatch.setattr(Registry, "do_request", get_manifest)
    monkeypatch.setattr(Registry, "get_blob", get_blob)
    remote = oci_remote.OciRemote(ref)
    if message:
        with pytest.raises(oci_remote.OciRemoteError, match=message) as error, remote.pull():
            pytest.fail("An invalid pull reached the consumer")
        assert "synthetic secret" not in "".join(format_exception(error.value))
    else:
        with remote.pull() as bundle:
            assert bundle.manifest_digest == digest(wire)
            assert bundle.config.bundle_digest == manifest.bundle_digest
            assert bundle.layer_path.read_bytes() == LAYER
        assert not bundle.layer_path.exists()
    assert selectors == [ref.selector]
    assert len(blobs) == blob_requests
    assert all(item.raw.closed for item in fetched)


def test_sdk_rewrite_is_refused_before_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    # A repository changed by ORAS must not reach credential loading.
    original = Container.parse

    def rewrite(container: Container, name: str) -> None:
        original(container, name)
        container.repository = "changed"

    def unexpected_auth(*_: object) -> None:
        pytest.fail("Reference rewrite reached credential loading")

    monkeypatch.setattr(Container, "parse", rewrite)
    monkeypatch.setattr("oras.auth.TokenAuth.load_configs", unexpected_auth)
    with pytest.raises(oci_remote.OciRemoteError, match="changed the OCI reference"):
        oci_remote.OciRemote(oci.parse_oci_reference(REF))
