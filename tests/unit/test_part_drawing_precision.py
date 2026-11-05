from dataclasses import replace
from decimal import Decimal
from xml.etree import ElementTree

from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_domain_part
from custombuild_manufacturing.exporters import dxf_for_part, svg_for_part
from custombuild_manufacturing.model import FeatureKind


def test_delivered_drill_radius_keeps_half_micrometre_in_svg_and_dxf():
    design = FurnitureWorkspace.model_validate({"design": {"intent": {"family": "chair"}}})
    seat = next(p for p in build_furniture(design.design).parts if p.semantic_key == "chair-seat")
    part = adapt_domain_part(seat)
    drill = next(f for f in part.features if f.kind == FeatureKind.DRILL)
    adjusted = replace(part, features=(replace(drill, diameter_um=5501),))
    svg = ElementTree.fromstring(svg_for_part(adjusted, drill.side))  # noqa: S314
    circle = next(c for c in svg.findall(".//{*}circle") if c.get("class") == "drill")
    assert Decimal(circle.attrib["r"]) == Decimal("2.7505")
    tags = dxf_for_part(adjusted, drill.side).decode().splitlines()
    pairs = list(zip(tags[::2], tags[1::2], strict=True))
    assert ("40", "2.7505") in pairs
