"""Golden regression tests for provider output-schema wrappers.

These tests pin the exact full-wrapper output of each provider's schema builder
against pre-refactor literals captured in Task 0. Any behavioral change in the
shared schema builder or a provider wrapper that alters the serialized JSON will
cause these tests to fail.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from conductor.config.schema import OutputField
from conductor.providers.claude import ClaudeProvider
from conductor.providers.claude_agent_sdk import _build_output_format
from conductor.providers.copilot import CopilotProvider
from conductor.providers.hermes import _build_prompt_schema

# Shared schema definitions copied from .omo/scripts/capture_schema_baseline.py.
rich_schema = {
    "string_scalar": OutputField(
        type="string",
        description="A string scalar field with a description",
    ),
    "number_scalar": OutputField(
        type="number",
    ),
    "nested_object": OutputField(
        type="object",
        description="A nested object with properties",
        properties={
            "nested_string": OutputField(
                type="string",
                description="Nested string field",
            ),
            "nested_number": OutputField(
                type="number",
            ),
        },
    ),
    "array_of_scalars": OutputField(
        type="array",
        description="An array of strings",
        items=OutputField(
            type="string",
            description="A string item",
        ),
    ),
    "array_of_objects": OutputField(
        type="array",
        description="An array of objects",
        items=OutputField(
            type="object",
            properties={
                "obj_key": OutputField(
                    type="string",
                    description="The key",
                ),
                "obj_val": OutputField(
                    type="number",
                    description="The value",
                ),
            },
        ),
    ),
    "array_of_arrays": OutputField(
        type="array",
        description="An array of arrays",
        items=OutputField(
            type="array",
            items=OutputField(
                type="number",
                description="A number in nested array",
            ),
        ),
    ),
}

missing_descriptions_schema = {
    "string_scalar": OutputField(
        type="string",
    ),
    "nested_object": OutputField(
        type="object",
        properties={
            "nested_string": OutputField(
                type="string",
            ),
            "nested_number": OutputField(
                type="number",
            ),
        },
    ),
    "array_of_scalars": OutputField(
        type="array",
        items=OutputField(
            type="string",
        ),
    ),
    "array_of_objects": OutputField(
        type="array",
        items=OutputField(
            type="object",
            properties={
                "obj_key": OutputField(
                    type="string",
                ),
                "obj_val": OutputField(
                    type="number",
                ),
            },
        ),
    ),
    "array_of_arrays": OutputField(
        type="array",
        items=OutputField(
            type="array",
            items=OutputField(
                type="number",
            ),
        ),
    ),
}


def _serialize(actual: Any) -> str:
    """Serialize the wrapper output using the exact golden format."""
    return json.dumps(actual, indent=2, sort_keys=False)


# Expected output for ClaudeProvider._build_tools_for_structured_output(rich_schema).
EXPECTED_CLAUDE_RICH_SCHEMA = """[
  {
    "name": "emit_output",
    "description": "Emit the structured output for this task",
    "input_schema": {
      "type": "object",
      "properties": {
        "string_scalar": {
          "type": "string",
          "description": "A string scalar field with a description"
        },
        "number_scalar": {
          "type": "number"
        },
        "nested_object": {
          "type": "object",
          "description": "A nested object with properties",
          "properties": {
            "nested_string": {
              "type": "string",
              "description": "Nested string field"
            },
            "nested_number": {
              "type": "number"
            }
          },
          "required": [
            "nested_string",
            "nested_number"
          ]
        },
        "array_of_scalars": {
          "type": "array",
          "description": "An array of strings",
          "items": {
            "type": "string",
            "description": "A string item"
          }
        },
        "array_of_objects": {
          "type": "array",
          "description": "An array of objects",
          "items": {
            "type": "object",
            "properties": {
              "obj_key": {
                "type": "string",
                "description": "The key"
              },
              "obj_val": {
                "type": "number",
                "description": "The value"
              }
            },
            "required": [
              "obj_key",
              "obj_val"
            ]
          }
        },
        "array_of_arrays": {
          "type": "array",
          "description": "An array of arrays",
          "items": {
            "type": "array",
            "items": {
              "type": "number",
              "description": "A number in nested array"
            }
          }
        }
      },
      "required": [
        "string_scalar",
        "number_scalar",
        "nested_object",
        "array_of_scalars",
        "array_of_objects",
        "array_of_arrays"
      ]
    }
  }
]"""

# Expected output for ClaudeProvider._build_tools_for_structured_output(
# missing_descriptions_schema).
EXPECTED_CLAUDE_MISSING_SCHEMA = """[
  {
    "name": "emit_output",
    "description": "Emit the structured output for this task",
    "input_schema": {
      "type": "object",
      "properties": {
        "string_scalar": {
          "type": "string"
        },
        "nested_object": {
          "type": "object",
          "properties": {
            "nested_string": {
              "type": "string"
            },
            "nested_number": {
              "type": "number"
            }
          },
          "required": [
            "nested_string",
            "nested_number"
          ]
        },
        "array_of_scalars": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "array_of_objects": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "obj_key": {
                "type": "string"
              },
              "obj_val": {
                "type": "number"
              }
            },
            "required": [
              "obj_key",
              "obj_val"
            ]
          }
        },
        "array_of_arrays": {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "number"
            }
          }
        }
      },
      "required": [
        "string_scalar",
        "nested_object",
        "array_of_scalars",
        "array_of_objects",
        "array_of_arrays"
      ]
    }
  }
]"""

# Expected output for CopilotProvider._build_prompt_schema(rich_schema).
EXPECTED_COPILOT_RICH_SCHEMA = """{
  "string_scalar": {
    "type": "string",
    "description": "A string scalar field with a description"
  },
  "number_scalar": {
    "type": "number",
    "description": "The number_scalar field"
  },
  "nested_object": {
    "type": "object",
    "description": "A nested object with properties",
    "properties": {
      "nested_string": {
        "type": "string",
        "description": "Nested string field"
      },
      "nested_number": {
        "type": "number",
        "description": "The nested_number field"
      }
    },
    "required": [
      "nested_string",
      "nested_number"
    ]
  },
  "array_of_scalars": {
    "type": "array",
    "description": "An array of strings",
    "items": {
      "type": "string",
      "description": "A string item"
    }
  },
  "array_of_objects": {
    "type": "array",
    "description": "An array of objects",
    "items": {
      "type": "object",
      "properties": {
        "obj_key": {
          "type": "string",
          "description": "The key"
        },
        "obj_val": {
          "type": "number",
          "description": "The value"
        }
      },
      "required": [
        "obj_key",
        "obj_val"
      ]
    }
  },
  "array_of_arrays": {
    "type": "array",
    "description": "An array of arrays",
    "items": {
      "type": "array",
      "items": {
        "type": "number",
        "description": "A number in nested array"
      }
    }
  }
}"""

# Expected output for CopilotProvider._build_prompt_schema(missing_descriptions_schema).
EXPECTED_COPILOT_MISSING_SCHEMA = """{
  "string_scalar": {
    "type": "string",
    "description": "The string_scalar field"
  },
  "nested_object": {
    "type": "object",
    "description": "The nested_object field",
    "properties": {
      "nested_string": {
        "type": "string",
        "description": "The nested_string field"
      },
      "nested_number": {
        "type": "number",
        "description": "The nested_number field"
      }
    },
    "required": [
      "nested_string",
      "nested_number"
    ]
  },
  "array_of_scalars": {
    "type": "array",
    "description": "The array_of_scalars field",
    "items": {
      "type": "string"
    }
  },
  "array_of_objects": {
    "type": "array",
    "description": "The array_of_objects field",
    "items": {
      "type": "object",
      "properties": {
        "obj_key": {
          "type": "string",
          "description": "The obj_key field"
        },
        "obj_val": {
          "type": "number",
          "description": "The obj_val field"
        }
      },
      "required": [
        "obj_key",
        "obj_val"
      ]
    }
  },
  "array_of_arrays": {
    "type": "array",
    "description": "The array_of_arrays field",
    "items": {
      "type": "array",
      "items": {
        "type": "number"
      }
    }
  }
}"""

# Expected output for Hermes _build_prompt_schema(rich_schema).
EXPECTED_HERMES_RICH_SCHEMA = """{
  "string_scalar": {
    "type": "string",
    "description": "A string scalar field with a description"
  },
  "number_scalar": {
    "type": "number",
    "description": "The number_scalar field"
  },
  "nested_object": {
    "type": "object",
    "description": "A nested object with properties",
    "properties": {
      "nested_string": {
        "type": "string",
        "description": "Nested string field"
      },
      "nested_number": {
        "type": "number",
        "description": "The nested_number field"
      }
    },
    "required": [
      "nested_string",
      "nested_number"
    ]
  },
  "array_of_scalars": {
    "type": "array",
    "description": "An array of strings",
    "items": {
      "type": "string",
      "description": "A string item"
    }
  },
  "array_of_objects": {
    "type": "array",
    "description": "An array of objects",
    "items": {
      "type": "object",
      "properties": {
        "obj_key": {
          "type": "string",
          "description": "The key"
        },
        "obj_val": {
          "type": "number",
          "description": "The value"
        }
      }
    }
  },
  "array_of_arrays": {
    "type": "array",
    "description": "An array of arrays",
    "items": {
      "type": "array"
    }
  }
}"""

# Expected output for Hermes _build_prompt_schema(missing_descriptions_schema).
EXPECTED_HERMES_MISSING_SCHEMA = """{
  "string_scalar": {
    "type": "string",
    "description": "The string_scalar field"
  },
  "nested_object": {
    "type": "object",
    "description": "The nested_object field",
    "properties": {
      "nested_string": {
        "type": "string",
        "description": "The nested_string field"
      },
      "nested_number": {
        "type": "number",
        "description": "The nested_number field"
      }
    },
    "required": [
      "nested_string",
      "nested_number"
    ]
  },
  "array_of_scalars": {
    "type": "array",
    "description": "The array_of_scalars field",
    "items": {
      "type": "string"
    }
  },
  "array_of_objects": {
    "type": "array",
    "description": "The array_of_objects field",
    "items": {
      "type": "object",
      "properties": {
        "obj_key": {
          "type": "string",
          "description": "The obj_key field"
        },
        "obj_val": {
          "type": "number",
          "description": "The obj_val field"
        }
      }
    }
  },
  "array_of_arrays": {
    "type": "array",
    "description": "The array_of_arrays field",
    "items": {
      "type": "array"
    }
  }
}"""

# Expected output for Claude Agent SDK _build_output_format(rich_schema).
EXPECTED_CLAUDE_AGENT_SDK_RICH_SCHEMA = """{
  "type": "json_schema",
  "schema": {
    "type": "object",
    "properties": {
      "string_scalar": {
        "type": "string",
        "description": "A string scalar field with a description"
      },
      "number_scalar": {
        "type": "number"
      },
      "nested_object": {
        "type": "object",
        "description": "A nested object with properties",
        "properties": {
          "nested_string": {
            "type": "string",
            "description": "Nested string field"
          },
          "nested_number": {
            "type": "number"
          }
        },
        "required": [
          "nested_string",
          "nested_number"
        ]
      },
      "array_of_scalars": {
        "type": "array",
        "description": "An array of strings",
        "items": {
          "type": "string",
          "description": "A string item"
        }
      },
      "array_of_objects": {
        "type": "array",
        "description": "An array of objects",
        "items": {
          "type": "object",
          "properties": {
            "obj_key": {
              "type": "string",
              "description": "The key"
            },
            "obj_val": {
              "type": "number",
              "description": "The value"
            }
          },
          "required": [
            "obj_key",
            "obj_val"
          ]
        }
      },
      "array_of_arrays": {
        "type": "array",
        "description": "An array of arrays",
        "items": {
          "type": "array",
          "items": {
            "type": "number",
            "description": "A number in nested array"
          }
        }
      }
    },
    "required": [
      "string_scalar",
      "number_scalar",
      "nested_object",
      "array_of_scalars",
      "array_of_objects",
      "array_of_arrays"
    ]
  }
}"""

# Expected output for Claude Agent SDK _build_output_format(missing_descriptions_schema).
EXPECTED_CLAUDE_AGENT_SDK_MISSING_SCHEMA = """{
  "type": "json_schema",
  "schema": {
    "type": "object",
    "properties": {
      "string_scalar": {
        "type": "string"
      },
      "nested_object": {
        "type": "object",
        "properties": {
          "nested_string": {
            "type": "string"
          },
          "nested_number": {
            "type": "number"
          }
        },
        "required": [
          "nested_string",
          "nested_number"
        ]
      },
      "array_of_scalars": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "array_of_objects": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "obj_key": {
              "type": "string"
            },
            "obj_val": {
              "type": "number"
            }
          },
          "required": [
            "obj_key",
            "obj_val"
          ]
        }
      },
      "array_of_arrays": {
        "type": "array",
        "items": {
          "type": "array",
          "items": {
            "type": "number"
          }
        }
      }
    },
    "required": [
      "string_scalar",
      "nested_object",
      "array_of_scalars",
      "array_of_objects",
      "array_of_arrays"
    ]
  }
}"""


def _make_copilot_provider() -> CopilotProvider:
    """Return a CopilotProvider instance wired to a no-op stub handler."""

    def stub_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        return {"result": "stub"}

    return CopilotProvider(mock_handler=stub_handler)


class TestClaudeOutputSchemaGolden:
    """Golden tests for ClaudeProvider's structured-output tool wrapper."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_build_tools_rich_schema_matches_baseline(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Claude tool wrapper output must be byte-for-byte identical to the pre-refactor
        baseline for a schema with descriptions, nested objects, and arrays."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        actual = _serialize(provider._build_tools_for_structured_output(rich_schema))
        assert actual == EXPECTED_CLAUDE_RICH_SCHEMA

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_build_tools_missing_descriptions_matches_baseline(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Claude tool wrapper output must be byte-for-byte identical to the pre-refactor
        baseline for a schema without explicit descriptions."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        actual = _serialize(
            provider._build_tools_for_structured_output(missing_descriptions_schema)
        )
        assert actual == EXPECTED_CLAUDE_MISSING_SCHEMA


