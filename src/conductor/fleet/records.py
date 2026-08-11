"""Run record read/write/prune primitives (Fleet Manager E1 — Run record core).

Every ``conductor run`` invocation (foreground, foreground-with-dashboard, or
``--web-bg``) writes a small JSON record here so that ``conductor stop`` and a
future ``conductor fleet`` TUI can discover it — see the design's *The fix*
section in ``docs/projects/fleet-manager/fleet-manager.design.md``.

Design points this module implements:

- **Keyed by ``run_id``, not port.** Foreground runs have no port, and port
  was already a poor key (stale, colliding ``.pid`` files).
- **Nine fields**, exactly: ``run_id``, ``pid``, ``workflow_path``,
  ``workflow_name``, ``started_at``, ``event_log_path``, ``port`` (nullable),
  ``mode`` (``fg`` / ``fg-web`` / ``bg``), ``checkpoint_dir``.
- **Atomic writes** (temp file in the same directory + ``os.replace``) so a
  reader never observes a partially-written record.
- **Tolerant readers.** ``read_run_records()`` mirrors the existing posture of
  ``cli.pid.read_pid_files()``: a corrupt, vanished, or unparseable file is
  removed rather than raised. ``read_run_record()`` (the single-key lookup)
  is stricter about *not* deleting on a parse failure, since a concurrent
  atomic write is the expected reason a read might transiently fail.
- **Liveness** delegates to :func:`conductor.cli.pid.is_process_alive` — the
  one process-probe implementation in the repo, already hardened for
  Windows footguns (issues #166, #344). This module does not reimplement it.
- **Legacy tolerance.** Pre-upgrade ``--web-bg`` runs left port-keyed
  ``*.pid`` files (via ``cli.pid.write_pid_file``) under the *default*
  ``cli.pid.pid_dir()`` (``~/.conductor/runs/``, which does **not** honor
  ``CONDUCTOR_HOME``). ``read_run_records()`` reads those too, surfacing them
  as ``RunRecord``s with ``mode="bg"`` (so D1's stop-confirmation gate never
  prompts for them) and best-effort field mapping rather than dropping or
  crashing on the unfamiliar shape.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conductor.cli import pid as cli_pid

logger = logging.getLogger(__name__)

_RUN_RECORDS_DIR_NAME = "runs"

_VALID_MODES = frozenset({"fg", "fg-web", "bg"})

# Path-safe run-id pattern. Real run_ids are ``secrets.token_hex(4)`` (see
# ``engine/event_log.py``) — 8 lowercase hex characters — but this pattern is
# intentionally broader (alphanumeric, ``-``, ``_``) so it doesn't need to
# track that format precisely. ``run_id`` is interpolated directly into a
# filename (``<run_id>.json``), so it must be validated before being used in
# a path: critically, ``.`` is excluded from the safe set, which rules out
# both ``..`` traversal and a value like ``"foo/../../etc"``; ``/`` and
# ``\`` are excluded outright, which rules out absolute paths and separators.
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def is_valid_run_id(run_id: str) -> bool:
    """Return True if ``run_id`` is safe to use as a run-record filename component.

    This is the single, authoritative "effective run-id" contract: the
    broadest format any run_id — freshly generated, or reused verbatim from
    a checkpoint by a resumed run (``EventLogSubscriber``'s
    ``existing_path``/``existing_run_id`` branch performs no format check of
    its own) — must satisfy for :func:`write_run_record` /
    :func:`read_run_record` to actually accept it.

    Exported (rather than kept as the private ``_RUN_ID_PATTERN`` regex) so
    ``cli.bg_runner._peek_resume_run_id`` can predict, using this exact same
    rule, whether the resumed child will successfully write a run record
    under a checkpoint's ``run_id`` — instead of maintaining its own,
    narrower hex-only copy that could (and did) reject a checkpoint
    ``run_id`` the child itself would happily reuse, causing the parent to
    poll for the wrong key and kill a healthy resumed run.

    Args:
        run_id: The run identifier to check.

    Returns:
        True if ``run_id`` matches the path-safe pattern used to key run
        record files.
    """
    return bool(_RUN_ID_PATTERN.fullmatch(run_id))


def _max_pid() -> int:
    """Largest value the platform's liveness probe can accept as a ``pid``.

    Checked dynamically (rather than baked into a module-level constant at
    import time) so tests can exercise both branches by patching
    ``conductor.fleet.records.sys.platform``, mirroring the dispatch
    convention already used by ``cli.pid.is_process_alive``.

    On POSIX, ``os.kill`` parses its ``pid`` argument as a signed 32-bit C
    ``int``, so ``2**31 - 1`` is the largest value it accepts before raising
    ``OverflowError``. On Windows, ``is_process_alive`` passes the value to
    ``OpenProcess`` via a ctypes wrapper typed ``wintypes.DWORD`` (unsigned
    32-bit), so a Windows PID can legally range up to ``2**32 - 1`` --
    bounding it at the POSIX ceiling there would wrongly reject a real (if
    unlikely) high-numbered Windows PID. Real PIDs never approach either
    bound in practice (Linux's own hard cap is far lower), so these are
    generous safety bounds, not realistic ceilings.
    """
    return 2**32 - 1 if sys.platform == "win32" else 2**31 - 1


def _validate_run_id(run_id: str) -> None:
    """Raise ``ValueError`` if ``run_id`` is not safe to use in a filename.

    Args:
        run_id: The run identifier to validate.

    Raises:
        ValueError: If ``run_id`` doesn't match the path-safe pattern (e.g.
            contains path separators or traversal sequences like ``".."``).
    """
    if not is_valid_run_id(run_id):
        raise ValueError(f"Invalid run_id (must be path-safe): {run_id!r}")


def _coerce_pid(value: Any) -> int:
    """Coerce a parsed JSON value into a safe, positive process ID.

    Rejects booleans (``bool`` is a subclass of ``int`` in Python),
    non-integral floats, out-of-range floats that would overflow ``int()``
    (e.g. ``1e10000``), non-numeric types, non-positive values, and integers
    outside the range a real PID can take on the current platform (see
    :func:`_max_pid`) — all of which would otherwise be handed to
    ``os.kill`` / ``OpenProcess`` via ``is_process_alive``, or used to gate
    a foreground-stop confirmation.

    The upper bound matters even for a plain (non-overflowing) Python
    ``int``: ``os.kill`` on POSIX parses its ``pid`` argument as a C
    ``int``, so a JSON payload with e.g. ``"pid": 8589934592`` parses fine
    as a Python integer but raises an uncaught ``OverflowError`` the moment
    it reaches ``os.kill`` — well before ``is_process_alive`` gets a chance
    to catch ``OSError``. Bounding it here, at parse time, keeps every
    downstream caller (including the tolerant bulk reader) from ever
    handing such a value to a process-signaling API.

    Raises:
        ValueError: If ``value`` is not a safe, positive integral PID
            within the platform's PID range (see :func:`_max_pid`).
    """
    if isinstance(value, bool):
        raise ValueError("pid must not be a boolean")
    if isinstance(value, int):
        pid = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("pid must be an integral value")
        try:
            pid = int(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("pid is out of range") from exc
    else:
        raise ValueError(f"pid must be an int, got {type(value).__name__}")
    if pid <= 0:
        raise ValueError("pid must be a positive integer")
    max_pid = _max_pid()
    if pid > max_pid:
        raise ValueError(f"pid is out of range (must be <= {max_pid})")
    return pid


def _coerce_optional_str(value: Any, field: str) -> str:
    """Coerce an optional string field, defaulting a missing (``None``) value to ``""``.

    Deliberately does *not* treat every falsy value as "missing": an
    explicitly-provided ``run_id: []`` or ``workflow_path: false`` is a
    malformed payload, not an omitted field, and must be rejected rather
    than silently collapsed to ``""``. Only ``None`` (absent key, or an
    explicit JSON ``null``) defaults; any other non-string is an error.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    """Return the value of the first key in ``keys`` that is present in ``data``.

    Unlike ``data.get(a) or data.get(b)``, this does not fall through to an
    alias key when the primary key is present but holds an explicitly
    invalid falsy value (e.g. ``False``, ``[]``, ``0``) — that would
    silently hide the invalid value behind the alias's value instead of
    surfacing it as a validation error. Returns ``None`` if none of
    ``keys`` are present at all.
    """
    for key in keys:
        if key in data:
            return data[key]
    return None


