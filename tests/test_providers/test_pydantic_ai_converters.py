"""Unit tests for OutputField to Pydantic model conversion.

Tests verify that Conductor output schemas are translated into Pydantic
models that validate identically to Conductor's own ``validate_output``
semantics: all declared fields are required, nested structures are
recursive, free-form objects/arrays tolerate arbitrary values, and
field descriptions are preserved as JSON-schema descriptions.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError as ConductorValidationError
from conductor.providers._pydantic_ai.converters import (
    _map_output_field_type,
    output_schema_to_pydantic_model,
)


class TestMapOutputFieldTypeScalar:
    """Tests for scalar type mapping."""

    def test_string_maps_to_str(self) -> None:
        """A string OutputField must map to the Python ``str`` type."""
        assert _map_output_field_type(OutputField(type="string")) is str

    def test_number_maps_to_union_int_float(self) -> None:
        """A number OutputField must accept both int and float but reject booleans."""
        number_type = _map_output_field_type(OutputField(type="number"))

        class M(BaseModel):
            value: number_type  # type: ignore[valid-type]

        M(value=42)
        M(value=3.14)
        with pytest.raises(PydanticValidationError):
            M(value=True)
        with pytest.raises(PydanticValidationError):
            M(value="not a number")

    def test_boolean_maps_to_bool(self) -> None:
        """A boolean OutputField must map to the Python ``bool`` type."""
        assert _map_output_field_type(OutputField(type="boolean")) is bool

    def test_integer_maps_to_int_rejecting_bool(self) -> None:
        """An integer OutputField must map to ``int`` but reject booleans, just
        as Conductor's ``_check_type`` rejects booleans for numeric types.

        ``OutputField.type`` currently omits ``integer`` from its Literal, so
        the field is built with ``model_construct`` to exercise the converter
        branch without Pydantic validation rejecting the value.
        """
        field = OutputField.model_construct(type="integer")
        integer_type = _map_output_field_type(field)

        class M(BaseModel):
            value: integer_type  # type: ignore[valid-type]

        M(value=42)
        with pytest.raises(PydanticValidationError):
            M(value=True)
        with pytest.raises(PydanticValidationError):
            M(value=3.14)
        with pytest.raises(PydanticValidationError):
            M(value="not an integer")


class TestMapOutputFieldTypeArray:
    """Tests for array type mapping."""

    def test_array_with_items_maps_to_list(self) -> None:
        """An array with declared items must map to ``list[item_type]``."""
        array_type = _map_output_field_type(
            OutputField(type="array", items=OutputField(type="string"))
        )

        class M(BaseModel):
            tags: array_type  # type: ignore[valid-type]

        M(tags=["a", "b"])
        with pytest.raises(PydanticValidationError):
            M(tags=[1, 2])

    def test_array_without_items_maps_to_list_any(self) -> None:
        """An array without declared items must map to ``list[Any]`` and accept
        heterogeneous values, matching Conductor's passthrough behaviour."""
        array_type = _map_output_field_type(OutputField(type="array"))

        class M(BaseModel):
            tags: array_type  # type: ignore[valid-type]

        M(tags=[1, "two", {"three": 3}])


class TestMapOutputFieldTypeObject:
    """Tests for object type mapping."""

    def test_object_with_properties_maps_to_nested_model(self) -> None:
        """An object with properties must map to a dynamically created Pydantic model."""
        obj_type = _map_output_field_type(
            OutputField(
                type="object",
                properties={
                    "name": OutputField(type="string"),
                    "age": OutputField(type="number"),
                },
            ),
            prefix="Person",
        )

        assert issubclass(obj_type, BaseModel)
        assert obj_type.__name__ == "Person"
        instance = obj_type(name="Alice", age=30)
        assert instance.name == "Alice"
        assert instance.age == 30

    def test_object_without_properties_maps_to_dict_any(self) -> None:
        """An object without properties must map to ``dict[str, Any]`` and accept
        arbitrary keys and values, matching free-form output behaviour."""
        obj_type = _map_output_field_type(OutputField(type="object"))

        class M(BaseModel):
            data: obj_type  # type: ignore[valid-type]

        M(data={"a": 1, "b": [2], "c": {"nested": True}})


