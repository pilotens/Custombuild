from importlib.util import find_spec
from math import pi

import pytest
from custombuild_cad.adapter import UnsupportedCADFeatureError
from custombuild_cad.measured_reference import reference_solid
from custombuild_domain.measured_assembly import MeasuredPart

pytestmark = pytest.mark.skipif(find_spec("cadquery") is None, reason="Native CAD required")


@pytest.mark.parametrize(
    "axes,size",
    [
        ("xy", [100000, 60000, 10000]),
        ("xz", [100000, 10000, 60000]),
        ("yz", [10000, 100000, 60000]),
    ],
)
def test_true_circular_arcs_have_the_same_volume_in_all_three_profile_planes(axes, size):
    part = MeasuredPart(
        id="p",
        code="P",
        name="Prov",
        size_um=size,
        blank_lbt_um=size,
        origin_um=[0, 0, 0],
        material="Prov",
        profile=dict(kind="rounded_rectangle", axes=axes, radius_um=10000, source="Prov"),
    )
    solid = reference_solid(part)
    assert solid.isValid()
    assert solid.Volume() == pytest.approx((100 * 60 - (4 - pi) * 100) * 10, abs=1e-6)


@pytest.mark.parametrize("fault", ["operation", "origin", "frame", "capsule"])
def test_reference_cad_cannot_silently_drop_operations_or_unknown_placement(fault):
    data = dict(
        id="p",
        code="P",
        name="Prov",
        size_um=[100000, 60000, 10000],
        blank_lbt_um=[100000, 60000, 10000],
        origin_um=[0, 0, 0],
        material="Prov",
    )
    if fault == "operation":
        data["operations"] = [
            dict(
                id="hole",
                shape="drill",
                face="top",
                axes="xy",
                u_um=20000,
                v_um=20000,
                width_um=4000,
                height_um=4000,
                depth_um=5000,
                source="Prov",
            )
        ]
    elif fault == "origin":
        data["origin_um"] = None
    elif fault == "frame":
        data["frame_id"] = "local"
    else:
        data["profile"] = dict(kind="rounded_rectangle", axes="xy", radius_um=30000, source="Prov")
    with pytest.raises(UnsupportedCADFeatureError):
        reference_solid(MeasuredPart.model_validate(data))
