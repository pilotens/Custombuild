"""Explicit, bounded proposals for separate shelving carcasses.

A plan is a new assembly concept, not a splice of an existing part or a released
manufacturing design. Every draft keeps the original installation contract so
exporting it cannot silently discard unresolved site/trim requirements.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any

from custombuild_domain.enums import BackPanelType
from custombuild_domain.furniture import FurnitureWorkspace, ShelvingIntent
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.identity import content_hash
from custombuild_domain.models import FrozenModel
from pydantic import Field, model_validator

from .furniture_profiles import preview_furniture
from .furniture_suggestions import _passes, _screening_checks

MODULE_PLANNING_VERSION = "furniture-module-planning-1.0.0"
MAX_MODULE_COUNT = 16
MAX_MODULE_PARTS = 512


class FurnitureModuleGrid(FrozenModel):
    """Equal outer widths/heights, with explicit gaps and shelf counts.

    Rows run from floor to ceiling; columns from left to right. Integer
    remainders go to the first rows/columns. Shelf positions inside each new
    carcass are evenly distributed by the ordinary geometry compiler.
    """

    columns: Annotated[int, Field(strict=True, ge=1, le=8)]
    rows: Annotated[int, Field(strict=True, ge=1, le=4)]
    gap_um: Annotated[int, Field(strict=True, ge=0, le=100_000)]
    shelf_count_per_row: tuple[Annotated[int, Field(strict=True, ge=0, le=40)], ...] = Field(
        min_length=1, max_length=4
    )
    divider_count_per_module: Annotated[int, Field(strict=True, ge=0, le=16)] = 0

    @model_validator(mode="after")
    def validate_grid(self) -> FurnitureModuleGrid:
        if self.rows * self.columns > MAX_MODULE_COUNT:
            raise ValueError(f"module grid exceeds the limit of {MAX_MODULE_COUNT} carcasses")
        if self.rows * self.columns < 2:
            raise ValueError("a module plan requires at least two separate carcasses")
        if len(self.shelf_count_per_row) != self.rows:
            raise ValueError("shelf_count_per_row must contain one count per row, bottom to top")
        return self


def _allocate(total: int, count: int, gap: int) -> list[int]:
    usable = total - gap * (count - 1)
    if usable <= 0:
        raise ValueError("module gaps consume the entire available dimension")
    base, remainder = divmod(usable, count)
    return [base + (index < remainder) for index in range(count)]


def _requirement(code: str, message: str, *, blocked: bool = False) -> dict[str, str]:
    return {"code": code, "status": "blocked" if blocked else "required", "message": message}


def _draft_requirements(preview: dict[str, Any]) -> list[dict[str, str]]:
    requirements = [
        _requirement(f"RULE_{rule['rule_id']}", rule["detail"], blocked=rule["status"] == "BLOCK")
        for rule in preview["rules"]["evaluations"]
        if rule["status"] != "PASS"
    ]
    requirements.extend(
        _requirement(issue["code"], issue["message"], blocked=True)
        for issue in preview["manufacturing"]["issues"]
    )
    # Preserve the source installation while surfacing the canonical module
    # review too: even a reconciled whole-assembly installation does not become
    # a reconciled site contract for a smaller, independently exported carcass.
    requirements.extend(
        _requirement(issue["code"], issue["message"], blocked=True)
        for issue in preview["workshop_handoff"]["dimensions"]["issues"]
    )
    return requirements


def plan_furniture_modules(
    workspace: FurnitureWorkspace, grid: FurnitureModuleGrid
) -> dict[str, Any]:
    """Generate complete canonical drafts, never partially usable proposals.

    Geometry/rules/stock reports are recomputed by preview_furniture. The plan
    does not qualify connections between modules, support of a vertical gap,
    stacking loads, installation, hardware or any physical machining operation.
    """
    # Revalidate even caller-created model instances (model_construct/model_copy
    # can otherwise bypass Pydantic bounds at a non-HTTP entry point).
    workspace = FurnitureWorkspace.model_validate(workspace.model_dump(mode="python"))
    grid = FurnitureModuleGrid.model_validate(grid.model_dump(mode="python"))
    source_document = workspace.model_dump(mode="json")
    grid_document = grid.model_dump(mode="json")
    current = preview_furniture(workspace)
    source_dimensions = review_furniture_dimensions(workspace.design)
    plan_hash = content_hash(
        {
            "version": MODULE_PLANNING_VERSION,
            "source_workspace": source_document,
            "grid": grid_document,
            "source_design_hash": current["design"]["design_hash"],
            "dependencies": current["dependencies"],
        }
    )
    requirements = [
        _requirement(
            "MODULE_DESIGN_CHANGE_REVIEW_REQUIRED",
            "Granska den nya konstruktionen: varje modul får egna sidor, topp, botten och "
            "hyllplaceringar. Delar, invändig volym, vikt och designidentitet ändras. "
            "Ingen befintlig del delas eller skarvas automatiskt.",
        ),
        _requirement(
            "MODULE_CONNECTIONS_REQUIRED",
            "Förband mellan moduler saknar modellerade beslag, hålbilder och verifierad "
            "kapacitet. Konstruktion och monteringsordning behöver fastställas.",
        ),
        _requirement(
            "WALL_ANCHORAGE_REQUIRED",
            "Väggunderlag, förankring och sidostabilitet måste verifieras för hela "
            "modulgruppen. Separata modulers screening kvalificerar inte helheten.",
        ),
        _requirement(
            "MATERIAL_AND_JOINT_QUALIFICATION_REQUIRED",
            "Valt material, faktisk batch, förband och ett representativt passningsprov "
            "behöver kvalificeras. Materialval och batch ersätts inte av planen.",
        ),
        _requirement(
            "WORKSHOP_QUALIFICATION_REQUIRED",
            "Verkstad, nesting, uppspänning, CAM, postprocessor och maskin måste "
            "verifieras för de nya delarna före tillverkning.",
        ),
        _requirement(
            "SOURCE_INSTALLATION_REVIEW_REQUIRED",
            "Källans installationskrav följer med oförändrade i varje arbetsfil och i "
            "planen. Modulernas placering gäller inom stommens koordinater. "
            "Ett delutkast är inte en separat godkänd installation; helheten behöver "
            "en granskad installations- och monteringslösning.",
        ),
    ]
    requirements.extend(
        _requirement(f"SOURCE_{issue['code']}", issue["message"], blocked=True)
        for issue in source_dimensions["issues"]
    )
    if workspace.design.installation is None:
        requirements.append(
            _requirement(
                "INSTALLATION_MEASUREMENTS_REQUIRED",
                "Källan saknar installationsmått. Mät plats, montageutrymme och eventuell "
                "list; inga frigångar eller listmått antas.",
            )
        )
    if grid.rows > 1:
        requirements.append(
            _requirement(
                "STACKING_LOAD_PATH_REQUIRED",
                "Staplingslast är inte beräknad. Övre modulers egenvikt och samtliga "
                "nyttiga laster måste föras genom verifierade upplag till underlaget. "
                "Modulens hyllscreening inkluderar inte lasten från moduler ovanför.",
            )
        )
        if grid.gap_um:
            requirements.append(
                _requirement(
                    "VERTICAL_GAP_SUPPORT_REQUIRED",
                    "Det valda vertikala mellanrummet saknar modellerad bärande "
                    "distans eller upphängning. Planen antar ingen flytande montering.",
                )
            )
    response: dict[str, Any] = {
        "schema_version": "custombuild.furniture-module-plan.v1",
        "version": MODULE_PLANNING_VERSION,
        "state": "unavailable",
        "code": "MODULE_PLAN_UNAVAILABLE",
        "message": "Ingen komplett modulplan har beräknats.",
        "source_design_hash": current["design"]["design_hash"],
        "source": {
            "workspace": source_document,
            "workspace_hash": content_hash(source_document),
            "design_hash": current["design"]["design_hash"],
            "installation": source_dimensions["installation"],
            "dimension_review": source_dimensions,
        },
        "grid": grid_document,
        "plan_hash": plan_hash,
        "modules": [],
        "layout": None,
        "load_distribution": None,
        "screening": None,
        "requirements": requirements,
        "review_status": "blocked",
        "can_apply": False,
        "can_export_drafts": False,
        "production_qualified": False,
        "physical_cutting_authorized": False,
        "limits": {"max_modules": MAX_MODULE_COUNT, "max_generated_parts": MAX_MODULE_PARTS},
        "scope": "Explicit konstruktionsförslag med fristående stommoduler. "
        "Varje arbetsfil är ett nytt utkast med källans installationskrav bevarade. "
        "Inga intermodulförband, skarvar, distanser, förankringar eller godkännanden skapas.",
    }

    def unavailable(code: str, message: str) -> dict[str, Any]:
        response.update(code=code, message=message)
        response["requirements"].append(_requirement(code, message, blocked=True))
        return response

    intent = workspace.design.intent
    if not isinstance(intent, ShelvingIntent):
        return unavailable("UNSUPPORTED_FAMILY", "Modulplanering stöder för närvarande hyllsystem.")
    if intent.bay_width_ratios_ppm:
        return unavailable(
            "CUSTOM_BAY_LAYOUT",
            "Valda fackproportioner kan inte omvandlas exakt till detta modulrutnät. "
            "Skapa och granska ett separat utkast med jämn fackfördelning först.",
        )
    if intent.shelf_height_ratios_ppm:
        return unavailable(
            "CUSTOM_SHELF_LAYOUT",
            "Valda hyllhöjder kan inte omvandlas exakt till detta modulrutnät. "
            "De tas inte bort automatiskt; skapa och granska ett separat utkast först.",
        )
    if grid.rows > 1 and intent.plinth_height_um:
        return unavailable(
            "PLINTH_TRANSFORMATION_UNSUPPORTED",
            "Sockelhöjd är vald. En staplad konstruktion behöver en explicit "
            "sockellösning; sockeln dupliceras eller tas inte bort automatiskt.",
        )
    if sum(grid.shelf_count_per_row) < intent.shelf_count:
        return unavailable(
            "SHELF_CAPACITY_REDUCTION_UNSUPPORTED",
            "Antalet hyllrader sammanlagt får inte vara lägre än källans. "
            "En mindre förvaringskapacitet och last får inte döljas av moduluppdelningen.",
        )
    dividers = grid.divider_count_per_module
    # Count the canonical shelving parts before compiling candidates. Captured
    # backs have one field per bay; a surface-mounted back is one whole panel.
    # The generated count is checked again below so a compiler change cannot
    # silently invalidate this preflight bound. This is not an ordering BOM.
    back_count = (
        dividers + 1
        if intent.back_panel == BackPanelType.INSET_GROOVE
        else int(intent.back_panel == BackPanelType.SURFACE_MOUNTED)
    )
    estimated_parts = grid.columns * sum(
        4 + dividers + count * (dividers + 1) + back_count + bool(intent.plinth_height_um)
        for count in grid.shelf_count_per_row
    )
    if estimated_parts > MAX_MODULE_PARTS:
        return unavailable(
            "MODULE_COMPLEXITY_LIMIT",
            f"Rutnätet överskrider beräkningsgränsen på {MAX_MODULE_PARTS} delar. "
            "Minska antalet moduler, hyllor eller invändiga avdelare.",
        )
    try:
        widths = _allocate(intent.width_um, grid.columns, grid.gap_um)
        heights = _allocate(intent.height_um, grid.rows, grid.gap_um)
    except ValueError as exc:
        return unavailable("MODULE_GAPS_CONSUME_SPACE", str(exc))
    net_width = sum(widths)
    source_load = intent.resolved_shelf_load_n
    if intent.shelf_load_basis == "per_metre":
        loads = [(width * intent.shelf_load_per_metre_n + 999_999) // 1_000_000 for width in widths]
    else:
        loads = [(source_load * width + net_width - 1) // net_width for width in widths]
    if sum(loads) < source_load:
        return unavailable(
            "LOAD_REDUCTION_UNSUPPORTED",
            "Valda mellanrum skulle minska den samlade radlasten med oförändrad N/m. "
            "Välj ett annat mellanrum eller granska källans lastkrav explicit först. "
            "Planen minskar inte lasten och ändrar inte N/m automatiskt.",
        )
    modules: list[dict[str, Any]] = []
    z = 0
    for row, height in enumerate(heights):
        x = 0
        for column, width in enumerate(widths):
            module_id = f"module-{plan_hash[:24]}-r{row + 1}-c{column + 1}"
            document = deepcopy(source_document)
            document["design"].update(design_id=module_id, revision=1)
            document["design"]["intent"].update(
                width_um=width,
                height_um=height,
                shelf_count=grid.shelf_count_per_row[row],
                divider_count=dividers,
            )
            if intent.shelf_load_basis == "per_row":
                document["design"]["intent"]["shelf_load_n"] = loads[column]
            try:
                draft = FurnitureWorkspace.model_validate(document)
                preview = preview_furniture(draft)
            except ValueError as exc:
                return unavailable(
                    "MODULE_GEOMETRY_UNSUPPORTED",
                    f"Modul rad {row + 1}, kolumn {column + 1} kan inte beräknas: {exc}",
                )
            canonical = draft.model_dump(mode="json")
            modules.append(
                {
                    "module_id": module_id,
                    "row": row,
                    "column": column,
                    "placement": {"x_um": x, "y_um": 0, "z_um": z},
                    "dimensions_um": {
                        "width_um": width,
                        "height_um": height,
                        "depth_um": intent.depth_um,
                    },
                    "workspace": canonical,
                    "workspace_hash": content_hash(canonical),
                    "design_hash": preview["design"]["design_hash"],
                    "source_workspace_hash": response["source"]["workspace_hash"],
                    "source_design_hash": response["source_design_hash"],
                    "plan_hash": plan_hash,
                    "weight_g": preview["design"]["total_weight_g"],
                    "shelf_row_payload_n": loads[column],
                    "total_shelf_payload_n": loads[column] * grid.shelf_count_per_row[row],
                    "preview": preview,
                    "requirements": _draft_requirements(preview),
                    "review_status": "requires_review",
                    "production_qualified": False,
                    "physical_cutting_authorized": False,
                }
            )
            x += width + grid.gap_um
        z += height + grid.gap_um
    for module in modules:
        # Each module carries whole-assembly unknowns as well as its own rules.
        module["requirements"] = deepcopy(requirements) + module["requirements"]
    stock_results = [
        module["preview"]["manufacturing"]["geometry_compatible"] for module in modules
    ]
    stock_fit = None if any(value is None for value in stock_results) else all(stock_results)
    part_count = sum(len(module["preview"]["design"]["parts"]) for module in modules)
    if part_count > MAX_MODULE_PARTS:
        return unavailable(
            "MODULE_COMPLEXITY_LIMIT", "Det genererade delantalet överskrider gränsen."
        )
    response.update(
        state="available",
        code="MODULE_PLAN_AVAILABLE",
        message=f"{len(modules)} nya stommoduler är beräknade som separata utkast. "
        "Granska modulmått, last, råformat, installation och förband före fortsatt beredning.",
        modules=modules,
        layout={
            "column_widths_um": widths,
            "row_heights_um": heights,
            "gap_um": grid.gap_um,
            "envelope": {
                "width_um": intent.width_um,
                "height_um": intent.height_um,
                "depth_um": intent.depth_um,
            },
            "coordinate_system": "source_carcass_bottom_left_front",
            "row_order": "bottom_to_top",
            "column_order": "left_to_right",
            "joint_or_spacer_geometry_created": False,
        },
        load_distribution={
            "basis": intent.shelf_load_basis,
            "source_shelf_row_payload_n": source_load,
            "module_shelf_row_payload_n_by_column": loads,
            "assembled_shelf_row_payload_n": sum(loads),
            "source_total_shelf_payload_n": source_load * intent.shelf_count,
            "planned_total_shelf_payload_n": sum(loads) * sum(grid.shelf_count_per_row),
            "shelf_load_per_metre_n": intent.shelf_load_per_metre_n
            if intent.shelf_load_basis == "per_metre"
            else None,
            "rounding": "ceiling_per_module",
            "stacking_loads_included_in_shelf_screening": False,
        },
        screening={
            "all_stock_fit": stock_fit,
            "all_shelf_numeric_pass": all(
                _passes(_screening_checks(module["preview"])) for module in modules
            ),
            "rule_block_count": sum(
                rule["status"] == "BLOCK"
                for module in modules
                for rule in module["preview"]["rules"]["evaluations"]
            ),
            "stock_issue_count": sum(
                len(module["preview"]["manufacturing"]["issues"]) for module in modules
            ),
            "module_count": len(modules),
            "part_count": part_count,
            "total_weight_g": sum(module["weight_g"] for module in modules),
            "assembly_qualified": False,
            "intermodule_connections": "unknown",
            "wall_anchorage": "unknown",
            "stacking_load_path": "unknown" if grid.rows > 1 else "not_applicable",
        },
        review_status="requires_review",
        can_export_drafts=True,
    )
    return response