class TestOutputSchemaToPydanticModel:
    """Tests for the top-level ``output_schema_to_pydantic_model`` helper."""

    def test_empty_schema_returns_none(self) -> None:
        """An empty or None output schema must return None so callers can fall
        back to plain text output."""
        assert output_schema_to_pydantic_model("Empty", {}) is None
        assert output_schema_to_pydantic_model("Empty", None) is None

    def test_all_scalar_types(self) -> None:
        """A schema with all scalar types must build a model that accepts only
        values matching Conductor's semantics."""
        model = output_schema_to_pydantic_model(
            "Scalars",
            {
                "name": OutputField(type="string"),
                "count": OutputField(type="number"),
                "active": OutputField(type="boolean"),
            },
        )
        assert model is not None

        instance = model(name="Alice", count=42, active=True)
        assert instance.name == "Alice"
        assert instance.count == 42
        assert instance.active is True

        with pytest.raises(PydanticValidationError):
            model(name="Alice", count=True, active=True)

    def test_missing_field_raises(self) -> None:
        """All output fields are required by default; missing fields must raise."""
        model = output_schema_to_pydantic_model(
            "Required",
            {"answer": OutputField(type="string")},
        )
        assert model is not None

        with pytest.raises(PydanticValidationError):
            model()

    def test_extra_fields_allowed(self) -> None:
        """The generated model must allow extra fields, matching Conductor's
        ``validate_output`` which ignores undeclared keys."""
        model = output_schema_to_pydantic_model(
            "Extras",
            {"answer": OutputField(type="string")},
        )
        assert model is not None

        instance = model(answer="hello", extra="ignored")
        assert instance.answer == "hello"
        assert instance.extra == "ignored"

    def test_descriptions_preserved(self) -> None:
        """Field descriptions must be preserved in the generated model's
        JSON schema so downstream tooling can expose them."""
        model = output_schema_to_pydantic_model(
            "Described",
            {
                "answer": OutputField(
                    type="string",
                    description="The final answer to the question.",
                )
            },
        )
        assert model is not None

        schema = model.model_json_schema()
        assert schema["properties"]["answer"]["description"] == "The final answer to the question."


class TestNestedStructures:
    """Tests for nested object and array combinations."""

    def test_array_of_scalars(self) -> None:
        """An array of strings must enforce string items."""
        model = output_schema_to_pydantic_model(
            "ArrayOfScalars",
            {
                "tags": OutputField(
                    type="array",
                    items=OutputField(type="string"),
                ),
            },
        )
        assert model is not None

        model(tags=["a", "b"])
        with pytest.raises(PydanticValidationError):
            model(tags=[1, 2])

    def test_array_of_objects(self) -> None:
        """An array of objects must validate each item's properties recursively."""
        model = output_schema_to_pydantic_model(
            "ArrayOfObjects",
            {
                "findings": OutputField(
                    type="array",
                    items=OutputField(
                        type="object",
                        properties={
                            "title": OutputField(type="string"),
                            "score": OutputField(type="number"),
                        },
                    ),
                ),
            },
        )
        assert model is not None

        model(
            findings=[
                {"title": "first", "score": 1.0},
                {"title": "second", "score": 2.0},
            ]
        )

        with pytest.raises(PydanticValidationError):
            model(findings=[{"title": "first", "score": "high"}])

        with pytest.raises(PydanticValidationError):
            model(findings=[{"title": "first"}])

    def test_object_with_nested_array(self) -> None:
        """An object containing an array property must validate that array's items."""
        model = output_schema_to_pydantic_model(
            "ObjectWithArray",
            {
                "person": OutputField(
                    type="object",
                    properties={
                        "name": OutputField(type="string"),
                        "scores": OutputField(
                            type="array",
                            items=OutputField(type="number"),
                        ),
                    },
                ),
            },
        )
        assert model is not None

        model(person={"name": "Alice", "scores": [1, 2, 3]})

        with pytest.raises(PydanticValidationError):
            model(person={"name": "Alice", "scores": [1, "two", 3]})

    def test_object_with_nested_object(self) -> None:
        """An object containing another object must validate nested properties."""
        model = output_schema_to_pydantic_model(
            "ObjectWithObject",
            {
                "outer": OutputField(
                    type="object",
                    properties={
                        "inner": OutputField(
                            type="object",
                            properties={
                                "value": OutputField(type="string"),
                            },
                        ),
                    },
                ),
            },
        )
        assert model is not None

        model(outer={"inner": {"value": "nested"}})

        with pytest.raises(PydanticValidationError):
            model(outer={"inner": {"value": 123}})

        with pytest.raises(PydanticValidationError):
            model(outer={"inner": {}})

    def test_deep_nesting_array_of_arrays(self) -> None:
        """array<array<number>> must validate at every level."""
        model = output_schema_to_pydantic_model(
            "DeepArray",
            {
                "matrix": OutputField(
                    type="array",
                    items=OutputField(
                        type="array",
                        items=OutputField(type="number"),
                    ),
                ),
            },
        )
        assert model is not None

        model(matrix=[[1.0, 2.0], [3.0, 4.0]])

        with pytest.raises(PydanticValidationError):
            model(matrix=[[1.0, "x"]])


class TestDepthLimits:
    """Tests for schema nesting depth protection."""

    def test_depth_limit_raises(self) -> None:
        """Schemas exceeding ``max_depth`` must raise a clear error, mirroring
        the JSON-schema builder's depth guard."""
        schema = OutputField(
            type="array",
            items=OutputField(
                type="array",
                items=OutputField(
                    type="array",
                    items=OutputField(type="string"),
                ),
            ),
        )

        with pytest.raises(ValueError, match="Maximum output schema nesting depth"):
            _map_output_field_type(schema, max_depth=2)

    def test_deep_valid_schema_at_default_limit(self) -> None:
        """A deeply nested schema at the default depth limit must build successfully."""
        inner: OutputField = OutputField(type="string")
        for _ in range(5):
            inner = OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={"x": inner},
                ),
            )
        model = output_schema_to_pydantic_model("Deep", {"data": inner})
        assert model is not None


class TestFreeformEdges:
    """Tests for free-form object and array edges."""

    def test_freeform_object_accepted(self) -> None:
        """An object without properties must accept arbitrary JSON-like data."""
        model = output_schema_to_pydantic_model(
            "FreeformObject",
            {"metadata": OutputField(type="object")},
        )
        assert model is not None

        model(metadata={"any": ["value", 1, True, {"nested": None}]})

    def test_freeform_array_accepted(self) -> None:
        """An array without items must accept a heterogeneous list."""
        model = output_schema_to_pydantic_model(
            "FreeformArray",
            {"items": OutputField(type="array")},
        )
        assert model is not None

        model(items=[1, "two", True, {"nested": []}])


class TestDescriptionPreservation:
    """Tests for description preservation across nested structures."""

    def test_nested_descriptions_in_json_schema(self) -> None:
        """Descriptions at every nesting level must survive in the model's JSON schema."""
        model = output_schema_to_pydantic_model(
            "DescribedNested",
            {
                "findings": OutputField(
                    type="array",
                    description="Top-level findings list.",
                    items=OutputField(
                        type="object",
                        description="A single finding.",
                        properties={
                            "title": OutputField(
                                type="string",
                                description="Finding title.",
                            ),
                        },
                    ),
                ),
            },
        )
        assert model is not None

        schema = model.model_json_schema()
        findings = schema["properties"]["findings"]
        assert findings["description"] == "Top-level findings list."
        item_ref = findings["items"]["$ref"]
        item_key = item_ref.split("/")[-1]
        item_schema = schema["$defs"][item_key]
        assert item_schema["description"] == "A single finding."
        assert item_schema["properties"]["title"]["description"] == "Finding title."


class TestValidationParity:
    """Tests that the generated model matches Conductor's validate_output semantics."""

    def test_array_of_objects_missing_nested_field(self) -> None:
        """Missing required fields inside nested array objects must raise, just as
        Conductor's ``validate_output`` does."""
        from conductor.executor.output import validate_output

        schema = {
            "findings": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "title": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                ),
            )
        }
        model = output_schema_to_pydantic_model("Parity", schema)
        assert model is not None

        with pytest.raises(PydanticValidationError):
            model(findings=[{"title": "first"}])

        with pytest.raises(ConductorValidationError):
            validate_output({"findings": [{"title": "first"}]}, schema)

    def test_valid_content_passes_both(self) -> None:
        """Valid content must pass both the Pydantic model and Conductor validation."""
        from conductor.executor.output import validate_output

        content = {"findings": [{"title": "first", "score": 1.0}]}
        schema = {
            "findings": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "title": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                ),
            )
        }
        model = output_schema_to_pydantic_model("Parity", schema)
        assert model is not None

        model(**content)
        validate_output(content, schema)
