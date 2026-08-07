from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ParameterDefinition(FrozenModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    value_type: Literal["integer", "boolean", "enum", "string"]
    unit: Literal["um", "N", "count", "id", "none"]
    required: bool = True
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    enum_values: tuple[str, ...] = ()
    production_impact: bool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> ParameterDefinition:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("parameter bounds are inverted")
        if self.value_type == "enum" and not self.enum_values:
            raise ValueError("enum parameter requires enum_values")
        return self


class DerivedDimension(FrozenModel):
    key: str
    expression: str
    unit: Literal["um", "count"]
    dependencies: tuple[str, ...]


class ComponentDefinition(FrozenModel):
    role: str
    multiplicity_expression: str
    material_compatibility: tuple[str, ...]
    named_edges: tuple[str, ...]
    machine_operations: tuple[str, ...]


class RuleBinding(FrozenModel):
    rule_id: str
    version: str
    severity_can_block: bool


class TemplateDefinition(FrozenModel):
    schema_version: str
    template_id: str
    template_version: str
    furniture_type: Literal["bookcase"]
    coordinate_system: Literal["RH_X_WIDTH_Y_DEPTH_Z_HEIGHT"]
    persisted_unit: Literal["um"]
    parameters: tuple[ParameterDefinition, ...]
    derived_dimensions: tuple[DerivedDimension, ...]
    constraints: tuple[str, ...]
    components: tuple[ComponentDefinition, ...]
    compatible_material_types: tuple[str, ...]
    joint_systems: tuple[str, ...]
    rules: tuple[RuleBinding, ...]
    assembly_graph_version: str
    manufacturing_feature_version: str

    @model_validator(mode="after")
    def unique_keys(self) -> TemplateDefinition:
        keys = [parameter.key for parameter in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("template parameter keys must be unique")
        derived = [item.key for item in self.derived_dimensions]
        if len(derived) != len(set(derived)):
            raise ValueError("derived dimension keys must be unique")
        return self