def _coerce_optional_str_or_none(value: Any, field: str) -> str | None:
    """Coerce a nullable string field (e.g. ``checkpoint_dir``), keeping ``None`` as-is."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _coerce_optional_int(value: Any, field: str) -> int | None:
    """Coerce a nullable integer field (e.g. ``port``), keeping ``None`` as-is."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int or null")
    return value


def _coerce_mode(data: dict[str, Any]) -> str:
    """Coerce the ``mode`` field, defaulting a genuinely *missing* key to ``"bg"``.

    Legacy records (which never had a ``mode`` key at all) are always
    treated as a background process — D1 relies on this to never prompt for
    one. Distinguishing "key absent" from "key present with value ``None``"
    matters here: an explicit ``"mode": null`` is a malformed payload (this
    field is written as a plain string, never null, by ``write_run_record``)
    and must be rejected like any other explicit-but-invalid value, rather
    than silently defaulting to ``"bg"`` the same way a truly missing key
    does.

    Raises:
        ValueError: If ``mode`` is present but not one of ``"fg"``,
            ``"fg-web"``, or ``"bg"``.
    """
    if "mode" not in data:
        return "bg"
    value = data["mode"]
    if not isinstance(value, str) or value not in _VALID_MODES:
        raise ValueError(f"invalid mode: {value!r}")
    return value


