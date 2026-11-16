"""A real forwarding parent for native peer ownership controls."""

from __future__ import annotations

import subprocess
import sys

if __name__ == "__main__":
    with subprocess.Popen([sys.executable, *sys.argv[1:]]) as child:
        try:
            raise SystemExit(child.wait(timeout=20))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise
