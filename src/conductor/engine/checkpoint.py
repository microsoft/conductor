"""Checkpoint management for workflow resume after failure.

This module provides the CheckpointManager class for saving, loading,
listing, and cleaning up workflow checkpoint files. Checkpoints capture
workflow state on failure so execution can be resumed later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.exceptions import CheckpointError

logger = logging.getLogger(__name__)


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types to strings.

    Handles bytes, datetime, Path, and arbitrary objects by falling
    back to ``str()``. Used by ``save_checkpoint()`` to prevent
    serialization failures from masking the original error.

    Args:
        obj: The object to convert.

    Returns:
        A JSON-serializable equivalent of *obj*.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes len={len(obj)}>"
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    if isinstance(obj, set):
        return [_make_json_serializable(item) for item in sorted(obj, key=str)]
    # Fallback for any other type
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


@dataclass
class CheckpointData:
    """Parsed checkpoint file contents.

    Attributes:
        version: Checkpoint format version.
        workflow_path: Absolute path to the workflow YAML file.
        workflow_hash: SHA-256 hash of the workflow file (``sha256:<hex>``).
        created_at: ISO-8601 timestamp of checkpoint creation.
        failure: Failure metadata (error_type, message, agent, iteration).
        inputs: Workflow inputs at the time of failure.
        current_agent: Name of the agent that was executing when failure occurred.
        context: Serialized ``WorkflowContext`` state.
        limits: Serialized ``LimitEnforcer`` state.
        copilot_session_ids: Mapping of agent names to Copilot session IDs.
        file_path: Path where the checkpoint file is stored.
    """

    version: int
    workflow_path: str
    workflow_hash: str
    created_at: str
    failure: dict[str, Any]
    inputs: dict[str, Any]
    current_agent: str
    context: dict[str, Any]
    limits: dict[str, Any]
    copilot_session_ids: dict[str, str] = field(default_factory=dict)
    file_path: Path = field(default_factory=lambda: Path())


class CheckpointManager:
    """Manages checkpoint file I/O for workflow resume.

    All methods are static — the manager carries no instance state.
    Checkpoint files are written to ``$TMPDIR/conductor/checkpoints/``.
    """

    CHECKPOINT_VERSION = 1

    @staticmethod
    def get_checkpoints_dir() -> Path:
        """Return the checkpoints directory, creating it if needed.

        Returns:
            Path to ``$TMPDIR/conductor/checkpoints/``.
        """
        path = Path(tempfile.gettempdir()) / "conductor" / "checkpoints"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def compute_workflow_hash(workflow_path: Path) -> str:
        """Compute SHA-256 hash of a workflow file.

        Args:
            workflow_path: Path to the workflow YAML file.

        Returns:
            Hash string in the format ``sha256:<hex_digest>``.
        """
        content = workflow_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def save_checkpoint(
        workflow_path: Path,
        context: WorkflowContext,
        limits: LimitEnforcer,
        current_agent: str,
        error: BaseException,
        inputs: dict[str, Any],
        copilot_session_ids: dict[str, str] | None = None,
    ) -> Path | None:
        """Serialize workflow state to a checkpoint file.

        Writes the checkpoint atomically (write to ``.tmp``, then rename)
        and sets file permissions to ``0o600``.

        This method **never raises** — on failure it logs a warning and
        returns ``None`` so the original error is not masked.

        Args:
            workflow_path: Path to the workflow YAML file.
            context: Current workflow context.
            limits: Current limit enforcer state.
            current_agent: Name of the agent executing when the error occurred.
            error: The exception that triggered the checkpoint.
            inputs: Workflow inputs.
            copilot_session_ids: Optional mapping of agent names to session IDs.

        Returns:
            Path to the saved checkpoint file, or ``None`` if saving failed.
        """
        try:
            checkpoints_dir = CheckpointManager.get_checkpoints_dir()

            # Compute workflow hash
            try:
                workflow_hash = CheckpointManager.compute_workflow_hash(workflow_path)
            except OSError:
                workflow_hash = "sha256:unknown"

            # Build checkpoint dict
            import secrets

            timestamp = time.strftime("%Y%m%d-%H%M%S")
            # Append random suffix to avoid filename collisions
            # when multiple runs start in the same second
            suffix = secrets.token_hex(4)
            timestamp = f"{timestamp}-{suffix}"
            created_at = datetime.now(UTC).isoformat()
            workflow_name = workflow_path.stem

            checkpoint = {
                "version": CheckpointManager.CHECKPOINT_VERSION,
                "workflow_path": str(workflow_path.resolve()),
                "workflow_hash": workflow_hash,
                "created_at": created_at,
                "failure": {
                    "error_type": type(error).__name__,
                    "message": str(error).split("\n")[0],
                    "agent": current_agent,
                    "iteration": limits.current_iteration,
                },
                "inputs": _make_json_serializable(inputs),
                "current_agent": current_agent,
                "context": _make_json_serializable(context.to_dict()),
                "limits": _make_json_serializable(limits.to_dict()),
                "copilot_session_ids": copilot_session_ids or {},
            }

            # Serialize to JSON
            json_data = json.dumps(checkpoint, indent=2)

            # Write atomically: .tmp then rename
            checkpoint_path = checkpoints_dir / f"{workflow_name}-{timestamp}.json"
            tmp_path = checkpoint_path.with_suffix(".tmp")

            tmp_path.write_text(json_data, encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            tmp_path.rename(checkpoint_path)

            # Warn if checkpoint is large
            size_bytes = checkpoint_path.stat().st_size
            if size_bytes > 10 * 1024 * 1024:  # 10MB
                logger.warning(
                    "Checkpoint file is large (%d MB): %s",
                    size_bytes // (1024 * 1024),
                    checkpoint_path,
                )

            return checkpoint_path

        except Exception:
            logger.warning("Failed to save checkpoint", exc_info=True)
            return None

    @staticmethod
    def load_checkpoint(checkpoint_path: Path) -> CheckpointData:
        """Load and validate a checkpoint file.

        Args:
            checkpoint_path: Path to the checkpoint JSON file.

        Returns:
            Parsed ``CheckpointData``.

        Raises:
            CheckpointError: If the file is not found, contains invalid JSON,
                or has an unsupported version.
        """
        if not checkpoint_path.exists():
            raise CheckpointError(
                f"Checkpoint file not found: {checkpoint_path}",
                suggestion="Check the file path and try again",
                checkpoint_path=str(checkpoint_path),
            )

        try:
            raw = checkpoint_path.read_text(encoding="utf-8")
        except OSError as e:
            raise CheckpointError(
                f"Cannot read checkpoint file: {e}",
                checkpoint_path=str(checkpoint_path),
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CheckpointError(
                f"Invalid JSON in checkpoint file: {e}",
                suggestion="The checkpoint file may be corrupted",
                checkpoint_path=str(checkpoint_path),
            ) from e

        # Validate version
        version = data.get("version")
        if version is None:
            raise CheckpointError(
                "Checkpoint file missing 'version' field",
                suggestion="This file may not be a valid Conductor checkpoint",
                checkpoint_path=str(checkpoint_path),
            )
        if version != CheckpointManager.CHECKPOINT_VERSION:
            raise CheckpointError(
                f"Unsupported checkpoint version: {version} "
                f"(expected {CheckpointManager.CHECKPOINT_VERSION})",
                suggestion=(
                    "This checkpoint was created by a different version of Conductor. "
                    "Re-run the workflow to create a new checkpoint."
                ),
                checkpoint_path=str(checkpoint_path),
            )

        # Validate required fields
        required_fields = [
            "workflow_path",
            "workflow_hash",
            "created_at",
            "failure",
            "current_agent",
            "context",
            "limits",
        ]
        for field_name in required_fields:
            if field_name not in data:
                raise CheckpointError(
                    f"Checkpoint file missing required field: '{field_name}'",
                    suggestion="The checkpoint file may be corrupted or incomplete",
                    checkpoint_path=str(checkpoint_path),
                )

        return CheckpointData(
            version=data["version"],
            workflow_path=data["workflow_path"],
            workflow_hash=data["workflow_hash"],
            created_at=data["created_at"],
            failure=data["failure"],
            inputs=data.get("inputs", {}),
            current_agent=data["current_agent"],
            context=data["context"],
            limits=data["limits"],
            copilot_session_ids=data.get("copilot_session_ids", {}),
            file_path=checkpoint_path,
        )

    @staticmethod
    def find_latest_checkpoint(workflow_path: Path) -> Path | None:
        """Find the most recent checkpoint for a workflow.

        Scans the checkpoints directory for files matching
        ``<workflow-name>-*.json`` and returns the one with the
        latest filename timestamp.

        Args:
            workflow_path: Path to the workflow YAML file.

        Returns:
            Path to the most recent checkpoint, or ``None`` if none exist.
        """
        checkpoints_dir = CheckpointManager.get_checkpoints_dir()
        workflow_name = workflow_path.stem

        matches = sorted(checkpoints_dir.glob(f"{workflow_name}-*.json"))
        if not matches:
            return None

        # Latest by filename (timestamps sort lexicographically)
        return matches[-1]

    @staticmethod
    def list_checkpoints(workflow_path: Path | None = None) -> list[CheckpointData]:
        """List all checkpoint files, optionally filtered by workflow.

        Args:
            workflow_path: If provided, only list checkpoints for this workflow.

        Returns:
            List of ``CheckpointData`` sorted by ``created_at`` descending.
        """
        checkpoints_dir = CheckpointManager.get_checkpoints_dir()

        if workflow_path is not None:
            workflow_name = workflow_path.stem
            files = list(checkpoints_dir.glob(f"{workflow_name}-*.json"))
        else:
            files = list(checkpoints_dir.glob("*.json"))

        results: list[CheckpointData] = []
        for f in files:
            try:
                cp = CheckpointManager.load_checkpoint(f)
                results.append(cp)
            except CheckpointError:
                # Skip invalid checkpoint files
                logger.warning("Skipping invalid checkpoint file: %s", f)
                continue

        # Sort by created_at descending (most recent first)
        results.sort(key=lambda c: c.created_at, reverse=True)
        return results

    @staticmethod
    def cleanup(checkpoint_path: Path) -> None:
        """Delete a checkpoint file.

        Idempotent — logs a warning if the file does not exist.

        Args:
            checkpoint_path: Path to the checkpoint file to delete.
        """
        try:
            checkpoint_path.unlink()
        except FileNotFoundError:
            logger.warning("Checkpoint file already deleted: %s", checkpoint_path)
        except OSError as e:
            logger.warning("Failed to delete checkpoint file %s: %s", checkpoint_path, e)