class TestCopilotOutputSchemaGolden:
    """Golden tests for CopilotProvider's prompt schema wrapper."""

    def test_build_prompt_schema_rich_matches_baseline(self) -> None:
        """Copilot prompt schema wrapper must be byte-for-byte identical to the pre-refactor
        baseline for a schema with descriptions and nested structure."""
        provider = _make_copilot_provider()
        actual = _serialize(provider._build_prompt_schema(rich_schema))
        assert actual == EXPECTED_COPILOT_RICH_SCHEMA

    def test_build_prompt_schema_missing_descriptions_matches_baseline(self) -> None:
        """Copilot prompt schema wrapper must be byte-for-byte identical to the pre-refactor
        baseline for a schema without descriptions, including description fallbacks."""
        provider = _make_copilot_provider()
        actual = _serialize(provider._build_prompt_schema(missing_descriptions_schema))
        assert actual == EXPECTED_COPILOT_MISSING_SCHEMA


class TestHermesOutputSchemaGolden:
    """Golden tests for Hermes' module-level prompt schema wrapper."""

    def test_build_prompt_schema_rich_matches_baseline(self) -> None:
        """Hermes prompt schema wrapper must be byte-for-byte identical to the pre-refactor
        baseline for a schema with descriptions, including legacy collapsed array-of-arrays."""
        actual = _serialize(_build_prompt_schema(rich_schema))
        assert actual == EXPECTED_HERMES_RICH_SCHEMA

    def test_build_prompt_schema_missing_descriptions_matches_baseline(self) -> None:
        """Hermes prompt schema wrapper must be byte-for-byte identical to the pre-refactor
        baseline for a schema without descriptions, including description fallbacks."""
        actual = _serialize(_build_prompt_schema(missing_descriptions_schema))
        assert actual == EXPECTED_HERMES_MISSING_SCHEMA


