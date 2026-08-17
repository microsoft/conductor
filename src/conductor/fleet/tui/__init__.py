"""Fleet Manager Textual TUI (``conductor fleet``, Fleet Manager E7+).

Imported only by :mod:`conductor.cli.fleet`'s bare-invocation callback, and
only after that callback has confirmed ``textual`` is installed (the ``tui``
optional extra) — this package (and everything under it) is free to import
``textual`` unconditionally at module scope, since nothing here is ever
imported unless the caller has already verified the dependency is present.

See ``docs/projects/fleet-manager/fleet-manager.design.md`` for the full
design: the Runs screen (home), drill-down screens reached via Textual's
``Screen`` push/pop stack, and a ~2s polled refresh model (no file watcher).
"""

from __future__ import annotations
