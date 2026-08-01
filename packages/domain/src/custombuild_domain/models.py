from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .enums import (
    AssemblyDirection,
    BackPanelType,
    DesignStatus,
    FaceName,
    FeatureKind,
    GrainDirection,
    JointType,
    MaterialType,
    PartRole,
    ReinforcementMode,
    ShelfMount,
)
from .units import mm

PositiveUm = Annotated[int, Field(strict=True, gt=0)]
NonNegativeUm = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StableKey = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")]

BOOKCASE_ENGINE_VERSION = "0.2.0"
BOOKCASE_TEMPLATE_VERSION = "1.0.0"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class PropertySource(FrozenModel):
    source_id: StableKey
    title: str = Field(min_length=1, max_length=240)
    revision: str = Field(min_length=1, max_length=64)
    uri: str | None = None
    valid_from: str | None = None
    note: str | None = None


class MaterialVersion(FrozenModel):
    material_id: StableKey
    version: StableKey
    name: str = Field(min_length=1, max_length=160)
    material_type: MaterialType = MaterialType.SHEET_GOOD
    nominal_thickness_um: PositiveUm
    density_kg_m3: PositiveInt
    elastic_modulus_mpa: PositiveInt
    bending_strength_mpa: PositiveInt
    shear_strength_mpa: PositiveInt
    creep_factor_permille: NonNegativeInt = 600
    property_uncertainty_permille: NonNegativeInt = 150
    min_supported_thickness_um: PositiveUm = mm(6)
    max_supported_thickness_um: PositiveUm = mm(40)
    grain_direction: GrainDirection = GrainDirection.X
    machining_allowance_um: NonNegativeUm = 0
    source: PropertySource

    @model_validator(mode="after")
    def validate_thickness_range(self) -> MaterialVersion:
        if self.min_supported_thickness_um > self.max_supported_thickness_um:
            raise ValueError("material thickness range is inverted")
        if (
            not self.min_supported_thickness_um
            <= self.nominal_thickness_um
            <= self.max_supported_thickness_um
        ):
            raise ValueError("nominal thickness is outside the material version's supported range")
        if self.property_uncertainty_permille > 900:
            raise ValueError("material property uncertainty must be at most 900 permille")
        return self


class WallAnchorSpec(FrozenModel):
    required: bool = False
    wall_substrate: str | None = None
    anchor_system_id: StableKey | None = None
    evidence_id: StableKey | None = None
    verified: bool = False

    @model_validator(mode="after")
    def verified_needs_evidence(self) -> WallAnchorSpec:
        if self.verified and not (
            self.required and self.wall_substrate and self.anchor_system_id and self.evidence_id
        ):
            raise ValueError(
                "a verified wall anchor requires substrate, approved system and evidence ID"
            )
        return self