class TestClaudeAgentSdkOutputSchemaGolden:
    """Golden tests for Claude Agent SDK's output_format wrapper."""

    def test_build_output_format_rich_matches_baseline(self) -> None:
        """Claude Agent SDK output_format wrapper must be byte-for-byte identical to the
        pre-refactor baseline for a schema with descriptions and nested structure."""
        actual = _serialize(_build_output_format(rich_schema))
        assert actual == EXPECTED_CLAUDE_AGENT_SDK_RICH_SCHEMA

    def test_build_output_format_missing_descriptions_matches_baseline(self) -> None:
        """Claude Agent SDK output_format wrapper must be byte-for-byte identical to the
        pre-refactor baseline for a schema without descriptions."""
        actual = _serialize(_build_output_format(missing_descriptions_schema))
        assert actual == EXPECTED_CLAUDE_AGENT_SDK_MISSING_SCHEMA


class TestGoldenMutationGuard:
    """Negative guard proving the golden tests are sensitive to behavioral changes."""

    def test_mutated_copilot_literal_does_not_match(self) -> None:
        """If the builder output is mutated (e.g., a description fallback is changed), the
        golden assertion must fail so regressions are caught."""
        provider = _make_copilot_provider()
        actual = _serialize(provider._build_prompt_schema(rich_schema))
        mutated = EXPECTED_COPILOT_RICH_SCHEMA.replace(
            '"description": "The number_scalar field"',
            '"description": "The number_scalar field (mutated)"',
        )
        assert actual != mutated