@dataclass(frozen=True)
class RunRecord:
    """A single run's discovery record.

    Carries exactly the nine fields listed in the Fleet Manager design's
    *The fix* section — no more (D4 explicitly rejected a tenth ``tty``
    field).

    Attributes:
        run_id: Unique run identifier (from ``EventLogSubscriber``). Empty
            string for a legacy port-keyed ``.pid`` record that predates
            this field.
        pid: Process ID of the run.
        workflow_path: Path to the workflow YAML file, as given on the CLI.
        workflow_name: The workflow file's stem (e.g. ``"my-workflow"``).
        started_at: ISO 8601 timestamp of when the run started.
        event_log_path: Path to the run's JSONL event log.
        port: TCP port the web dashboard is listening on, or ``None`` for a
            run with no dashboard (``mode == "fg"``).
        mode: One of ``"fg"``, ``"fg-web"``, or ``"bg"``.
        checkpoint_dir: The (global, ``$TMPDIR``-rooted) checkpoints
            directory, or ``None`` if unknown.
    """

    run_id: str
    pid: int
    workflow_path: str
    workflow_name: str
    started_at: str
    event_log_path: str
    port: int | None
    mode: str
    checkpoint_dir: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "workflow_path": self.workflow_path,
            "workflow_name": self.workflow_name,
            "started_at": self.started_at,
            "event_log_path": self.event_log_path,
            "port": self.port,
            "mode": self.mode,
            "checkpoint_dir": self.checkpoint_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        """Build a :class:`RunRecord` from a parsed JSON payload.

        Tolerant of the legacy port-keyed ``.pid`` shape written by
        ``cli.pid.write_pid_file`` (keys ``pid``, ``port``, ``workflow``,
        ``started_at``, ``run_id``, ``log_file``), which lacks ``mode`` and
        ``checkpoint_dir`` entirely and may carry an empty ``run_id``. Only
        ``pid`` is required; every other field defaults/optionalizes so a
        legacy or partially-populated record still parses.

        Every field is also type- and range-checked: ``pid`` must be a
        positive integral value (not a bool, zero, negative, or a
        non-integral/overflowing float), ``mode`` must be one of ``"fg"`` /
        ``"fg-web"`` / ``"bg"`` when present, and the string/int-or-null
        fields must actually be strings/ints. These values go on to drive
        process signaling (``pid``) and foreground-stop confirmation
        (``mode``), so a malformed or attacker-influenced value is rejected
        here rather than silently accepted.

        Raises:
            ValueError: If ``pid`` is missing or unsafe, ``mode`` is present
                but not a recognized value, or any other field has the
                wrong type.
        """
        pid_raw = data.get("pid")
        if pid_raw is None:
            raise ValueError("run record is missing required field 'pid'")
        pid = _coerce_pid(pid_raw)

        # Legacy `.pid` files use "workflow" / "log_file" instead of
        # "workflow_path" / "event_log_path". `_first_present` (rather than
        # `data.get(a) or data.get(b)`) makes sure an explicitly-invalid
        # primary value (e.g. `workflow_path: false`) is rejected instead of
        # silently falling through to the alias.
        workflow_path = _coerce_optional_str(
            _first_present(data, "workflow_path", "workflow"), "workflow_path"
        )
        workflow_name = _coerce_optional_str(data.get("workflow_name"), "workflow_name") or (
            Path(workflow_path).stem if workflow_path else ""
        )
        event_log_path = _coerce_optional_str(
            _first_present(data, "event_log_path", "log_file"), "event_log_path"
        )

        return cls(
            run_id=_coerce_optional_str(data.get("run_id"), "run_id"),
            pid=pid,
            workflow_path=workflow_path,
            workflow_name=workflow_name,
            started_at=_coerce_optional_str(data.get("started_at"), "started_at"),
            event_log_path=event_log_path,
            port=_coerce_optional_int(data.get("port"), "port"),
            mode=_coerce_mode(data),
            checkpoint_dir=_coerce_optional_str_or_none(
                data.get("checkpoint_dir"), "checkpoint_dir"
            ),
        )


