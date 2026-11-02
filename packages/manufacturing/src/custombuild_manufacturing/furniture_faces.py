"""Physical face views for furniture review; the two-sided CAM contract stays separate."""

from dataclasses import dataclass, replace

from custombuild_domain.enums import FaceName
from custombuild_domain.models import PartInstance

from .adapters import adapt_domain_feature
from .model import PanelAxisMapping, PartSpec, Side


@dataclass(frozen=True)
class DrawingFace:
    name: str
    part: PartSpec
    side: Side


def drawing_faces(source: PartInstance, part: PartSpec) -> tuple[DrawingFace, ...]:
    primary = replace(part, features=tuple(f for f in part.features if f.side != Side.EDGE))
    views = [DrawingFace(side.value, primary, side) for side in (Side.A, Side.B)]
    size = source.finished_size
    dimensions = {"x": size.width_um, "y": size.depth_um, "z": size.height_um}
    for neg, pos, mapping in (
        (FaceName.LEFT, FaceName.RIGHT, PanelAxisMapping("y", "z", "x")),
        (FaceName.FRONT, FaceName.BACK, PanelAxisMapping("x", "z", "y")),
        (FaceName.BOTTOM, FaceName.TOP, PanelAxisMapping("x", "y", "z")),
    ):
        for face, side in ((neg, Side.B), (pos, Side.A)):
            if face in (source.a_side, source.b_side):
                continue
            features = tuple(
                adapt_domain_feature(
                    f, mapping=mapping, a_side=pos.value.upper(), b_side=neg.value.upper()
                )
                for f in source.features
                if f.face == face
            )
            if not features:
                continue
            view = replace(
                part,
                width_um=dimensions[mapping.u_axis],
                height_um=dimensions[mapping.v_axis],
                thickness_um=dimensions[mapping.thickness_axis],
                raw_width_um=dimensions[mapping.u_axis],
                raw_height_um=dimensions[mapping.v_axis],
                grain_direction="NONE",
                axis_mapping=mapping,
                edge_bands=(),
                edge_band_details=(),
                features=features,
                metadata={
                    **part.metadata,
                    "domain_a_side": pos.value.upper(),
                    "domain_b_side": neg.value.upper(),
                    "auxiliary_view": True,
                },
            )
            views.append(DrawingFace(face.value.upper(), view, side))
    return tuple(views)
