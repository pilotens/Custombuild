"""Machine-readable release readiness without pretending physical authorization."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from scripts.check_environment_isolation import DeploymentSurface, IsolationError, load_surface
except ModuleNotFoundError:  # Direct `python scripts/release_readiness.py` execution.
    from check_environment_isolation import (  # type: ignore[import-not-found,no-redef]
        DeploymentSurface,
        IsolationError,
        load_surface,
    )

try:
    from scripts.source_manifest import (
        PRODUCTION_SEMANTIC_ROOT_PATH,
        SourceManifestError,
        build_source_manifest,
        verify_production_semantic_root,
    )
except ModuleNotFoundError:  # Direct `python scripts/release_readiness.py` execution.
    from source_manifest import (  # type: ignore[import-not-found,no-redef]
        PRODUCTION_SEMANTIC_ROOT_PATH,
        SourceManifestError,
        build_source_manifest,
        verify_production_semantic_root,
    )

try:
    from scripts.deploy_descriptor import registry_overlay_issues
except ModuleNotFoundError:  # Direct `python scripts/release_readiness.py` execution.
    from deploy_descriptor import registry_overlay_issues  # type: ignore[import-not-found,no-redef]


class CheckStatus(str, Enum):
    PASS = "PASS"  # noqa: S105 - status label, not a credential.
    WARNING = "WARNING"
    BLOCK = "BLOCK"
    EXTERNAL_EVIDENCE_REQUIRED = "EXTERNAL_EVIDENCE_REQUIRED"


@dataclass(frozen=True)
class ReleaseCheck:
    code: str
    title: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, order=True)
class VulnerabilityExceptionKey:
    vulnerability: str
    package: str
    version: str
    package_type: str

    def label(self) -> str:
        return (
            f"{self.vulnerability} package={self.package!r} "
            f"version={self.version!r} type={self.package_type!r}"
        )


VULNERABILITY_EXCEPTION_SCHEMA = "custombuild.vulnerability-exceptions.v2"
VULNERABILITY_SEVERITIES = frozenset({"Negligible", "Low", "Medium", "High", "Critical"})
PRODUCTION_SEMANTIC_SOURCE_PATHS = (
    "packages/manufacturing/src/custombuild_manufacturing/package.py",
    "packages/manufacturing/src/custombuild_manufacturing/readiness.py",
    "services/worker/custombuild_worker/tasks.py",
    "services/api/app/api.py",
    "scripts/live_acceptance.py",
)
_STATIC_VALUE_MISSING = object()

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _static_ast_value(node: ast.expr | None) -> object:
    if node is None:
        return _STATIC_VALUE_MISSING
    try:
        value: object = ast.literal_eval(node)
    except (TypeError, ValueError):
        return _STATIC_VALUE_MISSING
    return value


def _module_assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    assignments: dict[str, list[ast.expr]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            assignments.setdefault(statement.target.id, []).append(statement.value)
    return assignments


def _resolved_static_value(
    node: ast.expr,
    assignments: dict[str, list[ast.expr]],
    *,
    seen: frozenset[str] | None = None,
) -> object:
    visited = seen or frozenset()
    if isinstance(node, ast.Name) and node.id not in visited:
        values = assignments.get(node.id, [])
        if len(values) == 1:
            return _resolved_static_value(values[0], assignments, seen=visited | {node.id})
    return _static_ast_value(node)


def _resolved_static_string_mapping(
    node: ast.expr,
    assignments: dict[str, list[ast.expr]],
) -> dict[str, str] | None:
    if not isinstance(node, ast.Dict):
        return None
    resolved: dict[str, str] = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            return None
        key = _resolved_static_value(key_node, assignments)
        value = _resolved_static_value(value_node, assignments)
        if not isinstance(key, str) or not isinstance(value, str) or key in resolved:
            return None
        resolved[key] = value
    return resolved


def _simple_function(tree: ast.Module, name: str) -> FunctionNode | None:
    functions = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef) and statement.name == name
    ]
    return functions[0] if len(functions) == 1 else None


def _function_bindings(function: FunctionNode) -> dict[str, ast.expr]:
    candidates: dict[str, list[ast.expr]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    candidates.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            candidates.setdefault(node.target.id, []).append(node.value)
    return {name: values[0] for name, values in candidates.items() if len(values) == 1}


def _resolved_binding(
    node: ast.expr,
    bindings: dict[str, ast.expr],
    *,
    seen: frozenset[str] | None = None,
) -> ast.expr:
    visited = seen or frozenset()
    if isinstance(node, ast.Name) and node.id not in visited and node.id in bindings:
        return _resolved_binding(bindings[node.id], bindings, seen=visited | {node.id})
    return node


def _call_name(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _expression_path(node: ast.expr) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _expression_path(node.value)
        return (*parent, node.attr) if parent is not None else None
    return None


def _mapping_access(
    node: ast.expr,
    owner: tuple[str, ...],
    key: str,
    bindings: dict[str, ast.expr] | None = None,
) -> bool:
    resolved = _resolved_binding(node, bindings or {})
    if (
        isinstance(resolved, ast.Call)
        and isinstance(resolved.func, ast.Attribute)
        and resolved.func.attr == "get"
        and _expression_path(resolved.func.value) == owner
        and resolved.args
    ):
        return _static_ast_value(resolved.args[0]) == key
    if isinstance(resolved, ast.Subscript) and _expression_path(resolved.value) == owner:
        return _static_ast_value(resolved.slice) == key
    return False


def _identity_comparison(
    node: ast.expr,
    *,
    owner: tuple[str, ...],
    key: str,
    expected: bool,
    operation: type[ast.cmpop],
    bindings: dict[str, ast.expr] | None = None,
) -> bool:
    resolved = _resolved_binding(node, bindings or {})
    if (
        not isinstance(resolved, ast.Compare)
        or len(resolved.ops) != 1
        or not isinstance(resolved.ops[0], operation)
        or len(resolved.comparators) != 1
    ):
        return False
    comparator = resolved.comparators[0]
    return (
        _mapping_access(resolved.left, owner, key, bindings)
        and _static_ast_value(comparator) is expected
    ) or (
        _static_ast_value(resolved.left) is expected
        and _mapping_access(comparator, owner, key, bindings)
    )


def _string_membership(
    node: ast.expr,
    *,
    owner: tuple[str, ...],
    key: str,
    expected: frozenset[str],
    bindings: dict[str, ast.expr] | None = None,
) -> bool:
    resolved = _resolved_binding(node, bindings or {})
    if (
        not isinstance(resolved, ast.Compare)
        or len(resolved.ops) != 1
        or not isinstance(resolved.ops[0], ast.In)
        or len(resolved.comparators) != 1
        or not _mapping_access(resolved.left, owner, key, bindings)
    ):
        return False
    values = _static_ast_value(resolved.comparators[0])
    if not isinstance(values, tuple | list | set | frozenset):
        return False
    return all(isinstance(value, str) for value in values) and frozenset(values) == expected


def _string_equality(
    node: ast.expr,
    *,
    owner: tuple[str, ...],
    key: str,
    expected: str,
    bindings: dict[str, ast.expr] | None = None,
) -> bool:
    resolved = _resolved_binding(node, bindings or {})
    if (
        not isinstance(resolved, ast.Compare)
        or len(resolved.ops) != 1
        or not isinstance(resolved.ops[0], ast.Eq)
        or len(resolved.comparators) != 1
    ):
        return False
    comparator = resolved.comparators[0]
    return (
        _mapping_access(resolved.left, owner, key, bindings)
        and _static_ast_value(comparator) == expected
    ) or (
        _static_ast_value(resolved.left) == expected
        and _mapping_access(comparator, owner, key, bindings)
    )


def _flatten_conjunction(node: ast.expr, bindings: dict[str, ast.expr]) -> list[ast.expr]:
    resolved = _resolved_binding(node, bindings)
    if isinstance(resolved, ast.BoolOp) and isinstance(resolved.op, ast.And):
        return [term for value in resolved.values for term in _flatten_conjunction(value, bindings)]
    return [resolved]


def _is_dfm_string_check(node: ast.expr, bindings: dict[str, ast.expr]) -> bool:
    resolved = _resolved_binding(node, bindings)
    type_node = (
        _resolved_binding(resolved.args[1], bindings)
        if isinstance(resolved, ast.Call) and len(resolved.args) == 2
        else None
    )
    return (
        isinstance(resolved, ast.Call)
        and _call_name(resolved) == "isinstance"
        and len(resolved.args) == 2
        and _mapping_access(resolved.args[0], ("result_json",), "dfm_status", bindings)
        and isinstance(type_node, ast.Name)
        and type_node.id == "str"
    )


def _safe_generation_predicate_return(node: ast.expr, bindings: dict[str, ast.expr]) -> bool:
    resolved = _resolved_binding(node, bindings)
    if _static_ast_value(resolved) is False:
        return True
    terms = _flatten_conjunction(resolved, bindings)
    return (
        any(
            _identity_comparison(
                term,
                owner=("result_json",),
                key="authoritative_geometry",
                expected=True,
                operation=ast.Is,
                bindings=bindings,
            )
            for term in terms
        )
        and any(_is_dfm_string_check(term, bindings) for term in terms)
        and any(
            _string_membership(
                term,
                owner=("result_json",),
                key="dfm_status",
                expected=frozenset({"PASS", "WARNING"}),
                bindings=bindings,
            )
            for term in terms
        )
    )


def _dict_literal_items(node: ast.Dict) -> dict[str, ast.expr] | None:
    items: dict[str, ast.expr] = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        key = _static_ast_value(key_node)
        if not isinstance(key, str) or key in items:
            return None
        items[key] = value_node
    return items


def _dict_literal_parts(node: ast.Dict) -> tuple[dict[str, ast.expr], list[ast.expr]] | None:
    items: dict[str, ast.expr] = {}
    expansions: list[ast.expr] = []
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            expansions.append(value_node)
            continue
        key = _static_ast_value(key_node)
        if not isinstance(key, str) or key in items:
            return None
        items[key] = value_node
    return items, expansions


def _simple_name_assignment(statement: ast.stmt) -> tuple[str, ast.expr] | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id, statement.value
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.value is not None
    ):
        return statement.target.id, statement.value
    return None


def _unique_top_level_return(function: FunctionNode) -> tuple[int, ast.Return] | None:
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    if len(returns) != 1 or returns[0] not in function.body:
        return None
    return function.body.index(returns[0]), returns[0]


def _top_level_assignments_to(function: FunctionNode, name: str) -> list[tuple[int, ast.expr]]:
    assignments: list[tuple[int, ast.expr]] = []
    for index, statement in enumerate(function.body):
        assignment = _simple_name_assignment(statement)
        if assignment is not None and assignment[0] == name:
            assignments.append((index, assignment[1]))
    return assignments


def _returned_dict_literal(function: FunctionNode) -> ast.Dict | None:
    returned = _unique_top_level_return(function)
    if returned is None:
        return None
    return_index, return_node = returned
    value = return_node.value
    if isinstance(value, ast.Dict):
        return value
    if not isinstance(value, ast.Name):
        return None
    assignments = _top_level_assignments_to(function, value.id)
    if len(assignments) != 1:
        return None
    assignment_index, assignment_value = assignments[0]
    if assignment_index + 1 != return_index or not isinstance(assignment_value, ast.Dict):
        return None
    return assignment_value


def _call_keywords(call: ast.Call) -> dict[str, ast.expr] | None:
    keywords: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in keywords:
            return None
        keywords[keyword.arg] = keyword.value
    return keywords


def _require_conditions(function: FunctionNode) -> list[ast.expr]:
    return [
        node.args[0]
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_name(node) == "require" and node.args
    ]


def _originates_from_access(
    node: ast.expr,
    *,
    owner: tuple[str, ...],
    key: str,
    bindings: dict[str, ast.expr],
) -> bool:
    resolved = _resolved_binding(node, bindings)
    return any(
        isinstance(candidate, ast.expr) and _mapping_access(candidate, owner, key, bindings)
        for candidate in ast.walk(resolved)
    )


def _parse_production_sources(repo: Path, issues: list[str]) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for relative in PRODUCTION_SEMANTIC_SOURCE_PATHS:
        path = repo / relative
        if not path.is_file():
            issues.append(f"{relative} is missing")
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(f"{relative} cannot be read as UTF-8: {type(exc).__name__}")
            continue
        try:
            trees[relative] = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            issues.append(f"{relative} is not valid Python: {exc.msg}")
    return trees


def _context_hash_expression_uses(node: ast.expr, context_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and _call_name(node) == "sha256_hex"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Call)
        and _call_name(node.args[0]) == "canonical_json_bytes"
        and len(node.args[0].args) == 1
        and not node.args[0].keywords
        and _expression_path(node.args[0].args[0]) == (context_name,)
    )


def _package_manifest_emitter_is_safe(tree: ast.Module) -> bool:
    builder = _simple_function(tree, "build_manifest")
    if builder is None:
        return False
    returned = _unique_top_level_return(builder)
    if returned is None:
        return False
    return_index, return_node = returned
    return_call = return_node.value
    if (
        not isinstance(return_call, ast.Call)
        or _call_name(return_call) != "canonical_json_bytes"
        or len(return_call.args) != 1
        or return_call.keywords
        or not isinstance(return_call.args[0], ast.Name)
    ):
        return False

    manifest_name = return_call.args[0].id
    manifest_assignments = _top_level_assignments_to(builder, manifest_name)
    if len(manifest_assignments) != 1:
        return False
    manifest_index, manifest_value = manifest_assignments[0]
    if manifest_index + 1 != return_index or not isinstance(manifest_value, ast.Dict):
        return False
    manifest_parts = _dict_literal_parts(manifest_value)
    if manifest_parts is None:
        return False
    manifest_items, expansions = manifest_parts
    if len(expansions) != 1 or not isinstance(expansions[0], ast.Name):
        return False

    context_name = expansions[0].id
    context_assignments = _top_level_assignments_to(builder, context_name)
    if len(context_assignments) != 1:
        return False
    context_index, context_value = context_assignments[0]
    if context_index + 1 != manifest_index or not isinstance(context_value, ast.Dict):
        return False
    context_items = _dict_literal_items(context_value)
    if context_items is None:
        return False

    assignments = _module_assignments(tree)
    expected_claims: dict[str, object] = {
        "release_scope": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }
    for name, expected in expected_claims.items():
        expression = context_items.get(name)
        if expression is None:
            return False
        actual = _resolved_static_value(expression, assignments)
        if isinstance(expected, bool) and actual is not expected:
            return False
        if not isinstance(expected, bool) and actual != expected:
            return False
    if expected_claims.keys() & manifest_items.keys():
        return False

    schema_expression = manifest_items.get("schema_version")
    context_hash_expression = manifest_items.get("production_context_hash")
    return (
        schema_expression is not None
        and _resolved_static_value(schema_expression, assignments)
        == "custombuild.production-manifest.v4"
        and context_hash_expression is not None
        and _context_hash_expression_uses(context_hash_expression, context_name)
    )


def _check_package_semantics(tree: ast.Module, relative: str, issues: list[str]) -> None:
    assignments = _module_assignments(tree)
    schema_values = assignments.get("PRODUCTION_MANIFEST_SCHEMA_VERSION", [])
    if (
        len(schema_values) != 1
        or _resolved_static_value(schema_values[0], assignments)
        != "custombuild.production-manifest.v4"
    ):
        issues.append(f"{relative} does not declare the production manifest v4 schema")

    field_values = assignments.get("MANIFEST_CONTEXT_HASH_FIELDS", [])
    fields = (
        _resolved_static_value(field_values[0], assignments)
        if len(field_values) == 1
        else _STATIC_VALUE_MISSING
    )
    required_fields = frozenset(
        {
            "domain_template_version",
            "template_capability_version",
            "template_capability_registry_version",
            "release_scope",
            "machine_use",
            "physical_cutting_authorized",
        }
    )
    if (
        not isinstance(fields, tuple | list | set | frozenset)
        or not all(isinstance(field, str) for field in fields)
        or not required_fields.issubset(fields)
    ):
        issues.append(f"{relative} does not bind all v4 safety fields into the context hash")
    if not _package_manifest_emitter_is_safe(tree):
        issues.append(f"{relative} build_manifest emitter can output unsafe production claims")


def _check_readiness_semantics(tree: ast.Module, relative: str, issues: list[str]) -> None:
    assignments = _module_assignments(tree)
    expected_constants = {
        "WORKSHOP_READINESS_SCHEMA_VERSION": "custombuild.workshop-readiness.v2",
        "DESIGN_REVIEW_RELEASE_SCOPE": "design_review",
        "VALIDATION_ONLY_MACHINE_USE": "validation_only",
    }
    for name, expected in expected_constants.items():
        values = assignments.get(name, [])
        if len(values) != 1 or _resolved_static_value(values[0], assignments) != expected:
            issues.append(f"{relative} has an unsafe or missing {name}")

    builder = _simple_function(tree, "build_workshop_readiness_report")
    constructors = (
        [
            node
            for node in ast.walk(builder)
            if isinstance(node, ast.Call) and _call_name(node) == "WorkshopReadinessReport"
        ]
        if builder is not None
        else []
    )
    expected_keywords: dict[str, object] = {
        "schema_version": "custombuild.workshop-readiness.v2",
        "release_scope": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }
    builder_safe = bool(constructors)
    for constructor in constructors:
        keywords = _call_keywords(constructor)
        if keywords is None:
            builder_safe = False
            continue
        for name, expected_value in expected_keywords.items():
            value = keywords.get(name)
            resolved = (
                _resolved_static_value(value, assignments)
                if value is not None
                else _STATIC_VALUE_MISSING
            )
            values_match = (
                resolved is expected_value
                if isinstance(expected_value, bool)
                else resolved == expected_value
            )
            if not values_match:
                builder_safe = False
    if not builder_safe:
        issues.append(f"{relative} builder can emit unsafe workshop readiness claims")


def _worker_stock_projection_is_safe(tree: ast.Module, generate: FunctionNode) -> bool:
    stock_calls = [
        node
        for node in ast.walk(generate)
        if isinstance(node, ast.Call) and _call_name(node) == "StockSheet"
    ]
    if len(stock_calls) != 2:
        return False
    roles: set[str] = set()
    identity_fields = {
        "role",
        "material_id",
        "material_version",
        "thickness_um",
        "width_um",
        "height_um",
    }
    for call in stock_calls:
        keywords = _call_keywords(call)
        stock_id = keywords.get("stock_id") if keywords is not None else None
        grain = keywords.get("grain_direction") if keywords is not None else None
        if (
            not isinstance(stock_id, ast.Call)
            or _call_name(stock_id) != "_stock_id"
            or _static_ast_value(grain) != "UNBOUND"
        ):
            return False
        identity = _call_keywords(stock_id)
        if identity is None or set(identity) != identity_fields:
            return False
        role = _static_ast_value(identity["role"])
        if not isinstance(role, str):
            return False
        roles.add(role)
    if roles != {"carcass", "back"}:
        return False

    helper = _simple_function(tree, "_stock_id")
    if helper is None:
        return False
    returns = [
        node.value
        for node in ast.walk(helper)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    if len(returns) != 1 or not isinstance(returns[0], ast.JoinedStr):
        return False
    referenced = {
        node.value.id
        for node in ast.walk(returns[0])
        if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name)
    }
    literal = "".join(
        node.value
        for node in returns[0].values
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return referenced == identity_fields and literal.startswith("stock-") and "um-" in literal


def _worker_semantic_document_evidence_is_persisted(
    generate: FunctionNode,
    *,
    path: str,
    kind: str,
) -> bool:
    candidates = _top_level_assignments_to(generate, "evidence_candidates")
    if len(candidates) != 1 or not isinstance(candidates[0][1], ast.ListComp):
        return False
    comprehension = candidates[0][1]
    if (
        len(comprehension.generators) != 1
        or not isinstance(comprehension.generators[0].target, ast.Name)
        or comprehension.generators[0].target.id != "artifact"
        or _expression_path(comprehension.generators[0].iter) != ("bundle", "artifacts")
        or not any(
            isinstance(node, ast.Constant) and node.value == path
            for condition in comprehension.generators[0].ifs
            for node in ast.walk(condition)
        )
    ):
        return False

    for node in ast.walk(generate):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "get"
            or not isinstance(node.func.value, ast.Dict)
            or not node.args
            or _expression_path(node.args[0]) != ("artifact", "path")
        ):
            continue
        if any(
            _static_ast_value(key) == path and _static_ast_value(value) == kind
            for key, value in zip(node.func.value.keys, node.func.value.values, strict=True)
        ):
            return True
    return False


def _check_worker_semantics(tree: ast.Module, relative: str, issues: list[str]) -> None:
    identity_keys = {
        "bundle_sha256",
        "manifest_sha256",
        "evidence_artifacts",
        "generation_context_hash",
    }
    generate = _simple_function(tree, "_generate")
    returned_dict = _returned_dict_literal(generate) if generate is not None else None
    result = _dict_literal_items(returned_dict) if returned_dict is not None else None
    if generate is None or result is None or not identity_keys.issubset(result):
        issues.append(f"{relative} has no unique immutable _generate result dictionary")
        return
    review_status = result.get("design_review_package_status")
    review_status_is_bound = (
        isinstance(review_status, ast.Call)
        and isinstance(review_status.func, ast.Attribute)
        and review_status.func.attr == "as_dict"
        and _expression_path(review_status.func.value) == ("bundle", "review_status")
        and not review_status.args
        and not review_status.keywords
    )
    mode = result.get("machine_program_mode")
    cam_blocked_assignments = _top_level_assignments_to(generate, "cam_blocked")
    cam_blocked_claim_is_bound = False
    if len(cam_blocked_assignments) == 1:
        _, expression = cam_blocked_assignments[0]
        cam_blocked_claim_is_bound = (
            isinstance(expression, ast.Compare)
            and len(expression.ops) == 1
            and isinstance(expression.ops[0], ast.Is)
            and len(expression.comparators) == 1
            and _expression_path(expression.left) == ("bundle", "review_status", "cam_status")
            and _expression_path(expression.comparators[0]) == ("CAMStageStatus", "BLOCKED")
        )
    mode_is_strictly_bound = (
        isinstance(mode, ast.IfExp)
        and _expression_path(mode.test) == ("cam_blocked",)
        and _static_ast_value(mode.body) == "CAM_BLOCKED"
        and _static_ast_value(mode.orelse) == "VALIDATION_DRY_RUN"
        and cam_blocked_claim_is_bound
        and review_status_is_bound
    )
    if not mode_is_strictly_bound:
        issues.append(
            f"{relative} generation mode is not strictly bound to the checksum-bound review status"
        )
    if _static_ast_value(result.get("production_machine_program")) is not False:
        issues.append(f"{relative} generation result can claim a production machine program")
    if not _worker_stock_projection_is_safe(tree, generate):
        issues.append(
            f"{relative} does not build unique role/thickness stock IDs with UNBOUND grain"
        )
    for path, kind, label in (
        ("validation/stock-selection.json", "stock_selection", "stock-selection snapshot"),
        ("validation/generation-plan.json", "generation_plan", "generation plan"),
    ):
        if not _worker_semantic_document_evidence_is_persisted(generate, path=path, kind=kind):
            issues.append(f"{relative} does not persist the checksum-bound {label}")


def _raising_guards(function: FunctionNode) -> list[ast.expr]:
    guards: list[ast.expr] = []
    for statement in function.body:
        if isinstance(statement, ast.Raise) or any(
            isinstance(node, ast.Return | ast.Yield | ast.YieldFrom) for node in ast.walk(statement)
        ):
            break
        if (
            isinstance(statement, ast.If)
            and len(statement.body) == 1
            and isinstance(statement.body[0], ast.Raise)
        ):
            guards.append(statement.test)
    return guards


def _negates_safe_predicate(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and isinstance(node.operand, ast.Call)
        and _call_name(node.operand) == "_generation_result_claims_are_safe"
        and len(node.operand.args) == 1
        and not node.operand.keywords
        and _expression_path(node.operand.args[0]) == ("job", "result_json")
    )


def _check_api_semantics(tree: ast.Module, relative: str, issues: list[str]) -> None:
    predicate = _simple_function(tree, "_generation_result_claims_are_safe")
    if predicate is None:
        issues.append(f"{relative} lacks the total generation-result safety predicate")
    else:
        bindings = _function_bindings(predicate)
        returns = [
            node.value
            for node in ast.walk(predicate)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        if not returns or any(
            not _safe_generation_predicate_return(value, bindings) for value in returns
        ):
            issues.append(f"{relative} generation-result safety predicate is incomplete")

    release = _simple_function(tree, "release_version")
    guards = _raising_guards(release) if release is not None else []
    has_exact_geometry_guard = any(
        _identity_comparison(
            guard,
            owner=("job", "result_json"),
            key="authoritative_geometry",
            expected=True,
            operation=ast.IsNot,
        )
        for guard in guards
    )
    has_total_predicate_guard = any(_negates_safe_predicate(guard) for guard in guards)
    if not has_exact_geometry_guard or not has_total_predicate_guard:
        issues.append(f"{relative} release path does not fail closed on generation claims")


def _context_equality_is_required(function: FunctionNode) -> bool:
    bindings = _function_bindings(function)
    for condition in _require_conditions(function):
        resolved = _resolved_binding(condition, bindings)
        if (
            not isinstance(resolved, ast.Compare)
            or len(resolved.ops) != 1
            or not isinstance(resolved.ops[0], ast.Eq)
            or len(resolved.comparators) != 1
        ):
            continue
        left = resolved.left
        right = resolved.comparators[0]
        left_job = _originates_from_access(
            left,
            owner=("completed_job",),
            key="production_context_hash",
            bindings=bindings,
        )
        right_result = _originates_from_access(
            right,
            owner=("job_result",),
            key="generation_context_hash",
            bindings=bindings,
        )
        right_job = _originates_from_access(
            right,
            owner=("completed_job",),
            key="production_context_hash",
            bindings=bindings,
        )
        left_result = _originates_from_access(
            left,
            owner=("job_result",),
            key="generation_context_hash",
            bindings=bindings,
        )
        if (left_job and right_result) or (right_job and left_result):
            return True
    return False


def _context_hash_is_returned(function: FunctionNode) -> bool:
    bindings = _function_bindings(function)
    return any(
        node.value is not None
        and (
            _originates_from_access(
                node.value,
                owner=("completed_job",),
                key="production_context_hash",
                bindings=bindings,
            )
            or _originates_from_access(
                node.value,
                owner=("job_result",),
                key="generation_context_hash",
                bindings=bindings,
            )
        )
        for node in ast.walk(function)
        if isinstance(node, ast.Return)
    )


def _manifest_context_is_required(function: FunctionNode) -> bool:
    bindings = _function_bindings(function)
    for condition in _require_conditions(function):
        resolved = _resolved_binding(condition, bindings)
        if (
            not isinstance(resolved, ast.Compare)
            or len(resolved.ops) != 1
            or not isinstance(resolved.ops[0], ast.Eq)
            or len(resolved.comparators) != 1
        ):
            continue
        comparator = resolved.comparators[0]
        if (
            _mapping_access(resolved.left, ("manifest",), "generation_context_hash", bindings)
            and _expression_path(_resolved_binding(comparator, bindings))
            == ("generation_context_hash",)
        ) or (
            _mapping_access(comparator, ("manifest",), "generation_context_hash", bindings)
            and _expression_path(_resolved_binding(resolved.left, bindings))
            == ("generation_context_hash",)
        ):
            return True
    return False


def _run_acceptance_passes_context(function: FunctionNode) -> bool:
    bindings = _function_bindings(function)
    context_names = {
        name
        for name, value in bindings.items()
        if isinstance(value, ast.Call)
        and _call_name(value) == "verify_generation_context_hash"
        and [_expression_path(argument) for argument in value.args[:2]]
        == [("completed_job",), ("job_result",)]
    }
    if not context_names:
        return False
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _call_name(node) != "verify_package":
            continue
        keywords = _call_keywords(node)
        context = keywords.get("generation_context_hash") if keywords is not None else None
        if isinstance(context, ast.Name) and context.id in context_names:
            return True
    return False


def _semantic_document_snapshot_is_bound(
    package: FunctionNode,
    run_acceptance: FunctionNode,
    assignments: dict[str, list[ast.expr]],
    *,
    path_constant: str,
    standalone_argument: str,
    archived_name: str,
    evidence_kind: str,
    verify_package_argument_index: int,
) -> bool:
    required_paths = assignments.get("REQUIRED_REVIEW_PACKAGE_PATHS", [])
    if len(required_paths) != 1 or not any(
        isinstance(node, ast.Name) and node.id == path_constant
        for node in ast.walk(required_paths[0])
    ):
        return False

    argument_names = {argument.arg for argument in (*package.args.posonlyargs, *package.args.args)}
    if standalone_argument not in argument_names:
        return False
    package_bindings = _function_bindings(package)
    archived = package_bindings.get(archived_name)
    if (
        not isinstance(archived, ast.Call)
        or not isinstance(archived.func, ast.Attribute)
        or archived.func.attr != "read"
        or _expression_path(archived.func.value) != ("archive",)
        or len(archived.args) != 1
        or _expression_path(archived.args[0]) != (path_constant,)
    ):
        return False
    bytes_bound = any(
        isinstance(condition, ast.Compare)
        and len(condition.ops) == 1
        and isinstance(condition.ops[0], ast.Eq)
        and len(condition.comparators) == 1
        and {
            _expression_path(condition.left),
            _expression_path(condition.comparators[0]),
        }
        == {(archived_name,), (standalone_argument,)}
        for condition in _require_conditions(package)
    )
    if not bytes_bound:
        return False

    for node in ast.walk(run_acceptance):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "verify_package"
            and len(node.args) > verify_package_argument_index
            and _mapping_access(
                node.args[verify_package_argument_index],
                ("downloaded",),
                evidence_kind,
            )
        ):
            return True
    return False


def _exact_dfm_blocker_binding(node: ast.AST | None) -> bool:
    """Recognize only the two canonical singleton DFM blocker profiles."""

    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.In)
        or len(node.comparators) != 1
        or not isinstance(node.left, ast.Subscript)
        or _expression_path(node.left.value) != ("package_status",)
        or _static_ast_value(node.left.slice) != "blocker_codes"
        or not isinstance(node.comparators[0], ast.Tuple | ast.List)
    ):
        return False
    profiles = node.comparators[0].elts
    if len(profiles) != 2 or any(
        not isinstance(profile, ast.List) or len(profile.elts) != 1 for profile in profiles
    ):
        return False
    return {
        _expression_path(profile.elts[0]) for profile in profiles if isinstance(profile, ast.List)
    } == {("STOCK_PROFILE_MISSING",), ("DFM_GRAIN_MISSING",)}


def _exact_grain_blocker_equality(node: ast.expr, bindings: dict[str, ast.expr]) -> bool:
    resolved = _resolved_binding(node, bindings)
    if (
        not isinstance(resolved, ast.Compare)
        or len(resolved.ops) != 1
        or not isinstance(resolved.ops[0], ast.Eq)
        or len(resolved.comparators) != 1
    ):
        return False
    comparator = resolved.comparators[0]

    def is_grain_singleton(candidate: ast.expr) -> bool:
        return (
            isinstance(candidate, ast.List)
            and len(candidate.elts) == 1
            and _expression_path(candidate.elts[0]) == ("DFM_GRAIN_MISSING",)
        )

    return (
        _mapping_access(resolved.left, ("package_status",), "blocker_codes", bindings)
        and is_grain_singleton(comparator)
    ) or (
        is_grain_singleton(resolved.left)
        and _mapping_access(comparator, ("package_status",), "blocker_codes", bindings)
    )


def _grain_readiness_unresolved_is_required(
    function: FunctionNode,
    bindings: dict[str, ast.expr],
) -> bool:
    return any(
        isinstance(node, ast.If)
        and _exact_grain_blocker_equality(node.test, bindings)
        and any(
            isinstance(candidate, ast.Call)
            and _call_name(candidate) == "require"
            and bool(candidate.args)
            and _string_equality(
                candidate.args[0],
                owner=("workshop_status",),
                key="MATERIAL_GRAIN",
                expected="EXTERNAL_EVIDENCE_REQUIRED",
                bindings=bindings,
            )
            for statement in node.body
            for candidate in ast.walk(statement)
        )
        for node in ast.walk(function)
    )


def _blocked_cam_negative_release_is_required(function: FunctionNode) -> bool:
    for branch in ast.walk(function):
        if not isinstance(branch, ast.If) or _expression_path(branch.test) != ("cam_blocked",):
            continue
        rejected_paths: set[str] = set()
        for statement in branch.body:
            for call in ast.walk(statement):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "request"
                    or len(call.args) < 2
                ):
                    continue
                keywords = _call_keywords(call)
                expected = keywords.get("expected") if keywords is not None else None
                if _static_ast_value(expected) != (409,):
                    continue
                rendered_path = ast.unparse(call.args[1])
                if "/approve" in rendered_path:
                    rejected_paths.add("approve")
                if "/release" in rendered_path:
                    rejected_paths.add("release")
        if rejected_paths == {"approve", "release"}:
            return True
    return False


def _check_live_acceptance_semantics(tree: ast.Module, relative: str, issues: list[str]) -> None:
    assignments = _module_assignments(tree)
    expected_constants = {
        "PRODUCTION_MANIFEST_SCHEMA_VERSION": "custombuild.production-manifest.v4",
        "WORKSHOP_READINESS_SCHEMA_VERSION": "custombuild.workshop-readiness.v2",
        "DFM_ENGINE_VERSION": "dfm-1.3.0",
        "STOCK_SELECTION_PATH": "validation/stock-selection.json",
        "STOCK_SELECTION_ROLE": "STOCK_SELECTION_SNAPSHOT",
        "STOCK_SELECTION_SCHEMA_VERSION": "custombuild.stock-selection.v1",
        "GENERATION_PLAN_PATH": "validation/generation-plan.json",
        "GENERATION_PLAN_ROLE": "GENERATION_PLAN",
        "GENERATION_PLAN_SCHEMA_VERSION": "custombuild.generation-plan.v1",
        "PRODUCTION_PIPELINE_VERSION": "production-pipeline-1.10.0",
        "OPERATIONS_SCHEMA_VERSION": "custombuild.operations.v2",
        "OPERATIONS_ENGINE_VERSION": "semantic-operations-1.2.0",
        "STOCK_PROFILE_MISSING": "STOCK_PROFILE_MISSING",
        "DFM_GRAIN_MISSING": "DFM-GRAIN-001",
        "DADO_RETENTION_EVIDENCE_MISSING": "DADO_RETENTION_EVIDENCE_MISSING",
    }
    for name, expected in expected_constants.items():
        values = assignments.get(name, [])
        if len(values) != 1 or _resolved_static_value(values[0], assignments) != expected:
            issues.append(f"{relative} has an unsafe or missing {name}")

    required_actions = assignments.get("BLOCKED_CAM_REQUIRED_ACTIONS", [])
    required_action_values = (
        _resolved_static_string_mapping(required_actions[0], assignments)
        if len(required_actions) == 1
        else _STATIC_VALUE_MISSING
    )
    retention_action = (
        required_action_values.get("DADO_RETENTION_EVIDENCE_MISSING")
        if isinstance(required_action_values, dict)
        else None
    )
    if retention_action != (
        "The current MVP cannot resolve this blocker because it has no authenticated "
        "catalogue/evidence boundary. Such a server-side boundary must bind a versioned, "
        "checksum-addressed mechanical retention contract to every DADO joint, including "
        "exact geometry, hardware quantity, material/thickness applicability and separate "
        "shear/withdrawal capacity data; a review acknowledgement, adhesive or geometric "
        "bearing check is not retention evidence."
    ):
        issues.append(
            f"{relative} does not bind the DADO retention blocker to its canonical action"
        )

    context_fields = assignments.get("CONTEXT_HASH_FIELDS", [])
    context_field_values = (
        _resolved_static_value(context_fields[0], assignments)
        if len(context_fields) == 1
        else _STATIC_VALUE_MISSING
    )
    if (
        not isinstance(context_field_values, tuple | list | set | frozenset)
        or "generation_context_hash" not in context_field_values
    ):
        issues.append(f"{relative} does not include generation context in manifest hashing")

    readiness = _simple_function(tree, "verify_workshop_readiness")
    readiness_physical_safe = readiness is not None and any(
        _identity_comparison(
            condition,
            owner=("payload",),
            key="physical_cutting_authorized",
            expected=False,
            operation=ast.Is,
            bindings=_function_bindings(readiness),
        )
        for condition in _require_conditions(readiness)
    )
    package = _simple_function(tree, "verify_package")
    package_physical_safe = package is not None and any(
        _identity_comparison(
            condition,
            owner=("manifest",),
            key="physical_cutting_authorized",
            expected=False,
            operation=ast.Is,
            bindings=_function_bindings(package),
        )
        for condition in _require_conditions(package)
    )
    if not readiness_physical_safe or not package_physical_safe:
        issues.append(f"{relative} does not reject physical-cutting authorization claims")

    generation_safety = _simple_function(tree, "verify_generation_result_safety")
    generation_bindings = (
        _function_bindings(generation_safety) if generation_safety is not None else {}
    )
    dfm_safe = generation_safety is not None and any(
        _string_membership(
            condition,
            owner=("job_result",),
            key="dfm_status",
            expected=frozenset({"PASS", "WARNING"}),
            bindings=generation_bindings,
        )
        for condition in _require_conditions(generation_safety)
    )
    dfm_blocked_binding_safe = _exact_dfm_blocker_binding(generation_bindings.get("dfm_blocked"))
    blocked_dfm_safe = generation_safety is not None and any(
        isinstance(node, ast.If)
        and _expression_path(node.test) == ("dfm_blocked",)
        and any(
            isinstance(candidate, ast.Call)
            and _call_name(candidate) == "require"
            and bool(candidate.args)
            and _string_equality(
                candidate.args[0],
                owner=("job_result",),
                key="dfm_status",
                expected="BLOCK",
                bindings=generation_bindings,
            )
            for statement in node.body
            for candidate in ast.walk(statement)
        )
        for node in ast.walk(generation_safety)
    )
    if not dfm_safe or not dfm_blocked_binding_safe or not blocked_dfm_safe:
        issues.append(
            f"{relative} does not bind live DFM acceptance to PASS/WARNING or the exact "
            "stock/grain BLOCK profiles"
        )
    grain_readiness_safe = (
        generation_safety is not None
        and _grain_readiness_unresolved_is_required(
            generation_safety,
            generation_bindings,
        )
    )
    if not grain_readiness_safe:
        issues.append(f"{relative} does not keep grain-blocked MATERIAL_GRAIN readiness unresolved")

    context_verifier = _simple_function(tree, "verify_generation_context_hash")
    run_acceptance = _simple_function(tree, "run_acceptance")
    if run_acceptance is None or not _blocked_cam_negative_release_is_required(run_acceptance):
        issues.append(
            f"{relative} does not require CAM approval and release rejection for blocked CAM"
        )
    context_bound = (
        context_verifier is not None
        and _context_equality_is_required(context_verifier)
        and _context_hash_is_returned(context_verifier)
        and package is not None
        and _manifest_context_is_required(package)
        and run_acceptance is not None
        and _run_acceptance_passes_context(run_acceptance)
    )
    if not context_bound:
        issues.append(f"{relative} does not bind generation context from job through manifest")
    semantic_documents_bound = (
        package is not None
        and run_acceptance is not None
        and _semantic_document_snapshot_is_bound(
            package,
            run_acceptance,
            assignments,
            path_constant="STOCK_SELECTION_PATH",
            standalone_argument="standalone_stock_selection",
            archived_name="archived_stock_selection",
            evidence_kind="stock_selection",
            verify_package_argument_index=2,
        )
        and _semantic_document_snapshot_is_bound(
            package,
            run_acceptance,
            assignments,
            path_constant="GENERATION_PLAN_PATH",
            standalone_argument="standalone_generation_plan",
            archived_name="archived_generation_plan",
            evidence_kind="generation_plan",
            verify_package_argument_index=3,
        )
    )
    if not semantic_documents_bound:
        issues.append(
            f"{relative} does not require and byte-bind the standalone semantic review documents"
        )


def production_semantic_contract_issues(repo: Path) -> list[str]:
    """Statically verify the safety claims that define a review-only production release."""

    issues: list[str] = []
    trees = _parse_production_sources(repo, issues)
    checks = (
        (_check_package_semantics, PRODUCTION_SEMANTIC_SOURCE_PATHS[0]),
        (_check_readiness_semantics, PRODUCTION_SEMANTIC_SOURCE_PATHS[1]),
        (_check_worker_semantics, PRODUCTION_SEMANTIC_SOURCE_PATHS[2]),
        (_check_api_semantics, PRODUCTION_SEMANTIC_SOURCE_PATHS[3]),
        (_check_live_acceptance_semantics, PRODUCTION_SEMANTIC_SOURCE_PATHS[4]),
    )
    for check, relative in checks:
        tree = trees.get(relative)
        if tree is not None:
            check(tree, relative, issues)
    return issues


def run_git(repo: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git CLI is not available")
    process = subprocess.run(  # noqa: S603 - argv is explicit and shell execution is disabled.
        [git, *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "Git command failed")
    return process.stdout.strip()


def resolved_compose(repo: Path) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI is not available")
    process = subprocess.run(  # noqa: S603 - fixed Docker argv, without a shell.
        [docker, "compose", "--file", "compose.yml", "config", "--format", "json"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "Compose resolution failed")
    value = json.loads(process.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Compose configuration is not a JSON object")
    return {str(key): item for key, item in value.items()}


def compose_hardening_issues(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    services = config.get("services", {})
    for name in ("api", "worker", "web"):
        service = services.get(name, {})
        if service.get("read_only") is not True:
            issues.append(f"{name} is not read-only")
        if "ALL" not in (service.get("cap_drop") or []):
            issues.append(f"{name} does not drop all Linux capabilities")
        security = service.get("security_opt") or []
        if not any(str(value).startswith("no-new-privileges") for value in security):
            issues.append(f"{name} lacks no-new-privileges")
    if not services.get("api", {}).get("healthcheck"):
        issues.append("api has no dependency-backed readiness healthcheck")
    if not services.get("worker", {}).get("healthcheck"):
        issues.append("worker has no Celery healthcheck")
    for name in ("postgres", "redis", "object-storage", "api", "worker", "web"):
        if services.get(name, {}).get("restart") != "unless-stopped":
            issues.append(f"{name} does not use the unless-stopped restart policy")
    for name in (
        "postgres",
        "redis",
        "object-storage",
        "api",
        "worker",
        "scheduler",
        "web",
    ):
        pids_limit = services.get(name, {}).get("pids_limit")
        if not isinstance(pids_limit, int) or isinstance(pids_limit, bool) or pids_limit <= 0:
            issues.append(f"{name} has no positive PID limit")
    api_service = services.get("api", {})
    api_environment = api_service.get("environment", {})
    for variable in ("REDIS_URL", "RATE_LIMIT_REQUESTS", "RATE_LIMIT_WINDOW_SECONDS"):
        if not isinstance(api_environment, dict) or not api_environment.get(variable):
            issues.append(f"api does not configure {variable}")

    def redis_url_has_password(value: object) -> bool:
        try:
            parsed = urlparse(str(value))
            _ = parsed.port
        except ValueError:
            return False
        return parsed.scheme in {"redis", "rediss"} and bool(parsed.hostname and parsed.password)

    redis_url = api_environment.get("REDIS_URL", "")
    if not redis_url_has_password(redis_url):
        issues.append("api Redis connection is not password-authenticated")
    worker_environment = services.get("worker", {}).get("environment", {})
    worker_redis_url: object = (
        worker_environment.get("REDIS_URL", "") if isinstance(worker_environment, dict) else ""
    )
    if not redis_url_has_password(worker_redis_url):
        issues.append("worker Redis connection is not password-authenticated")
    redis_service = services.get("redis", {})
    redis_environment = redis_service.get("environment", {})
    redis_command = " ".join(str(item) for item in (redis_service.get("command") or []))
    if (
        not isinstance(redis_environment, dict)
        or not redis_environment.get("REDIS_PASSWORD")
        or "--requirepass" not in redis_command
    ):
        issues.append("Redis does not require the configured password")
    web_environment = services.get("web", {}).get("environment", {}) or {}
    if isinstance(web_environment, dict) and any("REDIS" in str(key) for key in web_environment):
        issues.append("web receives Redis credentials")
    api_dependencies = api_service.get("depends_on", {})
    redis_dependency = (
        api_dependencies.get("redis", {}) if isinstance(api_dependencies, dict) else {}
    )
    if redis_dependency.get("condition") != "service_healthy":
        issues.append("api does not wait for the shared rate-limit store")
    web_dependencies = services.get("web", {}).get("depends_on", {})
    api_dependency = web_dependencies.get("api", {}) if isinstance(web_dependencies, dict) else {}
    if api_dependency.get("condition") != "service_healthy":
        issues.append("web does not wait for API readiness")

    def attached_networks(service_name: str) -> set[str]:
        value = services.get(service_name, {}).get("networks", {})
        if isinstance(value, dict):
            return {str(name) for name in value}
        if isinstance(value, list):
            return {str(name) for name in value}
        return set()

    networks = config.get("networks", {})
    backend = networks.get("backend", {}) if isinstance(networks, dict) else {}
    if not isinstance(backend, dict) or backend.get("internal") is not True:
        issues.append("backend network is not internal")
    artifact_ingress = networks.get("artifact-ingress", {}) if isinstance(networks, dict) else {}
    if not isinstance(artifact_ingress, dict) or artifact_ingress.get("internal") is True:
        issues.append("artifact ingress network is missing or internal")
    if attached_networks("web") != {"edge"}:
        issues.append("web is not isolated to the edge network")
    if not {"edge", "backend"}.issubset(attached_networks("api")):
        issues.append("api does not bridge edge and backend networks")
    for name in ("postgres", "redis", "object-storage", "worker"):
        service_networks = attached_networks(name)
        if "backend" not in service_networks or "edge" in service_networks:
            issues.append(f"{name} is not isolated to the backend network")
    if "artifact-ingress" not in attached_networks("object-storage"):
        issues.append("object-storage has no isolated host-publish ingress network")
    if "artifact-ingress" in attached_networks("web"):
        issues.append("web can reach the object-storage ingress network")
    return issues


def supply_chain_issues(repo: Path) -> list[str]:
    issues: list[str] = []
    workflow_dir = repo / ".github/workflows"
    action_pattern = re.compile(r"^\s*-?\s*uses:\s*([^#\s]+)")
    pinned_pattern = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for workflow in sorted(workflow_dir.glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = action_pattern.match(line)
            if match and not pinned_pattern.fullmatch(match.group(1)):
                issues.append(f"{workflow.name}:{line_number} uses an unpinned action")
            image_match = re.match(r"^\s+image:\s*([^#\s]+)", line)
            if image_match:
                image = image_match.group(1)
                locally_built = image.startswith("custombuild-") or image.startswith("${{")
                if not locally_built and "@sha256:" not in image:
                    issues.append(f"{workflow.name}:{line_number} uses an unpinned container image")

    evidence_workflow = workflow_dir / "supply-chain.yml"
    if not evidence_workflow.is_file():
        issues.append("supply-chain.yml is missing")
        return issues
    evidence = evidence_workflow.read_text(encoding="utf-8")
    if "anchore/sbom-action@" not in evidence:
        issues.append("supply-chain workflow does not generate an SBOM")
    if "anchore/scan-action@" not in evidence:
        issues.append("supply-chain workflow does not enforce vulnerability scanning")
    scan_count = evidence.count("anchore/scan-action@")
    if evidence.count("config: .grype.yaml") < scan_count:
        issues.append("supply-chain workflow does not load the reviewed Grype policy")
    if evidence.count("fail-build: true") < scan_count:
        issues.append("supply-chain workflow does not fail every vulnerability scan")
    if evidence.count("severity-cutoff: high") < scan_count:
        issues.append("supply-chain workflow does not block every High/Critical finding")
    if re.search(r"^\s*only-fixed\s*:", evidence, re.MULTILINE):
        issues.append("supply-chain workflow filters out unfixed High/Critical vulnerabilities")
    if "actions/upload-artifact@" not in evidence or ".release.json" not in evidence:
        issues.append("supply-chain workflow does not archive an immutable image manifest")

    compose = repo / "compose.yml"
    if not compose.is_file():
        issues.append("compose.yml is missing for container provenance checks")
    else:
        for line_number, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            locally_built = (
                stripped.startswith("image: custombuild-") and "${VCS_REF:-uncommitted}" in stripped
            )
            if stripped.startswith("image:") and "@sha256:" not in stripped and not locally_built:
                issues.append(f"compose.yml:{line_number} uses an unpinned container image")

    for relative in (
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
        "apps/web/Dockerfile",
    ):
        dockerfile = repo / relative
        if not dockerfile.is_file():
            issues.append(f"{relative} is missing for container provenance checks")
            continue
        source = dockerfile.read_text(encoding="utf-8")
        local_stages: set[str] = set()
        for line_number, line in enumerate(source.splitlines(), 1):
            tokens = line.strip().split()
            if not tokens or tokens[0].upper() != "FROM":
                continue
            image_index = 2 if len(tokens) > 1 and tokens[1].startswith("--platform=") else 1
            image = tokens[image_index] if len(tokens) > image_index else ""
            reserved_scratch = image == "scratch" and image not in local_stages
            if image not in local_stages and not reserved_scratch and "@sha256:" not in image:
                issues.append(f"{relative}:{line_number} uses an unpinned base image")
            if "AS" in (token.upper() for token in tokens):
                as_index = next(
                    index for index, token in enumerate(tokens) if token.upper() == "AS"
                )
                if len(tokens) > as_index + 1:
                    local_stages.add(tokens[as_index + 1])
        if "apt-get update" in source:
            has_dated_snapshot = re.search(
                r"^ARG DEBIAN_SNAPSHOT=\d{8}T\d{6}Z$", source, re.MULTILINE
            )
            required_snapshot_sources = (
                "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}",
                "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}",
                "Acquire::Check-Valid-Until",
                "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg",
            )
            if not has_dated_snapshot or any(
                value not in source for value in required_snapshot_sources
            ):
                issues.append(f"{relative} uses a mutable APT package index")
        common_provenance_arguments = (
            "APP_VERSION",
            "VCS_REF",
            "BUILD_DATE",
            "SOURCE_URL",
            "SOURCE_MANIFEST_SHA256",
        )
        common_provenance_labels = (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.created",
            "org.opencontainers.image.source",
            "io.custombuild.source-manifest.sha256",
        )
        lock_argument = (
            "FRONTEND_LOCK_SHA256"
            if relative == "apps/web/Dockerfile"
            else "DEPENDENCY_LOCK_SHA256"
        )
        lock_label = (
            "io.custombuild.frontend-lock.sha256"
            if relative == "apps/web/Dockerfile"
            else "io.custombuild.dependency-lock.sha256"
        )
        provenance_arguments = (*common_provenance_arguments, lock_argument)
        provenance_labels = (*common_provenance_labels, lock_label)
        if (
            any(f"ARG {argument}=" not in source for argument in provenance_arguments)
            or any(label not in source for label in provenance_labels)
            or any(f"{argument}=${{{argument}}}" not in source for argument in provenance_arguments)
        ):
            issues.append(f"{relative} does not embed complete OCI release provenance")
    seaweed_dockerfile = repo / "infra/seaweedfs/Dockerfile"
    if seaweed_dockerfile.is_file():
        seaweed_source = seaweed_dockerfile.read_text(encoding="utf-8")
        required_seaweed_contract = (
            (
                "golang:1.26.6-alpine3.24@sha256:"
                "3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83"
            ),
            "ENV GOTOOLCHAIN=local",
            (
                "ADD --checksum=sha256:"
                "6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd"
            ),
            "6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd",
            "1a96843ba71c16cee5c7e396a3082ab3ae0327ab429956db51d0d1b07f6508e5",
            "go.etcd.io/etcd/client/pkg/v3@v3.6.14",
            "golang.org/x/image@v0.45.0",
            "golang.org/x/text@v0.41.0",
            "-mod=readonly",
            "FROM scratch AS runtime",
            'io.custombuild.security-overrides.sha256="${SEAWEEDFS_SECURITY_OVERRIDES_SHA256}"',
            'io.custombuild.go.version="1.26.6"',
            'io.custombuild.go-etcd-client-pkg.version="3.6.14"',
            'io.custombuild.go-x-image.version="0.45.0"',
            'io.custombuild.go-x-text.version="0.41.0"',
            "ENV TMPDIR=/tmp",
        )
        if any(value not in seaweed_source for value in required_seaweed_contract):
            issues.append("infra/seaweedfs/Dockerfile is not reproducibly source-bound")
    issues.extend(vulnerability_exception_issues(repo))
    return issues


def promotion_contract_issues(repo: Path) -> list[str]:
    """Verify the repository-owned build-once promotion boundary."""

    issues = registry_overlay_issues(repo / "compose.registry.yml")
    descriptor = repo / "scripts/deploy_descriptor.py"
    if not descriptor.is_file():
        issues.append("scripts/deploy_descriptor.py is missing")
    workflow = repo / ".github/workflows/cd.yml"
    if not workflow.is_file():
        issues.append(".github/workflows/cd.yml is missing")
    return issues


def _policy_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _grype_exception_keys(policy: str) -> tuple[list[VulnerabilityExceptionKey], list[str]]:
    """Parse the deliberately narrow, exact Grype ignore policy without a YAML dependency."""

    lines = [
        (line_number, line.rstrip())
        for line_number, line in enumerate(policy.splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) == 1 and lines[0][1] == "ignore: []":
        return [], []
    if not lines or lines[0][1] != "ignore:":
        return [], ["Grype policy must contain only an exact ignore list"]

    field_patterns = (
        ("vulnerability", re.compile(r"^  - vulnerability:\s*(.+?)\s*$")),
        ("package marker", re.compile(r"^    package:\s*$")),
        ("package", re.compile(r"^      name:\s*(.+?)\s*$")),
        ("version", re.compile(r"^      version:\s*(.+?)\s*$")),
        ("type", re.compile(r"^      type:\s*(.+?)\s*$")),
    )
    keys: list[VulnerabilityExceptionKey] = []
    issues: list[str] = []
    index = 1
    while index < len(lines):
        block = lines[index : index + len(field_patterns)]
        if len(block) != len(field_patterns):
            issues.append(f"Grype exception near line {lines[index][0]} is incomplete")
            break
        values: dict[str, str] = {}
        malformed = False
        for (field, pattern), (line_number, line) in zip(field_patterns, block, strict=True):
            match = pattern.fullmatch(line)
            if match is None:
                issues.append(
                    f"Grype exception near line {line_number} does not define exact {field}"
                )
                malformed = True
                break
            if match.lastindex:
                value = _policy_value(match.group(1))
                if not value:
                    issues.append(f"Grype exception near line {line_number} has an empty {field}")
                    malformed = True
                    break
                values[field] = value
        if malformed:
            break
        keys.append(
            VulnerabilityExceptionKey(
                vulnerability=values["vulnerability"],
                package=values["package"],
                version=values["version"],
                package_type=values["type"],
            )
        )
        index += len(field_patterns)

    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    issues.extend(f"Grype exception {key.label()} is duplicated" for key in duplicates)
    return keys, issues


def _ledger_key(
    entry: dict[str, Any], *, index: int, issues: list[str]
) -> VulnerabilityExceptionKey | None:
    values: dict[str, str] = {}
    for field in ("vulnerability", "package", "version", "type"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"vulnerability exception ledger entry {index} is missing {field}")
        else:
            values[field] = value.strip()
    if len(values) != 4:
        return None
    return VulnerabilityExceptionKey(
        vulnerability=values["vulnerability"],
        package=values["package"],
        version=values["version"],
        package_type=values["type"],
    )


def vulnerability_exception_issues(repo: Path, *, today: date | None = None) -> list[str]:
    issues: list[str] = []
    policy_path = repo / ".grype.yaml"
    ledger_path = repo / "security/vulnerability-exceptions.json"
    if not policy_path.is_file():
        return [".grype.yaml is missing"]
    if not ledger_path.is_file():
        return ["security/vulnerability-exceptions.json is missing"]

    policy_keys, policy_issues = _grype_exception_keys(policy_path.read_text(encoding="utf-8"))
    issues.extend(policy_issues)
    try:
        raw_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["vulnerability exception ledger is invalid JSON"]
    if not isinstance(raw_ledger, dict):
        return ["vulnerability exception ledger must be a JSON object"]
    if raw_ledger.get("schema_version") != VULNERABILITY_EXCEPTION_SCHEMA:
        issues.append("vulnerability exception ledger has an unsupported schema")
    raw_entries = raw_ledger.get("exceptions")
    if not isinstance(raw_entries, list):
        return [*issues, "vulnerability exception ledger must contain an exceptions array"]

    records: dict[VulnerabilityExceptionKey, dict[str, Any]] = {}
    review_date = today or date.today()
    for index, raw_entry in enumerate(raw_entries, 1):
        if not isinstance(raw_entry, dict):
            issues.append("vulnerability exception ledger contains a non-object entry")
            continue
        entry = {str(key): value for key, value in raw_entry.items()}
        key = _ledger_key(entry, index=index, issues=issues)
        if key is None:
            continue
        if key in records:
            issues.append(f"vulnerability exception {key.label()} is duplicated")
            continue
        records[key] = entry
        for field in ("severity", "owner", "rationale", "mitigation", "source", "review_by"):
            if not isinstance(entry.get(field), str) or not str(entry[field]).strip():
                issues.append(f"vulnerability exception {key.label()} is missing {field}")
        severity = entry.get("severity")
        if isinstance(severity, str) and severity not in VULNERABILITY_SEVERITIES:
            issues.append(f"vulnerability exception {key.label()} has invalid severity")
        source = entry.get("source")
        if isinstance(source, str) and not source.startswith("https://"):
            issues.append(f"vulnerability exception {key.label()} has a non-HTTPS source")
        review_by = entry.get("review_by")
        if isinstance(review_by, str):
            try:
                deadline = date.fromisoformat(review_by)
            except ValueError:
                issues.append(f"vulnerability exception {key.label()} has an invalid review date")
            else:
                if deadline < review_date:
                    issues.append(f"vulnerability exception {key.label()} expired on {deadline}")

    policy_set = set(policy_keys)
    for key in sorted(policy_set - set(records)):
        issues.append(f"Grype exception {key.label()} has no exact ledger record")
    for key in sorted(set(records) - policy_set):
        issues.append(f"ledger record {key.label()} is not present in the Grype policy")
    return issues


def build_report(repo: Path, *, require_clean: bool) -> dict[str, Any]:
    repo = repo.resolve()
    checks: list[ReleaseCheck] = []
    surface: DeploymentSurface | None = None
    surface_error: IsolationError | None = None
    try:
        surface = load_surface(repo / "compose.yml")
    except IsolationError as exc:
        surface_error = exc

    required_files = (
        "compose.yml",
        "compose.external-production.yml",
        "compose.registry.yml",
        "README.md",
        "docs/ENVIRONMENTS.md",
        "docs/OPERATIONS.md",
        "docs/PRODUCTION_SAFETY.md",
        "docs/SECURITY.md",
        ".github/workflows/supply-chain.yml",
        ".github/workflows/cd.yml",
        "scripts/compose_backup.py",
        "scripts/backup_freshness.py",
        "scripts/check_external_production.py",
        "scripts/deploy_descriptor.py",
        *PRODUCTION_SEMANTIC_SOURCE_PATHS,
        "scripts/source_manifest.py",
        PRODUCTION_SEMANTIC_ROOT_PATH,
        "scripts/restore_drill.py",
    )
    missing_files = [name for name in required_files if not (repo / name).is_file()]
    canonical_release_source = (
        not missing_files and surface is not None and surface.project == "custombuild-prod"
    )
    if missing_files:
        source_detail = f"Missing: {missing_files}"
    elif surface is None:
        source_detail = f"Compose identity is invalid: {surface_error}"
    elif surface.project != "custombuild-prod":
        source_detail = f"{surface.project} is an isolated test environment, not a release source."
    else:
        source_detail = "Repository root is the custombuild-prod release candidate."
    checks.append(
        ReleaseCheck(
            "RELEASE_SOURCE",
            "Canonical release source",
            CheckStatus.PASS if canonical_release_source else CheckStatus.BLOCK,
            source_detail,
        )
    )

    dirty = run_git(repo, "status", "--porcelain")
    checks.append(
        ReleaseCheck(
            "GIT_STATE",
            "Reproducible Git state",
            (
                CheckStatus.BLOCK
                if dirty and require_clean
                else CheckStatus.WARNING
                if dirty
                else CheckStatus.PASS
            ),
            (
                f"{len(dirty.splitlines())} changed paths are not committed."
                if dirty
                else "Working tree is clean."
            ),
        )
    )
    revision = run_git(repo, "rev-parse", "HEAD")
    branch = run_git(repo, "branch", "--show-current") or "detached"

    source_manifest_sha256: str | None = None
    try:
        source_manifest_sha256 = build_source_manifest(repo)[2]
    except (OSError, SourceManifestError) as exc:
        checks.append(
            ReleaseCheck(
                "SOURCE_MANIFEST",
                "Exact Docker build-context identity",
                CheckStatus.BLOCK,
                str(exc),
            )
        )
    else:
        checks.append(
            ReleaseCheck(
                "SOURCE_MANIFEST",
                "Exact Docker build-context identity",
                CheckStatus.PASS,
                f"Canonical source manifest SHA-256: {source_manifest_sha256}",
            )
        )

    repository_content_root_sha256: str | None = None
    try:
        repository_content_root_sha256 = verify_production_semantic_root(repo).digest
    except (OSError, SourceManifestError) as exc:
        checks.append(
            ReleaseCheck(
                "REPOSITORY_CONTENT_ROOT",
                "Exact tracked repository content root",
                CheckStatus.BLOCK,
                str(exc),
            )
        )
    else:
        checks.append(
            ReleaseCheck(
                "REPOSITORY_CONTENT_ROOT",
                "Exact tracked repository content root",
                CheckStatus.PASS,
                (
                    f"Verified diagnostic SHA-256: {repository_content_root_sha256}; "
                    "local authorization is false and external semantic approval remains required."
                ),
            )
        )

    if surface is not None:
        checks.append(
            ReleaseCheck(
                "ENVIRONMENT_IDENTITY",
                "Isolated Compose identity",
                CheckStatus.PASS,
                f"{surface.project}; ports {sorted(surface.published_ports)}",
            )
        )
    else:
        checks.append(
            ReleaseCheck(
                "ENVIRONMENT_IDENTITY",
                "Isolated Compose identity",
                CheckStatus.BLOCK,
                str(surface_error),
            )
        )

    try:
        hardening_issues = compose_hardening_issues(resolved_compose(repo))
        checks.append(
            ReleaseCheck(
                "CONTAINER_HARDENING",
                "Container hardening and readiness",
                CheckStatus.BLOCK if hardening_issues else CheckStatus.PASS,
                (
                    "; ".join(hardening_issues)
                    if hardening_issues
                    else "Runtime services are read-only, capability-dropped and readiness-gated."
                ),
            )
        )
    except RuntimeError as exc:
        checks.append(
            ReleaseCheck(
                "CONTAINER_HARDENING",
                "Container hardening and readiness",
                CheckStatus.BLOCK,
                str(exc),
            )
        )

    prod_workflow = (repo / ".github/workflows/prod-ci.yml").read_text(encoding="utf-8")
    stale_source = "working-directory: prod" in prod_workflow or "prod/compose.yml" in prod_workflow
    checks.append(
        ReleaseCheck(
            "CI_SOURCE",
            "Production CI uses canonical source",
            CheckStatus.BLOCK if stale_source else CheckStatus.PASS,
            (
                "Production CI targets the repository root."
                if not stale_source
                else "Production CI still targets the legacy prod snapshot."
            ),
        )
    )

    semantic_issues = production_semantic_contract_issues(repo)
    checks.append(
        ReleaseCheck(
            "PRODUCTION_SEMANTIC_CONTRACT",
            "Review-only production semantic contract",
            CheckStatus.BLOCK if semantic_issues else CheckStatus.PASS,
            (
                "; ".join(semantic_issues)
                if semantic_issues
                else "Manifest, readiness, worker, API and live acceptance safety claims agree."
            ),
        )
    )

    chain_issues = supply_chain_issues(repo)
    checks.append(
        ReleaseCheck(
            "SUPPLY_CHAIN_EVIDENCE",
            "Pinned CI actions, SBOM and vulnerability gate",
            CheckStatus.BLOCK if chain_issues else CheckStatus.PASS,
            (
                "; ".join(chain_issues)
                if chain_issues
                else "All workflow actions are immutable; image SBOM and fixed high/critical "
                "vulnerability gates are configured."
            ),
        )
    )

    promotion_issues = promotion_contract_issues(repo)
    checks.append(
        ReleaseCheck(
            "DIGEST_PROMOTION_CONTRACT",
            "Digest-only promotion input and Compose role mapping",
            CheckStatus.BLOCK if promotion_issues else CheckStatus.PASS,
            (
                "; ".join(promotion_issues)
                if promotion_issues
                else "The canonical descriptor and registry overlay bind every service role "
                "to an exact image digest and require --no-build."
            ),
        )
    )

    checks.extend(
        (
            ReleaseCheck(
                "COMMERCIAL_REVIEW",
                "Commercial and licence approval",
                CheckStatus.EXTERNAL_EVIDENCE_REQUIRED,
                "CI generates image SBOM and vulnerability evidence, and the unmaintained "
                "MinIO runtime has been replaced; final notices and dated counsel/release-owner "
                "approval remain external.",
            ),
            ReleaseCheck(
                "PHYSICAL_MACHINE_AUTHORIZATION",
                "Physical machine authorization",
                CheckStatus.EXTERNAL_EVIDENCE_REQUIRED,
                "Calibration, measured tooling/material, coupons, simulation, air-cut, reference "
                "part, prototype and named workshop approvals are required.",
            ),
        )
    )
    software_blockers = [item for item in checks if item.status is CheckStatus.BLOCK]
    return {
        "schema_version": "custombuild.release-readiness-static.v3",
        "repository": str(repo),
        "git_revision": revision,
        "git_branch": branch,
        "source_manifest_sha256": source_manifest_sha256,
        "repository_content_root_sha256": repository_content_root_sha256,
        "external_semantic_approval_required": True,
        "static_controls_ready": not software_blockers,
        "software_release_ready": False,
        "runtime_evidence_required": True,
        "commercial_release_ready": False,
        "physical_machine_release_ready": False,
        "checks": [asdict(item) for item in checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = build_report(args.repo, require_clean=args.require_clean)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["static_controls_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
