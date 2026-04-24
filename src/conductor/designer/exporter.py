"""Export a ``WorkflowConfig`` to clean, human-readable YAML.

Uses ``ruamel.yaml`` in round-trip mode so that editing an existing
file preserves comments and ordering where possible.  New files get
a clean, opinionated layout.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from conductor.config.schema import WorkflowConfig


def config_to_yaml(config: WorkflowConfig) -> str:
    """Serialise *config* to a YAML string.

    The output is designed to be readable and diff-friendly:
    * Top-level keys are ordered: ``workflow``, ``tools``, ``agents``,
      ``parallel``, ``for_each``, ``output``.
    * Empty collections (``[]``, ``{}``) are omitted.
    * ``None`` values are omitted.
    """
    data = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    ordered = _order_top_level(data)
    cleaned = _clean_defaults(ordered)

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 120
    yaml.indent(mapping=2, sequence=4, offset=2)

    buf = io.StringIO()
    yaml.dump(cleaned, buf)
    return buf.getvalue()


def _order_top_level(data: dict[str, Any]) -> CommentedMap:
    """Return a ``CommentedMap`` with canonical key ordering."""
    key_order = ["workflow", "tools", "agents", "parallel", "for_each", "output"]
    cm = CommentedMap()
    for key in key_order:
        if key in data:
            cm[key] = _to_commented(data[key])
    # Preserve any extra keys not in the canonical order
    for key in data:
        if key not in cm:
            cm[key] = _to_commented(data[key])
    return cm


def _to_commented(value: Any) -> Any:
    """Recursively convert dicts/lists to ruamel CommentedMap/Seq."""
    if isinstance(value, dict):
        cm = CommentedMap()
        for k, v in value.items():
            cm[k] = _to_commented(v)
        return cm
    if isinstance(value, list):
        cs = CommentedSeq()
        for item in value:
            cs.append(_to_commented(item))
        return cs
    return value


def _clean_defaults(data: Any) -> Any:
    """Remove empty collections and default-valued fields."""
    if isinstance(data, (dict, CommentedMap)):
        cleaned = CommentedMap() if isinstance(data, CommentedMap) else {}
        for key, value in data.items():
            value = _clean_defaults(value)
            # Skip empty collections
            if isinstance(value, (list, CommentedSeq)) and len(value) == 0:
                continue
            if isinstance(value, (dict, CommentedMap)) and len(value) == 0:
                continue
            cleaned[key] = value
        return cleaned
    if isinstance(data, (list, CommentedSeq)):
        result = CommentedSeq() if isinstance(data, CommentedSeq) else []
        for item in data:
            result.append(_clean_defaults(item))
        return result
    return data
