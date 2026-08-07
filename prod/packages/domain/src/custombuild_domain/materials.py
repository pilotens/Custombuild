from __future__ import annotations

from .enums import GrainDirection, MaterialType
from .models import MaterialVersion, PropertySource
from .units import mm


def screening_birch_plywood_18() -> MaterialVersion:
    """Conservative example data for screening, never a production certificate."""

    return MaterialVersion(
        material_id="birch-plywood",
        version="screening-2026.1",
        name="Björkplywood 18 mm – indikativa screeningvärden",
        material_type=MaterialType.SHEET_GOOD,
        nominal_thickness_um=mm(18),
        density_kg_m3=680,
        elastic_modulus_mpa=6_500,
        bending_strength_mpa=30,
        shear_strength_mpa=3,
        creep_factor_permille=500,
        property_uncertainty_permille=200,
        min_supported_thickness_um=mm(17),
        max_supported_thickness_um=mm(19),
        grain_direction=GrainDirection.X,
        source=PropertySource(
            source_id="screening-library",
            title="Custombuild conservative screening fixture; replace with supplier declaration",
            revision="2026.1",
            note="Not certified material data; batch verification is required before production.",
        ),
    )


def screening_birch_plywood_6() -> MaterialVersion:
    return MaterialVersion(
        material_id="birch-plywood-6",
        version="screening-2026.1",
        name="Björkplywood 6 mm – indikativa screeningvärden",
        material_type=MaterialType.SHEET_GOOD,
        nominal_thickness_um=mm(6),
        density_kg_m3=680,
        elastic_modulus_mpa=5_000,
        bending_strength_mpa=24,
        shear_strength_mpa=3,
        creep_factor_permille=500,
        property_uncertainty_permille=250,
        min_supported_thickness_um=mm("5.5"),
        max_supported_thickness_um=mm("6.5"),
        grain_direction=GrainDirection.X,
        source=PropertySource(
            source_id="screening-library",
            title="Custombuild conservative screening fixture; replace with supplier declaration",
            revision="2026.1",
            note="Not certified material data; batch verification is required before production.",
        ),
    )


def screening_mdf_18() -> MaterialVersion:
    return MaterialVersion(
        material_id="mdf",
        version="screening-2026.1",
        name="MDF 18 mm – indikativa screeningvärden",
        material_type=MaterialType.SHEET_GOOD,
        nominal_thickness_um=mm(18),
        density_kg_m3=750,
        elastic_modulus_mpa=3_000,
        bending_strength_mpa=18,
        shear_strength_mpa=2,
        creep_factor_permille=800,
        property_uncertainty_permille=250,
        min_supported_thickness_um=mm(17),
        max_supported_thickness_um=mm(19),
        grain_direction=GrainDirection.NONE,
        source=PropertySource(
            source_id="screening-library",
            title="Custombuild conservative screening fixture; replace with supplier declaration",
            revision="2026.1",
            note="Not certified material data; batch verification is required before production.",
        ),
    )


def screening_mdf_6() -> MaterialVersion:
    return MaterialVersion(
        material_id="mdf-6",
        version="screening-2026.1",
        name="MDF 6 mm – indikativa screeningvärden",
        material_type=MaterialType.SHEET_GOOD,
        nominal_thickness_um=mm(6),
        density_kg_m3=780,
        elastic_modulus_mpa=2_500,
        bending_strength_mpa=15,
        shear_strength_mpa=2,
        creep_factor_permille=800,
        property_uncertainty_permille=300,
        min_supported_thickness_um=mm("5.5"),
        max_supported_thickness_um=mm("6.5"),
        grain_direction=GrainDirection.NONE,
        source=PropertySource(
            source_id="screening-library",
            title="Custombuild conservative screening fixture; replace with supplier declaration",
            revision="2026.1",
            note="Not certified material data; batch verification is required before production.",
        ),
    )
