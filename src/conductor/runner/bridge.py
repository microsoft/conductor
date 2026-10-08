"""Pipe an in-container loopback runner over ``docker exec -i``.

The bridge runs in the container network namespace. Its stdout is only the
runner response; the host never exposes or publishes a runner port.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

_HEALTH_DEADLINE = 15.0


def main() -> int:
    """Forward one health, execution, or targeted interrupt request."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--health", action="store_true")
    operation.add_argument("--interrupt", metavar="EXECUTION_ID")
    args = parser.parse_args()
    base_url = f"http://127.0.0.1:{args.port}"
    opener = build_opener(ProxyHandler({}))
    if args.health:
        deadline = time.monotonic() + _HEALTH_DEADLINE
        while True:
            try:
                with opener.open(f"{base_url}/health", timeout=2) as response:
                    sys.stdout.buffer.write(response.read(65536))
                return 0
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    print("runner health endpoint did not become ready", file=sys.stderr)
                    return 1
                time.sleep(0.1)

    body = (
        json.dumps({"execution_id": args.interrupt}).encode("utf-8")
        if args.interrupt is not None
        else sys.stdin.buffer.read()
    )
    route = "/interrupt" if args.interrupt is not None else "/execute"
    request = Request(
        f"{base_url}{route}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=10 if args.interrupt is not None else None) as response:
            if args.interrupt is not None:
                sys.stdout.buffer.write(response.read(65536))
            else:
                for line in response:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
        return 0
    except HTTPError as exc:
        if args.interrupt is not None:
            print(f"runner interrupt HTTP {exc.code}", file=sys.stderr)
            return 1
        try:
            payload = json.loads(exc.read(65536))
            message = payload["error"]["message"]
            if not isinstance(message, str):
                raise ValueError("invalid error message")
        except (ValueError, KeyError, TypeError):
            message = f"runner HTTP {exc.code}"
        # The host redacts credential-bearing messages before raising them.
        sys.stdout.buffer.write(
            json.dumps({"type": "error", "data": {"message": message}}).encode("utf-8") + b"\n"
        )
        sys.stdout.buffer.flush()
        return 0
    except URLError as exc:
        print(f"runner connection failed: {type(exc.reason).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