class BookcaseParameters(FrozenModel):
    width_um: PositiveUm = mm(900)
    height_um: PositiveUm = mm(2_000)
    depth_um: PositiveUm = mm(320)
    nominal_thickness_um: PositiveUm = mm(18)
    actual_thickness_um: PositiveUm = mm(18)
    shelf_count: NonNegativeInt = 5
    shelf_mount: ShelfMount = ShelfMount.FIXED
    shelf_load_n: NonNegativeInt = 300
    vertical_divider_count: NonNegativeInt = 0
    back_panel: BackPanelType = BackPanelType.INSET_GROOVE
    back_thickness_um: PositiveUm = mm(6)
    plinth_height_um: NonNegativeUm = mm(80)
    shelf_side_clearance_um: NonNegativeUm = mm(1)
    edge_band_thickness_um: NonNegativeUm = mm(1)
    joint_system: JointType = JointType.DADO
    reinforcement_mode: ReinforcementMode = ReinforcementMode.MANUAL
    max_deflection_um: PositiveUm = mm(3)
    deflection_span_ratio: PositiveInt = 200
    structural_safety_factor_permille: PositiveInt = 1_800
    assumed_horizontal_force_n: PositiveInt = 50
    wall_anchor: WallAnchorSpec = Field(default_factory=WallAnchorSpec)

    @model_validator(mode="after")
    def validate_geometry(self) -> BookcaseParameters:
        # Import locally to keep the support catalogue independent from the
        # Pydantic schema module during package initialisation.
        from .support import SUPPORTED_BOOKCASE_PRIMARY_JOINTS

        if self.joint_system not in SUPPORTED_BOOKCASE_PRIMARY_JOINTS:
            supported = ", ".join(
                sorted(joint.value for joint in SUPPORTED_BOOKCASE_PRIMARY_JOINTS)
            )
            raise ValueError(
                f"joint system {self.joint_system.value!r} is not supported end-to-end "
                f"by the production MVP; supported primary joint systems: {supported}"
            )
        t = self.actual_thickness_um
        if self.width_um < mm(250) or self.height_um < mm(300) or self.depth_um < mm(100):
            raise ValueError("bookcase dimensions are below the supported template minimum")
        if self.width_um <= 2 * t:
            raise ValueError("width must exceed two side thicknesses")
        if self.height_um <= self.plinth_height_um + 2 * t:
            raise ValueError("height leaves no usable internal opening")
        if self.depth_um <= t:
            raise ValueError("depth must exceed material thickness")
        inner_width = self.width_um - 2 * t - self.vertical_divider_count * t
        if inner_width <= 0:
            raise ValueError("vertical dividers consume all internal width")
        bay_count = self.vertical_divider_count + 1
        minimum_shelf_width = 2 * self.shelf_side_clearance_um + mm(40)
        if inner_width // bay_count <= minimum_shelf_width:
            raise ValueError("divider layout leaves an unmanufacturable shelf width")
        inner_height = self.height_um - self.plinth_height_um - 2 * t
        if inner_height - self.shelf_count * t < (self.shelf_count + 1) * mm(40):
            raise ValueError("shelf layout leaves an unmanufacturable opening height")
        if (
            self.back_panel == BackPanelType.INSET_GROOVE
            and self.depth_um <= self.back_thickness_um + mm(12)
        ):
            raise ValueError("depth is insufficient for the inset back-panel groove")
        if self.back_panel == BackPanelType.NONE and self.back_thickness_um <= 0:
            raise ValueError("back thickness must remain a positive catalogue value")
        if not 1_000 <= self.structural_safety_factor_permille <= 5_000:
            raise ValueError("structural safety factor must be between 1.0 and 5.0")
        return self


class BookcaseDesignSpec(FrozenModel):
    design_id: StableKey
    revision: PositiveInt = 1
    template_id: StableKey = "bookcase"
    template_version: StableKey = BOOKCASE_TEMPLATE_VERSION
    engine_version: StableKey = BOOKCASE_ENGINE_VERSION
    status: DesignStatus = DesignStatus.DRAFT
    parameters: BookcaseParameters
    material: MaterialVersion
    back_material: MaterialVersion | None = None

    @model_validator(mode="after")
    def validate_material(self) -> BookcaseDesignSpec:
        p = self.parameters
        m = self.material
        if p.nominal_thickness_um != m.nominal_thickness_um:
            raise ValueError("design nominal thickness must match material catalogue version")
        if (
            not m.min_supported_thickness_um
            <= p.actual_thickness_um
            <= m.max_supported_thickness_um
        ):
            raise ValueError("measured thickness is outside the material version's supported range")
        if p.back_panel != BackPanelType.NONE:
            if self.back_material is None:
                raise ValueError(
                    "a versioned back material is required when a back panel is present"
                )
            if not (
                self.back_material.min_supported_thickness_um
                <= p.back_thickness_um
                <= self.back_material.max_supported_thickness_um
            ):
                raise ValueError(
                    "measured back thickness is outside the back material version's supported range"
                )
        return self


class Dimensions3D(FrozenModel):
    width_um: PositiveUm
    depth_um: PositiveUm
    height_um: PositiveUm


class Point3D(FrozenModel):
    x_um: NonNegativeUm = 0
    y_um: NonNegativeUm = 0
    z_um: NonNegativeUm = 0


class Placement(FrozenModel):
    x_um: NonNegativeUm = 0
    y_um: NonNegativeUm = 0
    z_um: NonNegativeUm = 0
    rotation_x_mdeg: int = Field(default=0, strict=True, ge=-360_000, le=360_000)
    rotation_y_mdeg: int = Field(default=0, strict=True, ge=-360_000, le=360_000)
    rotation_z_mdeg: int = Field(default=0, strict=True, ge=-360_000, le=360_000)


