from math import pi

import pytest
from custombuild_cad.adapter import CADExportError


@pytest.fixture
def order_step(tmp_path):
    cq = pytest.importorskip("cadquery")
    blank = cq.Solid.makeBox(60, 25, 12)
    bore = cq.Solid.makeCylinder(4, 5, cq.Vector(30, 12, 7))
    order = cq.Assembly(name="order")
    order.add(blank.cut(bore), name="panel", loc=cq.Location(cq.Vector(14, 25, 31)))
    order.add(cq.Solid.makeBox(7, 9, 3), name="back", loc=cq.Location(cq.Vector(18, 66, 32)))
    order.add(blank, name="neighbour")
    path = tmp_path / "source.step"
    order.export(str(path), exportType="STEP")
    return path.read_bytes()


def test_bench_step_preserves_the_installed_datum_and_blind_bore(order_step, tmp_path):
    import cadquery as cq
    from custombuild_cad.bookcase_sample import sample_step

    path = tmp_path / "download.step"
    path.write_bytes(sample_step(order_step, {"back", "panel"}))
    kit = cq.Assembly.load(str(path))
    assert {child.name for child in kit.children} == {"back", "panel"}
    panel = kit.objects["panel"].toCompound().Solids()[0]
    box = panel.BoundingBox()
    assert (box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax) == pytest.approx(
        (14, 25, 31, 74, 50, 43), abs=1e-6
    )
    assert panel.Volume() == pytest.approx(60 * 25 * 12 - pi * 4**2 * 5, abs=1e-6)
    assert panel.isInside((44, 37, 37.9))
    assert not panel.isInside((44, 37, 38.1))


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing", "saknar en vald orderdel"),
        ("shifted", "ändrad geometri"),
        ("filled", "ändrad geometri"),
    ],
)
def test_bench_step_rejects_damage_in_the_written_file(order_step, monkeypatch, damage, message):
    import cadquery as cq
    from custombuild_cad.bookcase_sample import sample_step

    export = cq.Assembly.export

    def damaged_export(kit, path, **kwargs):
        changed = cq.Assembly(name=kit.name)
        changed.add(kit.objects["back"].toCompound(), name="back")
        if damage == "shifted":
            changed.add(kit.objects["panel"].toCompound().translate((1, 0, 0)), name="panel")
        elif damage == "filled":
            changed.add(cq.Solid.makeBox(60, 25, 12, cq.Vector(14, 25, 31)), name="panel")
        return export(changed, path, **kwargs)

    monkeypatch.setattr(cq.Assembly, "export", damaged_export)
    with pytest.raises(CADExportError, match=message):
        sample_step(order_step, {"panel", "back"})
