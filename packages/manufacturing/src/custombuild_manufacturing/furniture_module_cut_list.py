"""One review cut list for the exact generated module drafts."""

from __future__ import annotations

import csv
import io
from typing import Any

from custombuild_domain.furniture_engine import FurnitureResult

from .adapters import adapt_design_result
from .exporters import cut_list_csv


def module_cut_list_csv(plan_hash: str, modules: list[dict[str, Any]]) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    for index, module in enumerate(modules):
        result = FurnitureResult.model_validate(module["preview"]["design"])
        parts = adapt_design_result(result).parts
        names = {part.part_id: part.semantic_key for part in result.parts}
        plane_axes = {"X": "U", "Y": "V", "NONE": "NONE"}
        grains = {part.part_id: plane_axes[part.grain_direction] for part in parts}
        standard = csv.DictReader(io.StringIO(cut_list_csv(parts).decode("utf-8-sig")))
        if index == 0:
            writer.writerow(
                (
                    "plan_hash",
                    "module_id",
                    "row_from_floor",
                    "column_from_left",
                    "draft_design_hash",
                    "part_name",
                    "grain_axis_uv",
                    *(
                        "draft_" + k if k in {"instance_id", "part_id"} else k
                        for k in (standard.fieldnames or ())
                    ),
                )
            )
        for item in standard:
            part_id = item["part_id"]
            writer.writerow(
                (
                    plan_hash,
                    module["module_id"],
                    module["row"] + 1,
                    module["column"] + 1,
                    result.design_hash,
                    names[part_id],
                    grains[part_id],
                    *item.values(),
                )
            )
    return out.getvalue()
