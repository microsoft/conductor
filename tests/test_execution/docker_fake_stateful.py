"""Stateful Docker CLI fake with disk-backed volumes across backend instances."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def labels(argv: list[str]) -> dict[str, str]:
    return dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "--label")


@contextmanager
def store_lock(path: Path) -> Iterator[None]:
    """Serialize the fake daemon's read-modify-write cycles across CLI processes."""
    with path.open("a+b") as lock_file:
        if sys.platform == "win32":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main() -> int:
    store = Path(os.environ["FAKE_DOCKER_STORE"])
    store.parent.mkdir(parents=True, exist_ok=True)
    with store_lock(store.with_name(store.name + ".lock")):
        return dispatch(store)


def dispatch(store: Path) -> int:
    argv = sys.argv[1:]
    volumes = store / "volumes"
    volumes.mkdir(parents=True, exist_ok=True)
    containers_path = store / "containers.json"
    containers = json.loads(containers_path.read_text()) if containers_path.exists() else {}
    with Path(os.environ["FAKE_DOCKER_LOG"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")

    if argv[:2] == ["image", "inspect"]:
        print("{}")
        return 0
    if argv[:2] == ["volume", "inspect"]:
        path = volumes / argv[-1] / "labels.json"
        if not path.exists():
            print("no such volume", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0
    if argv[:2] == ["volume", "create"]:
        volume = volumes / argv[-1]
        if not volume.exists():
            volume.mkdir()
            (volume / "labels.json").write_text(json.dumps(labels(argv)), encoding="utf-8")
        print(argv[-1])
        return 0
    if argv[:3] == ["volume", "rm", "-f"]:
        shutil.rmtree(volumes / argv[-1], ignore_errors=True)
        return 0
    if argv[0] == "create":
        name = option(argv, "--name")
        volume = option(argv, "-v").split(":", 1)[0]
        if os.environ.get("FAKE_DOCKER_DROP_ON_EXEC_CREATE") and "--env-file" in argv:
            shutil.rmtree(volumes / volume, ignore_errors=True)
        # Docker creates an unlabeled volume when -v names one that vanished.
        if not (volumes / volume).exists():
            (volumes / volume).mkdir()
            (volumes / volume / "labels.json").write_text("{}", encoding="utf-8")
        containers[name] = {"volume": volume, "labels": labels(argv)}
        containers_path.write_text(json.dumps(containers), encoding="utf-8")
        print(name)
        return 0
    if argv[0] == "cp":
        source, target = argv[-2:]
        if source.startswith("conductor-"):
            failure = store / "fail-probe-once"
            if failure.exists():
                failure.unlink()
                print("marker probe failed", file=sys.stderr)
                return 1
            name, relative = source.split(":", 1)
            file = volumes / containers[name]["volume"] / relative.removeprefix("/workspace/")
            if not file.exists():
                print("no such file", file=sys.stderr)
                return 1
            Path(target).write_bytes(file.read_bytes())
            return 0
        name, relative = target.split(":", 1)
        volume = volumes / containers[name]["volume"]
        if source.endswith("/."):
            failure = store / "fail-tree-once"
            if failure.exists():
                failure.unlink()
                print("tree copy failed", file=sys.stderr)
                return 1
            shutil.copytree(source[:-2], volume, dirs_exist_ok=True, symlinks=True)
            # The tree arrives separately from its commit marker.
            (volume / "tree-copied").write_text("yes", encoding="utf-8")
        else:
            failure = store / "fail-marker-once"
            if failure.exists():
                failure.unlink()
                print("marker copy failed", file=sys.stderr)
                return 1
            (volume / relative.removeprefix("/workspace/")).write_bytes(Path(source).read_bytes())
        return 0
    if argv[0] == "start":
        print("command output")
        return 0
    if argv[0] == "inspect":
        name = argv[-1]
        if name not in containers:
            print("no such container", file=sys.stderr)
            return 1
        data = {
            "Name": "/" + name,
            "Config": {"Labels": containers[name]["labels"]},
            "State": {"ExitCode": 0},
        }
        if "{{json .State}}" in argv:
            print(json.dumps(data["State"]))
        elif "{{json .Config.Labels}}" in argv:
            print(json.dumps(containers[name]["labels"]))
        else:
            print(json.dumps(data))
        return 0
    if argv[:2] == ["ps", "-aq"]:
        print("\n".join(containers))
        return 0
    if argv[:2] == ["rm", "-f"]:
        containers.pop(argv[-1], None)
        containers_path.write_text(json.dumps(containers), encoding="utf-8")
        return 0
    print(f"unsupported fake Docker command: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
