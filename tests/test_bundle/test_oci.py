"""Contract tests for the OCI RunBundle v1 wire boundary."""

import json
from collections.abc import Callable

import pytest

from conductor.bundle import oci
from conductor.bundle.model import BundleEntry, BundleManifest, compute_bundle_digest

DIGEST = "sha256:" + "a" * 64
ROOT = "tree/project/real.yaml"
LINK = "tree/project/link.yaml"


@pytest.mark.parametrize(
    ("authored", "canonical"),
    [
        ("oci://LOCALHOST:5443/project/run:Release", "oci://localhost:5443/project/run:Release"),
        (f"oci://EXAMPLE.org:443/run@{DIGEST}", f"oci://example.org/run@{DIGEST}"),
        ("oci://127.0.0.1/run:v1", "oci://127.0.0.1/run:v1"),
        ("oci://[2001:0DB8::1]:5443/run:_A", "oci://[2001:db8::1]:5443/run:_A"),
    ],
)
def test_reference_round_trip(authored: str, canonical: str) -> None:
    # Canonicalization preserves an explicit, case-sensitive selector.
    ref = oci.parse_oci_reference(authored)
    assert str(ref) == canonical
    assert ref == oci.parse_oci_reference(canonical)
    assert ref.is_digest == ("@" in canonical)


@pytest.mark.parametrize(
    "value",
    [
        "localhost/run:latest",
        "oci:///run:latest",
        "oci://localhost/run",
        "oci://user@localhost/run:v1",
        "oci://localhost/run:v1?query",
        "oci://localhost/run:v1#anchor",
        "oci://localhost/Run:v1",
        "oci://localhost/a//run:v1",
        "oci://localhost:0/run:v1",
        "oci://localhost:65536/run:v1",
        "oci://127.0.0.999/run:v1",
        "oci://[abcd]/run:v1",
        "oci://-example.org/run:v1",
        f"oci://localhost/run:v1@{DIGEST}",
        f"oci://localhost/run@sha256:{'A' * 64}",
    ],
)
def test_invalid_reference(value: str) -> None:
    # Refuse ambiguous refs instead of falling back to Docker defaults.
    with pytest.raises(oci.OciFormatError):
        oci.parse_oci_reference(value)


@pytest.fixture
def bundle_manifest() -> BundleManifest:
    entries = (
        BundleEntry(
            logical_path=ROOT,
            kind="file",
            digest=DIGEST,
            size=4,
            executable=False,
            link_target=None,
            origin_kind="workflow",
            origin_detail="workflow:root",
        ),
        BundleEntry.for_symlink(
            LINK, "real.yaml", origin_kind="workflow", origin_detail="workflow:root"
        ),
    )
    return BundleManifest(
        version=1,
        entries=entries,
        bundle_digest=compute_bundle_digest(entries, {}, {}),
        skills_topology={},
        plugins_topology={},
    )


def test_config_round_trip(bundle_manifest: BundleManifest) -> None:
    # A collector-style root symlink cannot displace the regular entrypoint.
    config = oci.parse_oci_config(oci.encode_oci_config(bundle_manifest))
    assert config.entrypoint == ROOT
    assert config.bundle_digest == bundle_manifest.bundle_digest
    assert config.manifest.entries == bundle_manifest.entries[::-1]


def test_config_rejects_permutation_preserving_digest(bundle_manifest: BundleManifest) -> None:
    # B alone cannot detect a noncanonical ordering on the wire.
    payload = json.loads(oci.encode_oci_config(bundle_manifest))
    payload["manifest"]["entries"].reverse()
    with pytest.raises(oci.OciFormatError):
        oci.parse_oci_config(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(entrypoint=LINK),
        lambda p: p.update(bundle_digest=DIGEST),
        lambda p: p["manifest"].update(version=1.0),
        lambda p: p["manifest"].update(bundle_digest=DIGEST),
        lambda p: p["manifest"]["entries"][0].update(size="0"),
        lambda p: p["manifest"]["entries"][1].update(executable=1),
        lambda p: p["manifest"]["entries"].insert(0, p["manifest"]["entries"][0]),
    ],
)
def test_invalid_config(
    bundle_manifest: BundleManifest, change: Callable[[dict[str, object]], None]
) -> None:
    # Reject malformed config without coercing types or trusting its asserted B.
    payload = json.loads(oci.encode_oci_config(bundle_manifest))
    change(payload)
    with pytest.raises(oci.OciFormatError):
        oci.parse_oci_config(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("subject", "field", "value", "reason"),
    [
        (0, "link_target", "é" * 513, "UTF-8 byte limit"),
        (1, "logical_path", "tree/z" + "é" * 510, "UTF-8 byte limit"),
        (1, "size", oci.MAX_PAYLOAD_BYTES + 1, "payload exceeds its byte limit"),
    ],
)
def test_config_entry_limits(
    bundle_manifest: BundleManifest, subject: int, field: str, value: object, reason: str
) -> None:
    # Reject oversized entry metadata or declared payload before computing B.
    payload = json.loads(oci.encode_oci_config(bundle_manifest))
    payload["manifest"]["entries"][subject][field] = value
    with pytest.raises(oci.OciFormatError) as error:
        oci.parse_oci_config(json.dumps(payload).encode())
    assert reason in str(error.value.__cause__)


