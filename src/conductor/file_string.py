"""Leaf module defining FileString class with source path metadata.

Kept as a leaf module (no intra-``conductor`` imports) so it can be imported
from any layer without risking an import cycle.
"""

from __future__ import annotations

from pathlib import Path


class FileString(str):
    """A string subclass that retains the origin file path.

    Used when loading external prompt files via the ``!file`` tag to track
    their paths, allowing relative template inclusions in Jinja environments.
    """

    source_path: Path

    def __new__(cls, value: str, source_path: Path | str) -> FileString:
        """Create a new FileString instance.

        Args:
            value: The string content.
            source_path: The file path of the source file.

        Returns:
            A new FileString instance.
        """
        obj = super().__new__(cls, value)
        obj.source_path = Path(source_path)
        return obj

    def __getnewargs__(self) -> tuple[str, Path]:  # type: ignore[override]
        """Return arguments for constructor during pickle/deepcopy serialization.

        Returns:
            A tuple of the string value and its source path.
        """
        return (str(self), self.source_path)
