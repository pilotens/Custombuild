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
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMethod,
    JointType,
    MaterialType,
    OpenEndRelief,
    PartRole,
    ReinforcementMode,
    ShelfMount,
)
from .identity import content_hash
from .units import mm

PositiveUm = Annotated[int, Field(strict=True, gt=0)]
NonNegativeUm = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
RatioPpm = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
StableKey = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
RetentionSafetyFactorPermille = Annotated[
    int,
    Field(strict=True, ge=1_000, le=100_000),
]
BookcaseWidthUm = Annotated[int, Field(strict=True, ge=mm(250), le=mm(6_000))]
BookcaseHeightUm = Annotated[int, Field(strict=True, ge=mm(300), le=mm(4_000))]
BookcaseDepthUm = Annotated[int, Field(strict=True, ge=mm(100), le=mm(1_200))]
BookcaseShelfCount = Annotated[int, Field(strict=True, ge=0, le=40)]
BookcaseShelfLoadN = Annotated[int, Field(strict=True, ge=0, le=5_000)]
BookcaseDividerCount = Annotated[int, Field(strict=True, ge=0, le=16)]
BaseCabinetHeightUm = Annotated[int, Field(strict=True, ge=0, le=mm(2_000))]
BaseCabinetDepthUm = Annotated[int, Field(strict=True, ge=0, le=mm(1_200))]
BaseCabinetCount = Annotated[int, Field(strict=True, ge=0, le=17)]

BOOKCASE_ENGINE_VERSION = "0.7.0"
BOOKCASE_TEMPLATE_VERSION = "2.0.0"


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


class JointRetentionMaterialIdentity(FrozenModel):
    """Exact material/version pair covered by retention evidence."""

    material_id: StableKey
    material_version: StableKey


class JointRetentionLoadCase(FrozenModel):
    """One evidence-backed retention capacity for an explicit load mode."""

    mode: JointRetentionLoadMode
    rated_design_load_n: PositiveInt
    verified_capacity_n: PositiveInt


class JointRetentionContract(FrozenModel):
    """Foundation for a future authenticated joint-retention selection.

    Validation proves structural completeness, exact application geometry and
    conservative capacity arithmetic. It does *not* authenticate the source of
    the catalogue/evidence bytes. The public preview API intentionally exposes
    no field for this model; a future server-side trust root must verify those
    bytes before freezing this payload. It never authorizes physical work.
    """

    system_id: StableKey
    system_version: StableKey
    joint_type: JointType = JointType.DADO
    method: JointRetentionMethod
    catalog_entry_sha256: Sha256Hex
    evidence_id: StableKey
    evidence_sha256: Sha256Hex
    installation_instruction_id: StableKey
    installation_instruction_version: StableKey
    installation_instruction_sha256: Sha256Hex
    machining_scope: JointRetentionMachiningScope
    hardware_sku: StableKey
    hardware_count_per_joint: PositiveInt
    applicable_materials: tuple[JointRetentionMaterialIdentity, ...] = Field(min_length=1)
    joint_geometry_sha256: Sha256Hex
    minimum_applicable_thickness_um: PositiveUm
    maximum_applicable_thickness_um: PositiveUm
    load_cases: tuple[JointRetentionLoadCase, ...] = Field(min_length=2, max_length=2)
    safety_factor_permille: RetentionSafetyFactorPermille
    bound_feature_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_machining_scope(self) -> JointRetentionContract:
        material_keys = tuple(
            (item.material_id, item.material_version) for item in self.applicable_materials
        )
        if material_keys != tuple(sorted(set(material_keys))):
            raise ValueError(
                "joint-retention applicable materials must be sorted and unique"
            )
        if self.minimum_applicable_thickness_um > self.maximum_applicable_thickness_um:
            raise ValueError("joint-retention thickness range is inverted")
        load_modes = tuple(item.mode for item in self.load_cases)
        if load_modes != (
            JointRetentionLoadMode.SHEAR,
            JointRetentionLoadMode.WITHDRAWAL,
        ):
            raise ValueError(
                "joint-retention load cases must contain canonical shear and withdrawal modes"
            )
        for load_case in self.load_cases:
            if (
                load_case.verified_capacity_n * 1_000
                < load_case.rated_design_load_n * self.safety_factor_permille
            ):
                raise ValueError(
                    "verified joint-retention capacity does not meet the declared design "
                    "load and safety factor"
                )
        feature_ids = self.bound_feature_ids
        if len(feature_ids) != len(set(feature_ids)) or any(not item for item in feature_ids):
            raise ValueError("joint-retention feature IDs must be unique and non-blank")
        if self.machining_scope == JointRetentionMachiningScope.FEATURES_BOUND_TO_JOINT:
            if not feature_ids:
                raise ValueError(
                    "feature-bound joint retention requires at least one manufacturing feature ID"
                )
        elif feature_ids:
            raise ValueError(
                "joint-retention feature IDs require the features-bound machining scope"
            )
        if (
            self.method == JointRetentionMethod.DRY_SELF_LOCKING
            and self.machining_scope != JointRetentionMachiningScope.FEATURES_BOUND_TO_JOINT
        ):
            raise ValueError(
                "dry self-locking retention requires explicit joint-bound manufacturing features"
            )
        return self


