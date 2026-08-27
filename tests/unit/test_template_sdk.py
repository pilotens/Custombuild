from __future__ import annotations

from custombuild_templates import load_bookcase_template


def test_bookcase_template_is_versioned_and_complete() -> None:
    template = load_bookcase_template()
    assert template.template_id == "bookcase"
    assert template.template_version == "1.1.0"
    assert template.persisted_unit == "um"
    assert {item.role for item in template.components} >= {
        "left_side",
        "right_side",
        "top",
        "bottom",
        "shelf",
        "back",
        "plinth",
        "divider",
        "base_side",
        "base_bottom",
        "cabinet_front",
    }
    assert {item.rule_id for item in template.rules} == {
        "CB-DEFLECTION-001",
        "CB-BENDING-001",
        "CB-JOINT-001",
        "CB-STABILITY-001",
        "CB-TIP-001",
        "CB-SUPPORT-001",
        "CB-HARDWARE-001",
    }
    assert {item.version for item in template.rules} == {"1.3.0"}
    assert template.joint_systems == ("dado",)
    joint_parameter = next(item for item in template.parameters if item.key == "joint_system")
    assert joint_parameter.enum_values == ("dado",)