class FeatureDimensions(FrozenModel):
    diameter_um: PositiveUm | None = None
    depth_um: PositiveUm | None = None
    width_um: PositiveUm | None = None
    length_um: PositiveUm | None = None
    radius_um: PositiveUm | None = None

    @model_validator(mode="after")
    def at_least_one_dimension(self) -> FeatureDimensions:
        if all(
            value is None
            for value in (
                self.diameter_um,
                self.depth_um,
                self.width_um,
                self.length_um,
                self.radius_um,
            )
        ):
            raise ValueError("a manufacturing feature must have a dimension")
        return self


class ManufacturingFeature(FrozenModel):
    feature_id: str = Field(min_length=8, max_length=64)
    part_id: str = Field(min_length=8, max_length=64)
    joint_id: str | None = Field(default=None, min_length=8, max_length=64)
    kind: FeatureKind
    face: FaceName
    origin: Point3D
    dimensions: FeatureDimensions
    pattern_count: PositiveInt = 1
    pitch_um: PositiveUm | None = None
    through: bool = False
    tolerance_um: NonNegativeUm = mm("0.15")
    fit_clearance_um: NonNegativeUm = 0
    corner_strategy: StableKey | None = None
    requires_square_corners: bool = False

    @model_validator(mode="after")
    def validate_pattern(self) -> ManufacturingFeature:
        if self.pattern_count > 1 and self.pitch_um is None:
            raise ValueError("a feature pattern with multiple items needs a pitch")
        return self


class EdgeBand(FrozenModel):
    edge: FaceName
    thickness_um: PositiveUm


class PartInstance(FrozenModel):
    part_id: str = Field(min_length=8, max_length=64)
    semantic_key: StableKey
    role: PartRole
    instance_index: NonNegativeInt
    revision: PositiveInt
    finished_size: Dimensions3D
    raw_size: Dimensions3D
    placement: Placement
    material_id: StableKey
    material_version: StableKey
    actual_thickness_um: PositiveUm
    grain_direction: GrainDirection
    a_side: FaceName = FaceName.A
    b_side: FaceName = FaceName.B
    named_edges: tuple[FaceName, ...] = (
        FaceName.FRONT,
        FaceName.BACK,
        FaceName.LEFT,
        FaceName.RIGHT,
    )
    visible_faces: tuple[FaceName, ...] = ()
    edge_bands: tuple[EdgeBand, ...] = ()
    features: tuple[ManufacturingFeature, ...] = ()
    weight_g: PositiveInt

    @model_validator(mode="after")
    def feature_ownership(self) -> PartInstance:
        if any(feature.part_id != self.part_id for feature in self.features):
            raise ValueError("part contains a feature belonging to another part")
        if len({feature.feature_id for feature in self.features}) != len(self.features):
            raise ValueError("part contains duplicate feature IDs")
        return self


class JointMember(FrozenModel):
    part_id: str = Field(min_length=8, max_length=64)
    mating_face: FaceName
    feature_ids: tuple[str, ...]


class Joint(FrozenModel):
    """Paired feature relationship.

    ``assembly_direction`` is the canonical mating axis relative to the first
    joint member.  The operator's actual movement is authoritatively represented
    by ``AssemblyStep.motion_path`` and its explicit moving group.
    """

    joint_id: str = Field(min_length=8, max_length=64)
    joint_type: JointType
    members: tuple[JointMember, ...] = Field(min_length=2, max_length=2)
    mating_origin: Point3D
    assembly_direction: AssemblyDirection
    hardware_sku: StableKey | None = None
    hardware_count: NonNegativeInt = 0
    tolerance_um: NonNegativeUm = mm("0.2")

    @model_validator(mode="after")
    def distinct_members(self) -> Joint:
        if self.members[0].part_id == self.members[1].part_id:
            raise ValueError("a joint must connect two distinct part instances")
        return self


class AssemblyNode(FrozenModel):
    part_id: str
    semantic_key: StableKey


