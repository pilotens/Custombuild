from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.design_service import auto_fix, preview
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "fixtures" / "golden" / "bookcases.cases.json"
EXPECTED_PATH = ROOT / "tests" / "fixtures" / "golden" / "bookcases.expected.json"


def evaluate(case: dict[str, Any]) -> dict[str, Any]:
    try:
        operation = auto_fix if case["mode"] == "auto" else preview
        spec, result, presented = operation(
            case["input"], design_id=f"golden-{case['name']}"
        )
        return {
            "outcome": "result",
            "design_hash": result.design_hash,
            "status": presented["status"],
            "part_count": len(result.parts),
            "joint_count": len(result.joints),
            "divider_count": spec.parameters.vertical_divider_count,
            "rule_statuses": {
                item["rule_id"]: item["status"]
                for item in presented["rule_evaluations"]
            },
            "change_paths": sorted(
                change["path"]
                for diff in presented["change_diff"]
                for change in diff.get("changes", [])
            ),
        }
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        return {
            "outcome": "error",
            "error_type": str(first["type"]),
            "location": [str(value) for value in first["loc"]],
            "message": str(first["msg"]),
        }
    except ValueError as exc:
        return {
            "outcome": "error",
            "error_type": type(exc).__name__,
            "location": [],
            "message": str(exc).splitlines()[0],
        }


def render() -> dict[str, Any]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": "custombuild.golden-bookcases.v1",
        "fixture_count": len(cases),
        "fixtures": [
            {"name": case["name"], "expected": evaluate(case)} for case in cases
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--render", action="store_true")
    arguments = parser.parse_args()
    rendered = json.dumps(render(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.write:
        EXPECTED_PATH.write_text(rendered, encoding="utf-8")
    if arguments.render:
        print(rendered, end="")
    if arguments.check:
        expected = EXPECTED_PATH.read_text(encoding="utf-8")
        if expected != rendered:
            raise SystemExit(
                "Golden fixtures changed. Review the deterministic diff and run "
                "scripts/regenerate_golden.py --write only when intentional."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