@pytest.mark.parametrize(
    ("extra_entries", "path_pattern", "reason"),
    [
        # Reject 10,001 declared entries even though they occupy only 10,003 nodes.
        pytest.param(10_000, "tree/z{:05d}.yaml", "entry count", id="declared-entries"),
        # Count implicit directories: 8,334 entries occupy 50,001 nodes.
        pytest.param(8_333, "tree/z{:05d}/a/b/c/d/file.yaml", "node limit", id="implicit-nodes"),
    ],
)
def test_config_rejects_count_limit(extra_entries: int, path_pattern: str, reason: str) -> None:
    root = BundleEntry(
        logical_path=ROOT,
        kind="file",
        digest=DIGEST,
        size=4,
        executable=False,
        link_target=None,
        origin_kind="workflow",
        origin_detail="workflow:root",
    )
    entries = (root,) + tuple(
        root.model_copy(
            update={"logical_path": path_pattern.format(index), "origin_detail": "workflow:include"}
        )
        for index in range(extra_entries)
    )
    digest = compute_bundle_digest(entries, {}, {})
    manifest = BundleManifest(
        version=1, entries=entries, bundle_digest=digest, skills_topology={}, plugins_topology={}
    )
    raw = json.dumps(
        {
            "version": 1,
            "bundle_digest": digest,
            "entrypoint": ROOT,
            "manifest": manifest.model_dump(),
        }
    ).encode()

    with pytest.raises(oci.OciFormatError) as error:
        oci.parse_oci_config(raw)
    assert reason in str(error.value.__cause__)


def test_config_rejects_second_regular_root(bundle_manifest: BundleManifest) -> None:
    # A second regular root is ambiguous even when the authored entrypoint exists.
    payload = json.loads(oci.encode_oci_config(bundle_manifest))
    entries = payload["manifest"]["entries"]
    entries.append({**entries[1], "logical_path": "tree/project/second.yaml"})
    with pytest.raises(oci.OciFormatError) as error:
        oci.parse_oci_config(json.dumps(payload).encode())
    assert "exactly one regular root" in str(error.value.__cause__)


def test_duplicate_nested_key(bundle_manifest: BundleManifest) -> None:
    # Equal duplicate keys are ambiguous even when the parsed values would agree.
    raw = oci.encode_oci_config(bundle_manifest).replace(b'"size":4', b'"size":4,"size":4')
    with pytest.raises(oci.OciFormatError, match="Duplicate JSON"):
        oci.parse_oci_config(raw)


@pytest.fixture
def descriptors() -> tuple[oci.OciDescriptor, oci.OciDescriptor]:
    return (
        oci.OciDescriptor(mediaType=oci.CONFIG_MEDIA_TYPE, digest=DIGEST, size=250),
        oci.OciDescriptor(mediaType=oci.LAYER_MEDIA_TYPE, digest=DIGEST, size=500),
    )


def test_manifest_round_trip(descriptors: tuple[oci.OciDescriptor, oci.OciDescriptor]) -> None:
    # The manifest deterministically retains the two differently typed descriptors.
    config, layer = descriptors
    raw = oci.encode_oci_manifest(config, layer)
    manifest = oci.parse_oci_manifest(raw + b"\n")
    assert manifest.config == config
    assert manifest.layers == (layer,)
    assert oci.encode_oci_manifest(manifest.config, manifest.layers[0]) == raw


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schemaVersion=1),
        lambda p: p.update(schemaVersion=2.0),
        lambda p: p.update(mediaType="application/json"),
        lambda p: p.update(artifactType="application/json"),
        lambda p: p["config"].update(size=True),
        lambda p: p["config"].update(size=-1),
        lambda p: p["config"].update(size=oci.MAX_CONFIG_BYTES + 1),
        lambda p: p["config"].update(digest=DIGEST.upper()),
        lambda p: p["config"].update(urls=[]),
        lambda p: p["config"].update(data="AAAA"),
        lambda p: p.update(layers=[]),
        lambda p: p["layers"].append(p["layers"][0]),
        lambda p: p["layers"][0].update(mediaType=oci.CONFIG_MEDIA_TYPE),
        lambda p: p["layers"][0].update(size=oci.MAX_LAYER_BYTES + 1),
    ],
)
def test_invalid_manifest(
    descriptors: tuple[oci.OciDescriptor, oci.OciDescriptor],
    change: Callable[[dict[str, object]], None],
) -> None:
    # Reject unsupported schemas, layer counts and descriptors at the JSON boundary.
    payload = json.loads(oci.encode_oci_manifest(*descriptors))
    change(payload)
    with pytest.raises(oci.OciFormatError):
        oci.parse_oci_manifest(json.dumps(payload).encode())


def test_json_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Apply document limits before trying to decode invalid bytes.
    monkeypatch.setattr(oci, "MAX_MANIFEST_BYTES", 2)
    monkeypatch.setattr(oci, "MAX_CONFIG_BYTES", 2)
    for parse in (oci.parse_oci_config, oci.parse_oci_manifest):
        with pytest.raises(oci.OciFormatError, match="byte limit"):
            parse(b"\xff" * 3)