class BookcaseParameters(FrozenModel):
    width_um: BookcaseWidthUm = mm(900)
    height_um: BookcaseHeightUm = mm(2_000)
    depth_um: BookcaseDepthUm = mm(320)
    nominal_thickness_um: PositiveUm = mm(18)
    actual_thickness_um: PositiveUm = mm(18)
    shelf_count: BookcaseShelfCount = 5
    shelf_mount: ShelfMount = ShelfMount.FIXED
    shelf_load_n: BookcaseShelfLoadN = 300
    vertical_divider_count: BookcaseDividerCount = 0
    bay_width_ratios_ppm: tuple[RatioPpm, ...] = ()
    shelf_height_ratios_ppm: tuple[RatioPpm, ...] = ()
    base_cabinet_height_um: BaseCabinetHeightUm = 0
    base_cabinet_depth_um: BaseCabinetDepthUm = 0
    base_cabinet_count: BaseCabinetCount = 0
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
        if self.bay_width_ratios_ppm:
            if len(self.bay_width_ratios_ppm) != bay_count:
                raise ValueError("bay width ratios must match the number of bays")
            ratio_total = sum(self.bay_width_ratios_ppm)
            if any(value * 100 < ratio_total * 8 for value in self.bay_width_ratios_ppm):
                raise ValueError("every custom bay must be at least 8 percent of the inner width")
        if self.shelf_height_ratios_ppm:
            if len(self.shelf_height_ratios_ppm) != self.shelf_count:
                raise ValueError("shelf height ratios must match the shelf count")
            if any(
                value < 50_000
                or value > 950_000
                or (index > 0 and value - self.shelf_height_ratios_ppm[index - 1] < 50_000)
                for index, value in enumerate(self.shelf_height_ratios_ppm)
            ):
                raise ValueError(
                    "custom shelf centres must be ordered and separated by at least 5 percent"
                )
        minimum_shelf_width = 2 * self.shelf_side_clearance_um + mm(40)
        if inner_width // bay_count <= minimum_shelf_width:
            raise ValueError("divider layout leaves an unmanufacturable shelf width")
        shelf_zone_bottom = (
            self.plinth_height_um + self.base_cabinet_height_um
            if self.base_cabinet_count
            else self.plinth_height_um + t
        )
        shelf_zone_height = self.height_um - t - shelf_zone_bottom
        if shelf_zone_height - self.shelf_count * t < (self.shelf_count + 1) * mm(40):
            raise ValueError("shelf layout leaves an unmanufacturable opening height")
        if self.base_cabinet_count:
            if self.base_cabinet_height_um < mm(300):
                raise ValueError("base cabinet height must be at least 300 mm")
            if self.base_cabinet_depth_um != self.depth_um:
                raise ValueError("base cabinet depth must equal the furniture depth")
            if self.base_cabinet_height_um >= self.height_um - t - mm(200):
                raise ValueError("base cabinet leaves no usable upper shelving zone")
            base_opening = (
                self.width_um - (self.base_cabinet_count + 1) * t
            ) // self.base_cabinet_count
            if base_opening < mm(200):
                raise ValueError("base cabinet layout leaves an unmanufacturable module width")
        elif self.base_cabinet_height_um or self.base_cabinet_depth_um:
            raise ValueError("base cabinet dimensions require at least one cabinet module")
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

    def assert_furniture_family(self, furniture_type: str) -> BookcaseParameters:
        """Reject family-incoherent parameters without normalizing or mutating them."""

        if furniture_type == "bookcase":
            if (
                self.base_cabinet_count != 0
                or self.base_cabinet_height_um != 0
                or self.base_cabinet_depth_um != 0
            ):
                raise ValueError("bookcase furniture cannot contain base cabinets")
            return self
        if furniture_type == "wall_library":
            if self.base_cabinet_count < 1:
                raise ValueError("wall-library furniture requires at least one base cabinet")
            if self.base_cabinet_height_um < mm(300):
                raise ValueError("wall-library base cabinet height must be at least 300 mm")
            if self.base_cabinet_depth_um != self.depth_um:
                raise ValueError("wall-library base cabinet depth must equal furniture depth")
            if self.base_cabinet_height_um >= self.height_um - self.actual_thickness_um - mm(200):
                raise ValueError("wall-library base cabinet leaves no usable upper shelving zone")
            return self
        raise ValueError(f"unsupported furniture family: {furniture_type!r}")


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
    joint_retention: JointRetentionContract | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def reject_retired_compiler_versions(self) -> BookcaseDesignSpec:
        if (
            self.engine_version != BOOKCASE_ENGINE_VERSION
            or self.template_version != BOOKCASE_TEMPLATE_VERSION
        ):
            raise ValueError(
                "the requested engine/template version is retired; create new revision"
            )
        return self

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
        elif self.back_material is not None:
            raise ValueError("back material is forbidden when the design has no back panel")
        return self

    @model_validator(mode="after")
    def validate_joint_retention(self) -> BookcaseDesignSpec:
        retention = self.joint_retention
        if retention is None:
            return self
        parameters = self.parameters
        if retention.joint_type != JointType.DADO:
            raise ValueError(
                "the current bookcase retention contract applies only to DADO joints"
            )
        if (
            parameters.joint_system != JointType.DADO
            and parameters.back_panel != BackPanelType.INSET_GROOVE
        ):
            raise ValueError("the design contains no DADO joint requiring retention")
        if (
            retention.method != JointRetentionMethod.MECHANICAL
            or retention.machining_scope
            != JointRetentionMachiningScope.NO_ADDITIONAL_CNC
        ):
            raise ValueError(
                "the current bookcase template accepts only mechanical joint retention "
                "that requires no additional CNC features"
            )
        applicable_thicknesses = [parameters.actual_thickness_um]
        required_materials = {(self.material.material_id, self.material.version)}
        if parameters.back_panel == BackPanelType.INSET_GROOVE:
            applicable_thicknesses.append(parameters.back_thickness_um)
            if self.back_material is None:  # guarded by validate_material; keep fail-closed
                raise ValueError("inset back retention requires a versioned back material")
            required_materials.add(
                (self.back_material.material_id, self.back_material.version)
            )
        covered_materials = {
            (item.material_id, item.material_version)
            for item in retention.applicable_materials
        }
        if not required_materials <= covered_materials:
            raise ValueError(
                "joint-retention contract does not cover every DADO member material version"
            )
        if any(
            not retention.minimum_applicable_thickness_um
            <= thickness_um
            <= retention.maximum_applicable_thickness_um
            for thickness_um in applicable_thicknesses
        ):
            raise ValueError(
                "joint-retention contract does not cover every retained member thickness"
            )
        load_cases = {item.mode: item for item in retention.load_cases}
        required_loads = {
            JointRetentionLoadMode.SHEAR: max(parameters.shelf_load_n, 1),
            JointRetentionLoadMode.WITHDRAWAL: parameters.assumed_horizontal_force_n,
        }
        if any(
            load_cases[mode].rated_design_load_n < required_load_n
            for mode, required_load_n in required_loads.items()
        ):
            raise ValueError("joint-retention load case is below the bookcase design load")
        if retention.safety_factor_permille < parameters.structural_safety_factor_permille:
            raise ValueError(
                "joint-retention safety factor is below the bookcase design requirement"
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
    open_end_reliefs: tuple[OpenEndRelief, ...] = ()

    @model_validator(mode="after")
    def validate_pattern(self) -> ManufacturingFeature:
        if self.pattern_count > 1 and self.pitch_um is None:
            raise ValueError("a feature pattern with multiple items needs a pitch")
        if len(set(self.open_end_reliefs)) != len(self.open_end_reliefs):
            raise ValueError("open-end relief declarations must be unique")
        if self.open_end_reliefs and self.corner_strategy != "dogbone-v1":
            raise ValueError("open-end reliefs require the versioned dogbone-v1 strategy")
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
    retention: JointRetentionContract | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def distinct_members(self) -> Joint:
        if self.members[0].part_id == self.members[1].part_id:
            raise ValueError("a joint must connect two distinct part instances")
        if self.retention is not None:
            if self.retention.joint_type != self.joint_type:
                raise ValueError("joint retention must match its joint type")
            if self.hardware_sku != self.retention.hardware_sku:
                raise ValueError("joint hardware SKU must match its retention contract")
            if self.hardware_count != self.retention.hardware_count_per_joint:
                raise ValueError("joint hardware count must match its retention contract")
            referenced_features = {
                feature_id for member in self.members for feature_id in member.feature_ids
            }
            if not set(self.retention.bound_feature_ids) <= referenced_features:
                raise ValueError(
                    "joint retention references manufacturing features outside the joint"
                )
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


def dado_joint_geometry_fingerprint(
    parts: tuple[PartInstance, ...],
    joints: tuple[Joint, ...],
) -> str:
    """Hash exact DADO application geometry without retention or hardware fields."""

    part_by_id = {part.part_id: part for part in parts}
    feature_by_id = {
        feature.feature_id: feature for part in parts for feature in part.features
    }
    payload: list[dict[str, object]] = []
    for joint in sorted(
        (item for item in joints if item.joint_type == JointType.DADO),
        key=lambda item: item.joint_id,
    ):
        members: list[dict[str, object]] = []
        for member in joint.members:
            part = part_by_id[member.part_id]
            members.append(
                {
                    "part_id": member.part_id,
                    "material_id": part.material_id,
                    "material_version": part.material_version,
                    "actual_thickness_um": part.actual_thickness_um,
                    "mating_face": member.mating_face,
                    "feature_ids": member.feature_ids,
                    "features": tuple(
                        feature_by_id[feature_id].model_dump(mode="python")
                        for feature_id in member.feature_ids
                    ),
                }
            )
        payload.append(
            {
                "joint_id": joint.joint_id,
                "joint_type": joint.joint_type,
                "members": tuple(members),
                "mating_origin": joint.mating_origin,
                "assembly_direction": joint.assembly_direction,
                "tolerance_um": joint.tolerance_um,
            }
        )
    return content_hash(tuple(payload))


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
        part_by_id = {part.part_id: part for part in self.parts}
        part_ids = set(part_by_id)
        if len(part_ids) != len(self.parts):
            raise ValueError("design result has duplicate part IDs")
        joint_ids = {joint.joint_id for joint in self.joints}
        if len(joint_ids) != len(self.joints):
            raise ValueError("design result has duplicate joint IDs")
        if {node.part_id for node in self.assembly_graph.nodes} != part_ids:
            raise ValueError("assembly graph and part collection differ")
        if {edge.joint_id for edge in self.assembly_graph.edges} != joint_ids:
            raise ValueError("assembly graph and joint collection differ")
        features = tuple(feature for part in self.parts for feature in part.features)
        feature_ids = {feature.feature_id for feature in features}
        if len(feature_ids) != len(features):
            raise ValueError("design result has duplicate manufacturing feature IDs")
        feature_by_id = {feature.feature_id: feature for feature in features}
        referenced_features: dict[str, str] = {}
        for joint in self.joints:
            if joint.joint_type == JointType.DADO:
                if joint.retention != self.spec.joint_retention:
                    raise ValueError(
                        "every DADO joint must carry the frozen design retention contract"
                    )
            elif joint.retention is not None:
                raise ValueError("joint retention is bound to a non-DADO joint")
            if joint.retention is not None:
                applicable_materials = {
                    (item.material_id, item.material_version)
                    for item in joint.retention.applicable_materials
                }
                for member in joint.members:
                    member_part = part_by_id.get(member.part_id)
                    if member_part is None:
                        raise ValueError("joint references a missing part")
                    member_thickness_um = member_part.actual_thickness_um
                    if not (
                        joint.retention.minimum_applicable_thickness_um
                        <= member_thickness_um
                        <= joint.retention.maximum_applicable_thickness_um
                    ):
                        raise ValueError(
                            "joint retention does not cover a joint member's actual thickness"
                        )
                    if (
                        member_part.material_id,
                        member_part.material_version,
                    ) not in applicable_materials:
                        raise ValueError(
                            "joint retention does not cover a joint member's material version"
                        )
            for member in joint.members:
                if member.part_id not in part_ids:
                    raise ValueError("joint references a missing part")
                for feature_id in member.feature_ids:
                    feature = feature_by_id.get(feature_id)
                    if feature is None:
                        raise ValueError("joint references a missing manufacturing feature")
                    if feature.part_id != member.part_id:
                        raise ValueError("joint member references another part's feature")
                    if feature.joint_id != joint.joint_id:
                        raise ValueError("joint member references a feature owned by another joint")
                    previous_joint = referenced_features.setdefault(feature_id, joint.joint_id)
                    if previous_joint != joint.joint_id:
                        raise ValueError("manufacturing feature is referenced by multiple joints")
        for feature in features:
            if feature.joint_id is None:
                continue
            if feature.joint_id not in joint_ids:
                raise ValueError("manufacturing feature references a missing joint")
            if referenced_features.get(feature.feature_id) != feature.joint_id:
                raise ValueError("joint-owned manufacturing feature is absent from its joint")
        retention = self.spec.joint_retention
        if retention is not None and retention.joint_geometry_sha256 != (
            dado_joint_geometry_fingerprint(self.parts, self.joints)
        ):
            raise ValueError(
                "joint-retention contract does not match the exact DADO geometry fingerprint"
            )
        if sum(part.weight_g for part in self.parts) != self.total_weight_g:
            raise ValueError("total design weight does not equal the part weights")
        return self