def run_records_dir() -> Path:
    """Return the directory used for run records, creating it if needed.

    Respects the ``CONDUCTOR_HOME`` environment variable (mirroring
    ``registry/config.py::get_config_path``) so tests and isolated
    environments can redirect it. Note this deliberately differs from
    ``cli.pid.pid_dir()``, which does not honor ``CONDUCTOR_HOME`` — legacy
    ``.pid`` files are read from that unredirected location explicitly (see
    :func:`read_run_records`).

    Returns:
        Path to ``$CONDUCTOR_HOME/runs/`` or ``~/.conductor/runs/``.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    d = base / _RUN_RECORDS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_run_record(record: RunRecord) -> Path:
    """Atomically write ``record`` to ``<run_records_dir>/<run_id>.json``.

    Writes to a temp file in the same directory first, then ``os.replace``s
    it into place, so a concurrent reader never observes a partially-written
    file (readers may still race the *absence* of the file, which is fine —
    see :func:`read_run_record`).

    Args:
        record: The run record to persist.

    Returns:
        Path to the written record file.

    Raises:
        ValueError: If ``record.run_id`` is not a path-safe run id.
    """
    _validate_run_id(record.run_id)
    d = run_records_dir()
    filepath = d / f"{record.run_id}.json"

    fd, tmp_name = tempfile.mkstemp(prefix=f".{record.run_id}.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record.to_dict(), f, indent=2)
        os.replace(tmp_name, filepath)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure. `os.replace`
        # either fully completed (in which case tmp_name no longer exists)
        # or didn't happen at all, so partial content never becomes visible
        # under the real filename either way.
        Path(tmp_name).unlink(missing_ok=True)
        raise

    logger.debug("Wrote run record: %s", filepath)
    return filepath


def _safe_unlink(f: Path) -> bool:
    """Best-effort delete of ``f``, never raising.

    Args:
        f: Path to delete.

    Returns:
        True if this call's ``unlink()`` actually removed the file. False if
        the file was already absent, or an ``OSError`` (permission denied,
        read-only filesystem, etc.) prevented removal — the latter is logged
        but never raised, since a bulk scan (:func:`read_run_records`) must
        never crash on one bad file, and a caller reporting deletion status
        must not claim success for a removal that didn't happen.
    """
    try:
        f.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not remove run record file: %s", f, exc_info=True)
        return False
    return True


def _restore_if_absent(src: Path, dst: Path) -> None:
    """Best-effort, non-clobbering restore of the quarantined file ``src`` back to ``dst``.

    Uses ``os.link`` rather than ``os.replace`` for the restoration step:
    ``os.link`` atomically fails with ``FileExistsError`` when ``dst``
    already exists, so a fresh record concurrently written to ``dst`` (by a
    writer racing this quarantine-then-restore sequence) is never clobbered
    by restoring stale/corrupt content back over it. This is the no-clobber
    counterpart to :func:`_delete_if_unchanged`'s own use of ``os.rename``'s
    atomicity: neither operation needs a separate ``stat()``-then-act pair
    that a concurrent writer could slip through the middle of.

    If ``dst`` already exists, the quarantined copy at ``src`` is simply
    discarded (best-effort) -- something newer has legitimately taken its
    place in the interim and the quarantine copy is no longer needed. If
    ``src`` itself is already gone (e.g. the caller's own earlier ``stat()``
    of it failed), this is a silent no-op -- there is nothing to restore.
    Any other failure (permission denied, read-only filesystem, etc.) is
    logged and ``src`` is left in place; a subsequent scan may retry.

    Never raises.

    Args:
        src: The quarantined file to restore (or discard).
        dst: The original path to restore it to, iff still absent.
    """
    try:
        os.link(src, dst)
    except FileNotFoundError:
        # `src` no longer exists -- nothing to restore.
        return
    except FileExistsError:
        # `dst` already holds a newer record written since `src` was
        # quarantined -- the quarantined copy is superseded; discard it so
        # it doesn't linger as an orphaned `.prune-*` artifact.
        _safe_unlink(src)
        return
    except OSError:
        logger.warning(
            "Could not restore quarantined run record to %s; leaving %s in place",
            dst,
            src,
            exc_info=True,
        )
        return
    # `src` and `dst` now both point at the same inode (two names for one
    # file) -- drop the quarantine name so only the canonical path remains.
    _safe_unlink(src)


def _delete_if_unchanged(f: Path, stat_before: os.stat_result | None) -> bool:
    """Atomically delete ``f`` iff its on-disk identity still matches ``stat_before``.

    Guards against a read-then-delete race: a reader may load a stale
    (dead-``pid``) or corrupt record, and *before* it acts on that, a
    ``resume`` (or any other writer) can atomically replace the same
    ``run_id`` file via ``write_run_record``'s ``os.replace``. A naive
    "``stat()`` again, compare, then ``unlink()``" only narrows this race —
    it leaves a window between the confirming ``stat()`` and the ``unlink()``
    call itself where a concurrent replacement can still land, silently
    deleting the writer's brand-new, live record instead of the stale one
    that was actually read.

    There is no atomic "unlink-if-inode-matches" syscall exposed to Python,
    so this closes the gap using ``os.rename``'s own atomicity instead of a
    second, separate check: ``f`` is unconditionally moved to a private
    quarantine path in the same directory first (a single, indivisible
    filesystem operation — a concurrent ``os.replace(tmp, f)`` either
    completes fully before this rename or not at all; there is no
    in-between state either side can observe). Only *after* the move do we
    inspect what was actually captured:

    - If its identity matches ``stat_before``, it genuinely was the
      stale/corrupt file that was read — unlink the quarantined copy.
    - If it doesn't match, a concurrent writer's replacement won the race
      for the original path (this call's rename captured the *new* file
      instead of the one it intended to remove) — restore it, but only if
      nothing has been written back to ``f`` since (see
      :func:`_restore_if_absent`): a *second* writer could land in the gap
      between this function's own rename and the restoration attempt, and
      an unconditional ``os.replace`` restoration would clobber that
      second writer's file with the (older) quarantined one. Using a
      non-clobbering restore instead means the newest writer always wins,
      no matter how many races stack up.

    Args:
        f: Path to the record file to (conditionally) delete.
        stat_before: The ``stat()`` result captured just before the
            (stale/corrupt) content was read, or ``None`` if no such
            snapshot is available (in which case this is a no-op).

    Returns:
        True if ``f`` was actually removed by this call. False if it was
        already gone, a concurrent replacement was detected and restored
        (or superseded by a still-newer replacement, in which case the
        quarantined copy is simply discarded), or the final removal itself
        failed (e.g. permission denied) — in the last two cases the
        original content is put back at its original path (when nothing
        newer has since taken its place) so it isn't silently lost as an
        orphaned quarantine file.
    """
    if stat_before is None:
        return False

    quarantine = f.with_name(f".{f.name}.prune-{uuid.uuid4().hex}")
    try:
        os.rename(f, quarantine)
    except OSError:
        return False  # Already gone -- nothing to prune.

    try:
        stat_now = quarantine.stat()
    except OSError:
        # Vanished (or otherwise un-stat-able) between the rename we just
        # did and this stat. A best-effort, non-clobbering restore handles
        # both sub-cases without crashing a tolerant scan: if the
        # quarantine file is genuinely gone this is a silent no-op, and if
        # it merely couldn't be stat'd (but still exists) it is restored
        # rather than left behind as an orphaned `.prune-*` artifact.
        _restore_if_absent(quarantine, f)
        return False

    if (stat_now.st_ino, stat_now.st_dev) != (stat_before.st_ino, stat_before.st_dev):
        logger.debug(
            "Restoring %s: replaced concurrently since it was read (captured via %s)",
            f,
            quarantine,
        )
        _restore_if_absent(quarantine, f)
        return False

    if _safe_unlink(quarantine):
        return True

    # The final unlink itself failed (e.g. permission denied, read-only
    # filesystem). Restore the file to its original path rather than
    # leaving an orphaned `.prune-*` artifact around -- the next scan will
    # retry deletion from scratch. Non-clobbering: a concurrent writer may
    # already have created a fresh record at `f` in the gap between the
    # quarantine rename above and this restoration attempt.
    _restore_if_absent(quarantine, f)
    return False


def _load_record_file(f: Path) -> tuple[RunRecord | None, bool, os.stat_result | None]:
    """Read and parse a single record file.

    ``stat`` is captured *before* the read (rather than atomically via an
    open file descriptor) so the file's identity snapshot is available even
    when the read itself fails — this is what lets a caller later delete the
    file only if it is unchanged (see :func:`_delete_if_unchanged`). The
    stat-then-read ordering leaves a narrow window (a concurrent replace
    landing between the two calls) that a single-descriptor read would
    close entirely, but it keeps this function's I/O calls independently
    mockable, which the existing test suite relies on.

    Returns:
        A ``(record, corrupt, stat)`` tuple. ``record`` is the parsed
        :class:`RunRecord`, or ``None`` if it couldn't be produced.
        ``corrupt`` is only meaningful when ``record`` is ``None``: ``True``
        means the file's *content* is unrecoverable (malformed JSON, not a
        JSON object, invalid UTF-8, missing the required ``pid`` field, or a
        field with an unsafe/wrong-typed value) and safe to delete;
        ``False`` means the file could not even be *read* (vanished
        mid-scan, a permission error, or another transient I/O error) — in
        that case the file must be left alone rather than treated as
        corrupt, since a transient read failure is not evidence the record
        is bad. ``stat`` is the pre-read snapshot described above, or
        ``None`` when the file had already vanished before it could even be
        stat'd.
    """
    try:
        stat_before = f.stat()
    except OSError:
        # Vanished before we could even stat it — nothing to prune.
        return None, False, None

    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Vanished mid-scan (e.g. removed by its own process exiting).
        # Nothing to delete — the file is already gone.
        return None, False, None
    except UnicodeDecodeError:
        # Bytes on disk aren't valid UTF-8 -- genuinely corrupt content, not
        # a transient I/O issue.
        return None, True, stat_before
    except OSError:
        # Permission denied or other transient I/O error. Leave the file in
        # place; a corrupt read must not delete a record that may well
        # belong to a still-running process.
        logger.warning("Could not read run record file: %s", f, exc_info=True)
        return None, False, None

    try:
        data = json.loads(text)
    except ValueError:
        # `json.JSONDecodeError` (a `ValueError` subclass) covers malformed
        # or truncated JSON from a non-atomic legacy write. A syntactically
        # valid but absurdly large integer literal (e.g. a corrupted/
        # attacker-influenced `"pid"` with thousands of digits) also raises
        # a plain `ValueError` here — CPython's integer-string conversion
        # guard (PEP hardening for CVE-2020-10735) rejects it during
        # `json.loads` itself, before `RunRecord.from_dict` ever runs. Both
        # cases are genuinely corrupt content, not a transient I/O issue.
        return None, True, stat_before
    except RecursionError:
        # A deeply nested malformed payload (e.g. thousands of unclosed
        # `[` / `{`) exhausts the interpreter's recursion limit inside the
        # (recursive-descent) JSON decoder before it can raise its usual
        # `JSONDecodeError`. `RecursionError` is a `RuntimeError` subclass,
        # not a `ValueError`, so it needs its own arm here -- but it's the
        # same class of problem (unrecoverable content), not a transient
        # I/O issue, so it's classified as corrupt too.
        return None, True, stat_before

    if not isinstance(data, dict):
        return None, True, stat_before

    try:
        return RunRecord.from_dict(data), False, stat_before
    except (ValueError, TypeError):
        return None, True, stat_before


def _read_and_prune(files: list[Path], *, require_run_id_match: bool = False) -> list[RunRecord]:
    """Parse ``files`` as run records, pruning stale/corrupt ones.

    A file whose *content* fails to parse is deleted (treated as
    corrupt/unrecoverable). A file that parses but whose ``pid`` is no
    longer alive is deleted (a stale record from a process that exited
    without cleaning up after itself, e.g. ``kill -9``). A file that could
    not be *read* at all (vanished mid-scan or a transient I/O error) is
    left alone. Every deletion goes through :func:`_delete_if_unchanged`,
    which performs the identity check and the removal as a single atomic
    rename rather than a separate ``stat()``-then-``unlink()`` pair — this
    closes (not just narrows) the window where a concurrent writer (e.g. a
    `resume` re-writing the same `run_id`) replaces the file between this
    function reading stale/corrupt content and deciding to delete it, which
    would otherwise destroy the new, live record instead of the stale one
    that was actually read. Deletions themselves are best-effort — an
    ``OSError`` while unlinking never escapes this function. All of this
    mirrors the existing posture of ``cli.pid.read_pid_files()``.

    Args:
        files: Candidate record files to parse.
        require_run_id_match: When ``True`` (used for this module's own
            ``<run_id>.json`` files, never for legacy ``.pid`` files), a
            parsed record whose ``run_id`` doesn't exactly equal the
            file's own stem is treated as corrupt and pruned rather than
            surfaced — a payload must not be allowed to claim an identity
            other than the one it was filed under.
    """
    results: list[RunRecord] = []
    for f in files:
        record, corrupt, stat_before = _load_record_file(f)
        if record is None:
            if corrupt:
                logger.debug("Removing corrupt run record: %s", f)
                _delete_if_unchanged(f, stat_before)
            continue
        if require_run_id_match and record.run_id != f.stem:
            logger.debug(
                "Removing run record with mismatched identity: %s (payload run_id=%r)",
                f,
                record.run_id,
            )
            _delete_if_unchanged(f, stat_before)
            continue
        if cli_pid.is_process_alive(record.pid):
            results.append(record)
        else:
            logger.debug("Cleaning up stale run record: %s (PID %s)", f, record.pid)
            _delete_if_unchanged(f, stat_before)
    return results


def scan_run_records() -> list[RunRecord]:
    """Read every run record **without modifying anything on disk**.

    :func:`read_run_records` is the maintenance path: it prunes stale and
    corrupt records as it goes, which is right for ``stop`` and wrong for
    anything whose contract is to observe. A reader that deletes turns a
    diagnostic command into the one that loses the run it was asked about,
    so read-only callers (``conductor status``) use this instead — the same
    split ``cli.pid`` draws between ``scan_pid_files`` and
    ``read_pid_files``.

    Like that pair, this still filters to live processes (a dead run is not
    "running") and still tolerates corrupt, vanished, unreadable, or
    legacy-shaped files by skipping them: one bad file must not take down
    the listing of every other run.

    Returns:
        List of :class:`RunRecord` for every run whose process is confirmed
        alive, in filename order, merging ``run_id``-keyed records with
        legacy port-keyed ``.pid`` files.
    """
    results: list[RunRecord] = []

    # Sorted rather than raw glob order: the listing is user-facing, and
    # ``Path.glob`` order is filesystem-dependent.
    for f in sorted(run_records_dir().glob("*.json")):
        record, _corrupt, _stat = _load_record_file(f)
        if record is None:
            continue
        if record.run_id != f.stem:
            # Same identity guard read_run_records applies, minus the delete.
            continue
        if cli_pid.is_process_alive(record.pid):
            results.append(record)

    for f in sorted(cli_pid.pid_dir().glob("*.pid")):
        record, _corrupt, _stat = _load_record_file(f)
        if record is None:
            continue
        if record.port is None:
            # A *legacy* .pid file always recorded a port -- it was the file's
            # own key. One without a port is malformed, not a foreground run,
            # so it is skipped here exactly as ``cli.pid.scan_pid_files``
            # skips it. (``port=None`` is only meaningful for a modern ``fg``
            # run record, which lives in the .json branch above.)
            logger.warning("Skipping legacy PID file without a port: %s", f)
            continue
        if cli_pid.is_process_alive(record.pid):
            results.append(record)

    return results


def read_run_records() -> list[RunRecord]:
    """Return run records for every process that is still alive.

    Combines two sources:

    - Every ``*.json`` file in :func:`run_records_dir` (the
      ``CONDUCTOR_HOME``-aware location this module writes to).
    - Every legacy ``*.pid`` file in ``cli.pid.pid_dir()`` (always
      ``~/.conductor/runs/``, ignoring ``CONDUCTOR_HOME`` — see that
      function's docstring), surfaced as ``RunRecord``s with ``mode="bg"``.

    Stale records (dead ``pid``) and corrupt/unparseable files are pruned
    from disk as a side effect, matching the tolerant posture of
    ``cli.pid.read_pid_files()``. This function never raises for a corrupt,
    vanished, unreadable, or legacy-shaped file.

    Returns:
        List of :class:`RunRecord` for every run whose process is confirmed
        alive.
    """
    results = _read_and_prune(list(run_records_dir().glob("*.json")), require_run_id_match=True)
    results += _read_and_prune(list(cli_pid.pid_dir().glob("*.pid")))
    return results


def read_run_record(run_id: str) -> RunRecord | None:
    """Return the single run record keyed by ``run_id``.

    Unlike :func:`read_run_records`, this never deletes anything: a
    concurrent atomic write (temp file + ``os.replace``) is the expected
    reason a read might transiently fail to find or parse the file, not
    corruption. This is the primitive a parent-side launch gate polls while
    waiting for its child to report in (D2).

    Args:
        run_id: The run identifier to look up.

    Returns:
        The parsed :class:`RunRecord`, or ``None`` if ``run_id`` isn't a
        path-safe run id, the record is absent, it could not (yet) be
        parsed, or the parsed payload's own ``run_id`` field doesn't
        exactly equal the requested key (guards against a record written
        under one name that claims to be another, including a payload with
        a missing/empty ``run_id`` — since ``run_id`` here is always a
        non-empty path-safe string, that can never match by coincidence).
        Does not check liveness — a caller that just wrote the record for
        its own process wouldn't want a liveness race to hide it.
    """
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        return None
    filepath = run_records_dir() / f"{run_id}.json"
    record, _corrupt, _stat = _load_record_file(filepath)
    if record is None:
        return None
    if record.run_id != run_id:
        return None
    return record


def remove_run_record(run_id: str) -> bool:
    """Remove the run record file for ``run_id``, if it exists.

    Args:
        run_id: The run identifier to remove.

    Returns:
        True only if a record file existed and this call actually removed
        it. False otherwise — this includes ``run_id`` values that aren't
        path-safe (which never have a corresponding file by construction,
        see :func:`write_run_record`), a ``run_id`` with no on-disk record,
        and a removal that was attempted but failed (e.g. permission
        denied) or lost a race to a concurrent deletion. A prior
        ``.exists()`` check followed by a separate ``unlink()`` call would
        itself be a check-then-act race (the file could vanish in between)
        and would also report success even when the removal failed — both
        are avoided by treating ``_safe_unlink``'s own return value as the
        single source of truth for whether removal occurred.
    """
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        return False
    filepath = run_records_dir() / f"{run_id}.json"
    removed = _safe_unlink(filepath)
    if removed:
        logger.debug("Removed run record: %s", filepath)
    return removed


def remove_run_record_for_current_process() -> bool:
    """Find and remove the run record matching the current process.

    Mirrors the shape of ``cli.pid.remove_pid_file_for_current_process()``:
    called by a run on exit to clean up its own record, matching by ``pid``
    rather than requiring the caller to have retained its own ``run_id``.

    Returns:
        True only if a matching record file existed and this call actually
        removed it. False otherwise, including when a match was found but
        the removal itself was suppressed by :func:`_delete_if_unchanged`
        (a concurrent replacement was detected and restored, or the
        underlying unlink failed) — the record was not actually removed in
        that case, so this must not report success.
    """
    current_pid = os.getpid()
    d = run_records_dir()

    for f in d.glob("*.json"):
        record, _corrupt, stat_before = _load_record_file(f)
        if record is not None and record.pid == current_pid:
            removed = _delete_if_unchanged(f, stat_before)
            if removed:
                logger.debug("Removed run record for current process (PID %s): %s", current_pid, f)
            return removed
    return False
