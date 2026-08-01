from __future__ import annotations

from enum import StrEnum


class PartRole(StrEnum):
    LEFT_SIDE = "left_side"
    RIGHT_SIDE = "right_side"
    TOP = "top"
    BOTTOM = "bottom"
    SHELF = "shelf"
    BACK = "back"
    PLINTH = "plinth"
    DIVIDER = "divider"


class FeatureKind(StrEnum):
    DRILL = "drill"
    SERIES_DRILL = "drill_pattern"
    COUNTERSINK = "countersink"
    POCKET = "pocket"
    GROOVE = "groove"
    RABBET = "rabbet"
    TENON = "tenon"
    EDGE_RELIEF = "edge_relief"
    OUTER_CONTOUR = "outer_contour"
    MARK = "mark"


class JointType(StrEnum):
    DOWEL = "dowel"
    CONFIRMAT = "confirmat"
    CAM_DOWEL = "cam_dowel"
    SHELF_PIN = "shelf_pin"
    RABBET = "rabbet"
    DADO = "dado"
    TENON = "tenon"


class FaceName(StrEnum):
    A = "a"
    B = "b"
    FRONT = "front"
    BACK = "back"
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class GrainDirection(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"
    NONE = "none"


class ShelfMount(StrEnum):
    FIXED = "fixed"
    ADJUSTABLE = "adjustable"


class BackPanelType(StrEnum):
    NONE = "none"
    SURFACE_MOUNTED = "surface_mounted"
    INSET_GROOVE = "inset_groove"


class ReinforcementMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class MaterialType(StrEnum):
    SHEET_GOOD = "sheet_good"
    SOLID_WOOD = "solid_wood"


class AssemblyDirection(StrEnum):
    POS_X = "+x"
    NEG_X = "-x"
    POS_Y = "+y"
    NEG_Y = "-y"
    POS_Z = "+z"
    NEG_Z = "-z"


class DesignStatus(StrEnum):
    CONCEPT = "concept"
    DRAFT = "draft"
    DESIGN_VALIDATED = "design_validated"
    CAM_VALIDATED = "cam_validated"
    APPROVED = "approved"
    RELEASED = "released"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