class AssemblyEdge(FrozenModel):
    joint_id: str
    from_part_id: str
    to_part_id: str


class AssemblyStep(FrozenModel):
    """One deterministic operator action derived from the assembly graph."""

    step_number: PositiveInt
    step_id: str
    part_ids: tuple[str, ...]
    moving_part_ids: tuple[str, ...] = Field(min_length=1)
    joint_ids: tuple[str, ...]
    direction: AssemblyDirection
    motion_path: tuple[AssemblyDirection, ...] = Field(min_length=1)
    tool_ids: tuple[StableKey, ...] = ()
    checkpoint: str

    @model_validator(mode="after")
    def moving_group_belongs_to_step(self) -> AssemblyStep:
        if len(self.moving_part_ids) != len(set(self.moving_part_ids)):
            raise ValueError("assembly moving group contains duplicate parts")
        if not set(self.moving_part_ids) <= set(self.part_ids):
            raise ValueError("assembly moving group must be contained in the step parts")
        if self.motion_path[-1] != self.direction:
            raise ValueError("assembly direction must equal the final motion-path segment")
        return self


class AssemblyGraph(FrozenModel):
    nodes: tuple[AssemblyNode, ...]
    edges: tuple[AssemblyEdge, ...]
    steps: tuple[AssemblyStep, ...]

    @model_validator(mode="after")
    def graph_references_exist(self) -> AssemblyGraph:
        node_ids = {node.part_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("assembly graph has duplicate nodes")
        edge_ids = {edge.joint_id for edge in self.edges}
        if len(edge_ids) != len(self.edges):
            raise ValueError("assembly graph has duplicate joint edges")
        for edge in self.edges:
            if edge.from_part_id not in node_ids or edge.to_part_id not in node_ids:
                raise ValueError("assembly edge references a missing part")
        expected_steps = tuple(range(1, len(self.steps) + 1))
        if tuple(step.step_number for step in self.steps) != expected_steps:
            raise ValueError("assembly steps must be sequential and one-based")
        if any(part_id not in node_ids for step in self.steps for part_id in step.part_ids):
            raise ValueError("assembly step references a missing part")
        if any(joint_id not in edge_ids for step in self.steps for joint_id in step.joint_ids):
            raise ValueError("assembly step references a missing joint")
        step_joint_ids = [joint_id for step in self.steps for joint_id in step.joint_ids]
        if len(step_joint_ids) != len(set(step_joint_ids)):
            raise ValueError("each assembly joint must be installed exactly once")
        if set(step_joint_ids) != edge_ids:
            raise ValueError("assembly steps must install every joint")
        step_part_ids = {part_id for step in self.steps for part_id in step.part_ids}
        if step_part_ids != node_ids:
            raise ValueError("assembly steps must use every part exactly as a BOM item")
        return self


class DesignResult(FrozenModel):
    design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_version: StableKey
    template_version: StableKey
    spec: BookcaseDesignSpec
    parts: tuple[PartInstance, ...]
    joints: tuple[Joint, ...]
    assembly_graph: AssemblyGraph
    total_weight_g: PositiveInt

    @model_validator(mode="after")
    def validate_result_integrity(self) -> DesignResult:
        part_ids = {part.part_id for part in self.parts}
        if len(part_ids) != len(self.parts):
            raise ValueError("design result has duplicate part IDs")
        joint_ids = {joint.joint_id for joint in self.joints}
        if len(joint_ids) != len(self.joints):
            raise ValueError("design result has duplicate joint IDs")
        if {node.part_id for node in self.assembly_graph.nodes} != part_ids:
            raise ValueError("assembly graph and part collection differ")
        if {edge.joint_id for edge in self.assembly_graph.edges} != joint_ids:
            raise ValueError("assembly graph and joint collection differ")
        feature_ids = {feature.feature_id for part in self.parts for feature in part.features}
        for joint in self.joints:
            referenced = {
                feature_id for member in joint.members for feature_id in member.feature_ids
            }
            if not referenced <= feature_ids:
                raise ValueError("joint references a missing manufacturing feature")
        if sum(part.weight_g for part in self.parts) != self.total_weight_g:
            raise ValueError("total design weight does not equal the part weights")
        return self
