from __future__ import annotations

import pytest
from app import models  # noqa: F401
from app.db import Base
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

PROJECT_CHILDREN = (
    "design_versions",
    "designs",
    "external_evidence",
    "imported_assets",
)

PRODUCTION_TABLES = (
    "assembly_graphs",
    "assembly_steps",
    "bom_lines",
    "cam_operations",
    "constraints",
    "cut_list_lines",
    "load_cases",
    "manufacturing_features",
    "nesting_layouts",
    "parameter_definitions",
    "parameter_values",
    "part_faces",
    "parts",
    "rule_evaluations",
    "setups",
    "toolpaths",
)


def _tenant_foreign_key(
    table_name: str,
    child_column: str,
    parent_table: str,
) -> ForeignKeyConstraint:
    table = Base.metadata.tables[table_name]
    matches = [
        constraint
        for constraint in table.foreign_key_constraints
        if tuple(constraint.column_keys) == ("organization_id", child_column)
        and tuple(element.target_fullname for element in constraint.elements)
        == (
            f"{parent_table}.organization_id",
            f"{parent_table}.id",
        )
    ]
    legacy_single_column_matches = [
        constraint
        for constraint in table.foreign_key_constraints
        if tuple(constraint.column_keys) == (child_column,)
        and tuple(element.target_fullname for element in constraint.elements)
        == (f"{parent_table}.id",)
    ]
    assert len(matches) == 1
    assert legacy_single_column_matches == []
    return matches[0]


@pytest.mark.parametrize("table_name", PROJECT_CHILDREN)
def test_project_children_bind_the_project_id_to_the_same_tenant(table_name: str) -> None:
    constraint = _tenant_foreign_key(table_name, "project_id", "projects")

    assert constraint.name == f"fk_{table_name}_org_project"
    assert constraint.ondelete == "CASCADE"


@pytest.mark.parametrize(
    "table_name",
    (*PRODUCTION_TABLES, "generation_jobs", "releases", "approvals"),
)
def test_production_children_bind_the_design_version_to_the_same_tenant(
    table_name: str,
) -> None:
    constraint = _tenant_foreign_key(
        table_name,
        "design_version_id",
        "design_versions",
    )

    assert constraint.name == f"fk_{table_name}_org_design_version"
    assert constraint.ondelete == "CASCADE"


@pytest.mark.parametrize("table_name", ("artifacts", "approvals"))
def test_job_children_bind_the_generation_job_to_the_same_tenant(
    table_name: str,
) -> None:
    constraint = _tenant_foreign_key(
        table_name,
        "generation_job_id",
        "generation_jobs",
    )

    assert constraint.name == f"fk_{table_name}_org_generation_job"
    assert constraint.ondelete == "CASCADE"


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    (
        ("projects", "uq_projects_org_id"),
        ("design_versions", "uq_design_versions_org_id"),
        ("generation_jobs", "uq_generation_jobs_org_id"),
    ),
)
def test_tenant_parent_keys_are_unique(table_name: str, constraint_name: str) -> None:
    constraints = [
        constraint
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns)
        == ("organization_id", "id")
    ]

    assert len(constraints) == 1
    assert constraints[0].name == constraint_name
