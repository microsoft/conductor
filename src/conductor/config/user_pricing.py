"""User-level (machine-wide) pricing overrides.

Loads optional pricing overrides from ``~/.conductor/pricing.yaml`` (or the
path in ``CONDUCTOR_PRICING_FILE``, with ``~`` expansion). A missing file
returns an empty mapping; a malformed file raises ``ConfigurationError``
with a pointer to the path — silent acceptance of corrupted overrides
would re-introduce the "your costs are wrong and you don't know" bug this
exists to solve.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.config.schema import PricingOverride
from conductor.engine.pricing import ModelPricing
from conductor.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

USER_PRICING_ENV_VAR = "CONDUCTOR_PRICING_FILE"


def get_user_pricing_path() -> Path:
    """Return the path conductor would read for user-level pricing.

    Honors ``CONDUCTOR_PRICING_FILE`` (with ``~`` expansion) when set;
    otherwise returns ``~/.conductor/pricing.yaml``.
    """
    raw = os.environ.get(USER_PRICING_ENV_VAR)
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".conductor" / "pricing.yaml"


def load_user_pricing(path: Path | None = None) -> dict[str, ModelPricing]:
    """Load machine-wide pricing overrides from the user file.

    Returns an empty mapping when the file does not exist. Raises
    ``ConfigurationError`` when the file exists but is unreadable, invalid
    YAML, or fails ``PricingOverride`` schema validation.
    """
    target = path if path is not None else get_user_pricing_path()

    if not target.exists():
        return {}

    try:
        data = YAML(typ="safe").load(target.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as e:
        raise ConfigurationError(
            f"Failed to load user pricing file '{target}': {e}",
            suggestion=(
                "Fix the file, delete it, or temporarily bypass it by setting "
                "CONDUCTOR_PRICING_FILE to a path that does not exist "
                "(e.g. CONDUCTOR_PRICING_FILE=/dev/null)."
            ),
            file_path=str(target),
        ) from e

    if data is None:
        return {}

    if not isinstance(data, dict) or "pricing" not in data:
        raise ConfigurationError(
            f"User pricing file '{target}' must contain a top-level `pricing:` mapping.",
            file_path=str(target),
        )

    raw_entries = data["pricing"]
    if raw_entries is None:
        return {}
    if not isinstance(raw_entries, dict):
        raise ConfigurationError(
            f"User pricing file '{target}' has a `pricing:` value of type "
            f"{type(raw_entries).__name__}; expected a mapping of model name to "
            f"pricing override.",
            file_path=str(target),
        )

    overrides: dict[str, ModelPricing] = {}
    for model_name, entry in raw_entries.items():
        if not isinstance(model_name, str):
            raise ConfigurationError(
                f"User pricing file '{target}' has a non-string model key "
                f"{model_name!r}; model names must be strings.",
                file_path=str(target),
            )
        try:
            override = PricingOverride.model_validate(entry)
        except ValidationError as e:
            raise ConfigurationError(
                f"Invalid pricing entry for {model_name!r} in '{target}': {e}",
                file_path=str(target),
            ) from e
        overrides[model_name] = ModelPricing(
            input_per_mtok=override.input_per_mtok,
            output_per_mtok=override.output_per_mtok,
            cache_read_per_mtok=override.cache_read_per_mtok,
            cache_write_per_mtok=override.cache_write_per_mtok,
        )

    if overrides:
        logger.info("Loaded %d user pricing override(s) from %s", len(overrides), target)

    return overrides
