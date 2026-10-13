"""Stateful fake Docker CLI used by DockerRunnerBackend contract tests."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"volumes": {}, "containers": {}, "volume_creates": 0}


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def _labels(argv: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for index, value in enumerate(argv[:-1]):
        if value == "--label":
            key, label_value = argv[index + 1].split("=", 1)
            labels[key] = label_value
    return labels


def _option(argv: list[str], name: str) -> str | None:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _record(log_path: Path, argv: list[str], stdin: bytes) -> None:
    record = {
        "argv": argv,
        "env": dict(os.environ),
        "stdin": stdin.decode("utf-8", errors="replace"),
    }
    if argv and argv[0] == "create" and "--env-file" in argv:
        path = _option(argv, "--env-file")
        assert path is not None
        record["env_file_content"] = Path(path).read_text(encoding="utf-8")
        record["env_file_mode"] = Path(path).stat().st_mode & 0o777
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def main() -> int:
    """Execute one canned Docker command against a JSON state file."""
    argv = sys.argv[1:]
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    log_path = Path(os.environ["FAKE_DOCKER_LOG"])
    state_path = Path(os.environ["FAKE_DOCKER_STATE"])
    _record(log_path, argv, stdin)
    state = _load(state_path)
    scenario = os.environ.get("FAKE_DOCKER_SCENARIO", "")

    delay_command = os.environ.get("FAKE_DOCKER_DELAY_COMMAND")
    if delay_command and delay_command in " ".join(argv):
        marker = os.environ.get("FAKE_DOCKER_DELAY_MARKER")
        if marker:
            Path(marker).write_text("started", encoding="utf-8")
        time.sleep(float(os.environ.get("FAKE_DOCKER_DELAY", "30")))

    if argv[:2] == ["image", "inspect"]:
        if scenario == "pull" or scenario == "pullfail":
            return 1
        print("{}")
        return 0
    if argv and argv[0] == "pull":
        if scenario == "pullfail":
            print("registry unavailable", file=sys.stderr)
            return 1
        print("pulled")
        return 0
    if argv[:2] == ["volume", "create"]:
        name = argv[-1]
        state["volumes"][name] = _labels(argv)
        state["volume_creates"] += 1
        _save(state_path, state)
        print(name)
        return 0
    if argv[:2] == ["volume", "inspect"]:
        if scenario == "inspect-error":
            print("Cannot connect to the Docker daemon", file=sys.stderr)
            return 1
        if scenario == "inspect-no-such-host":
            print("lookup docker.example: no such host", file=sys.stderr)
            return 1
        if scenario == "inspect-malformed":
            print("{invalid")
            return 0
        if scenario == "inspect-nondict":
            print("null")
            return 0
        name = argv[-1]
        labels = state["volumes"].get(name)
        if labels is None:
            print("no such volume", file=sys.stderr)
            return 1
        print(json.dumps(labels))
        return 0
    if argv[:3] == ["volume", "rm", "-f"]:
        state["volumes"].pop(argv[-1], None)
        _save(state_path, state)
        return 0
    if argv and argv[0] == "create":
        if scenario == "createfail":
            print("create failed", file=sys.stderr)
            return 1
        name = _option(argv, "--name")
        assert name is not None
        state["containers"][name] = {
            "Name": f"/{name}",
            "Config": {"Labels": _labels(argv)},
            "State": {"ExitCode": int(os.environ.get("FAKE_DOCKER_EXIT_CODE", "0"))},
        }
        _save(state_path, state)
        print(f"cid-{name}")
        return 0
    if argv and argv[0] == "cp":
        if scenario == "cpfail":
            print("copy failed", file=sys.stderr)
            return 1
        return 0
    if argv and argv[0] == "start":
        if scenario == "startfail":
            print("working directory does not exist", file=sys.stderr)
            return 1
        # Canned output goes out as raw UTF-8 bytes, mirroring a real
        # container's byte stream: a text-mode write would translate newlines
        # (\n -> \r\n) and fail on characters the pipe's encoding cannot
        # represent (cp1252 on Windows, e.g. U+FFFD).
        sys.stdout.buffer.write(os.environ.get("FAKE_DOCKER_STDOUT", "command output").encode())
        sys.stderr.buffer.write(os.environ.get("FAKE_DOCKER_STDERR", "").encode())
        return int(os.environ.get("FAKE_DOCKER_START_RC", "0"))
    if argv and argv[0] == "exec":
        if "--health" in argv:
            if scenario == "realm-no-bridge":
                print("python: No module named conductor.runner.bridge", file=sys.stderr)
                return 1
            if scenario == "realm-daemon-error":
                print("Cannot connect to the Docker daemon", file=sys.stderr)
                return 1
            features = os.environ.get("FAKE_DOCKER_FEATURES", "interrupt")
            print(
                json.dumps(
                    {
                        "ready": True,
                        "protocol_version": int(
                            os.environ.get("FAKE_DOCKER_PROTOCOL_VERSION", "2")
                        ),
                        "features": features.split(",") if features else [],
                    }
                )
            )
            return 0
        if "--interrupt" in argv:
            release_port = os.environ.get("FAKE_DOCKER_RELEASE_PORT")
            if release_port:
                with socket.create_connection(("127.0.0.1", int(release_port))) as release:
                    release.sendall(b"I")
            print('{"accepted":true}')
            return 0
        request = json.loads(stdin)
        if scenario == "realm-malformed":
            sys.stdout.buffer.write(b"{not-json}\n")
            sys.stdout.buffer.flush()
            return 0
        if scenario == "realm-error":
            sys.stdout.buffer.write(
                json.dumps({"type": "error", "data": {"message": "secret-overlay"}}).encode()
                + b"\n"
            )
            sys.stdout.buffer.flush()
            return 0
        event = {"type": "agent_message", "data": {"text": "streamed"}}
        sys.stdout.buffer.write(json.dumps(event).encode() + b"\n")
        sys.stdout.buffer.flush()
        if scenario == "realm-truncated":
            return 0
        release_port = os.environ.get("FAKE_DOCKER_RELEASE_PORT")
        release_byte = b""
        if release_port:
            with socket.create_connection(("127.0.0.1", int(release_port))) as release:
                release_byte = release.recv(1)
        result = {
            "type": "result",
            "data": {
                "content": {"answer": request["agent"]["name"]},
                "partial": release_byte == b"I",
            },
        }
        sys.stdout.buffer.write(json.dumps(result).encode() + b"\n")
        sys.stdout.buffer.flush()
        return 0
    if argv and argv[0] == "inspect":
        name = argv[-1]
        data = state["containers"].get(name)
        if data is None:
            print("no such container", file=sys.stderr)
            return 1
        if "{{json .State}}" in argv:
            print(json.dumps(data["State"]))
        elif "{{json .Config.Labels}}" in argv:
            print(json.dumps(data["Config"]["Labels"]))
        else:
            print(json.dumps(data))
        return 0
    if argv[:2] == ["ps", "-aq"]:
        for name in state["containers"]:
            print(name)
        return 0
    if argv and argv[0] == "kill":
        if scenario == "termination-fails":
            print("kill denied", file=sys.stderr)
            return 1
        return 0
    if argv[:2] == ["rm", "-f"]:
        if scenario == "termination-fails":
            print("remove denied", file=sys.stderr)
            return 1
        state["containers"].pop(argv[-1], None)
        _save(state_path, state)
        return 0
    print(f"unsupported fake docker argv: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
