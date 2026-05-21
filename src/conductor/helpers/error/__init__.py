"""Engine-agnostic helpers for raising typed Conductor error envelopes.

This subpackage ships *optional* convenience modules for the five
script engines Conductor most commonly executes:

* PowerShell (``Conductor.Error.psm1``)
* Bash / sh  (``conductor-error.sh``)
* Python     (``conductor_error.py``)
* Node       (``conductor-error.mjs``)
* .NET       (``ConductorError.cs``)

All of them write the same JSON envelope to ``$CONDUCTOR_ERROR_OUT``
and exit ``0``. They exist so the common path reads naturally; nothing
in Conductor *requires* them — a script that wants to opt out of the
helper layer can write the JSON itself.

The Python helper is also importable directly (``from
conductor.helpers.error import conductor_error``) for in-process use
from agent prompts that shell out, but its primary purpose is to be
shipped to user scripts as a reference implementation.
"""

from __future__ import annotations
