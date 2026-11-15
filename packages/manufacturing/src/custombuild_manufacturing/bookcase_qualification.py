"""Machine-neutral release of tested fixed bookcases at a qualified site.

The registry is part of the deployed, reviewed source. Orders cannot supply a
profile, report, exemption or approval. Removing an entry revokes future downloads.
"""

import hashlib
import json
from datetime import UTC, datetime
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from custombuild_domain.bookcase_site import WALL_NAMES, review_standing_site
from custombuild_domain.enums import MaterialType, PartRole
from custombuild_domain.furniture import (
    FURNITURE_ENGINE_VERSION,
    FurnitureWorkspace,
    ShelvingIntent,
    furniture_review_schema,
)
from custombuild_domain.furniture_catalog import CATALOG_VERSION, resolve_material
from custombuild_domain.furniture_dimensions import DIMENSION_REVIEW_VERSION
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from custombuild_domain.identity import content_hash
from custombuild_domain.models import BookcaseWidthUm
from custombuild_rules.engine import RULES_VERSION
from custombuild_rules.furniture import FURNITURE_RULES_VERSION, evaluate_furniture
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pypdf import PdfReader

from .bookcase_test_results import BookcaseTestResults, bookcase_test_limits
from .model import um_to_mm

REGISTRY = Path(__file__).with_name("bookcase_qualifications")
READBACK_SCHEMA = "custombuild.fixed-bookcase-readback.v2"
Category = Literal[
    "materials",
    "connection",
    "service_load",
    "stability",
    "installation",
    "door_service",
    "width_range",
]
REQUIRED_REPORTS = {"materials", "connection", "service_load", "stability", "installation"}


class EvidenceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: Category
    file: str = Field(pattern=r"^[A-Za-z0-9_-]{1,96}\.pdf$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _check_report_pdf(raw: bytes, name: str) -> None:
    # A matching digest identifies the file, but cannot establish that its pages
    # can be opened. Scanned instructions need no OCR or extractable text.
    try:
        reader = PdfReader(BytesIO(raw))
        if not reader.pages:
            raise ValueError("PDF saknar sidor")
        for page in reader.pages:
            if page.mediabox.width <= 0 or page.mediabox.height <= 0:
                raise ValueError("ogiltigt sidformat")
            page.get_contents()
    except Exception as exc:
        # The PDF reader may raise different parser/codec errors for a bad
        # page. Give the same actionable outcome at order, job and download.
        raise ValueError(
            f"Registrera en PDF för {name} där alla sidor kan öppnas utan lösenord."
        ) from exc


class TestedWidthRange(BaseModel):
    """The width-range report covers the entire interval at the specified site and lots."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    minimum_um: BookcaseWidthUm
    maximum_um: BookcaseWidthUm
    minimum_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self) -> "TestedWidthRange":
        if self.minimum_um >= self.maximum_um:
            raise ValueError("Breddintervallet behöver två skilda gränser i stigande ordning.")
        return self


class BookcaseProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    organization_id: str = Field(min_length=1, max_length=128)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    evidence: tuple[EvidenceFile, ...]
    physical_results: BookcaseTestResults
    width_range: TestedWidthRange | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def complete(self) -> "BookcaseProfile":
        required = REQUIRED_REPORTS | (
            {"door_service"} if self.physical_results.door_service else set()
        )
        if self.width_range:
            required = required | {"width_range"}
        if (
            self.valid_until <= self.valid_from
            or self.physical_results.tested_on > self.valid_from.date()
            or len(self.evidence) != len(required)
            or {r.category for r in self.evidence} != required
            or len({r.file for r in self.evidence}) != len(required)
        ):
            raise ValueError(
                "Provprofilen behöver giltighetstid och spårbara underlag för varje provkategori."
            )
        return self

    @property
    def fingerprint(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def reports(self) -> dict[str, bytes]:
        documents: dict[str, bytes] = {}
        for report in self.evidence:
            path = REGISTRY / self.profile_id / report.file
            raw = path.read_bytes() if path.is_file() and path.stat().st_size <= 2_000_000 else b""
            if not raw.startswith(b"%PDF-") or hashlib.sha256(raw).hexdigest() != report.sha256:
                raise ValueError(f"Provunderlaget för {report.category} saknas eller har ändrats.")
            _check_report_pdf(raw, report.file)
            documents[report.category] = raw
        return documents


def bookcase_scope(workspace: FurnitureWorkspace) -> str:
    # Fix only the customer identity. Measured stock, placements, both joint
    # members and the complete assembly sequence remain part of the tested scope.
    reference = build_furniture(
        workspace.design.model_copy(
            update={
                "design_id": "bookcase-type-test",
                "revision": 1,
            }
        )
    )
    if not reference.shelving_result:
        raise ValueError("Typkvalificering gäller fast bokhylla.")
    return content_hash(
        {
            "design": workspace.design.model_dump(mode="json", exclude={"design_id", "revision"}),
            "resolved_model": reference.design_hash,
            "assembly_graph": reference.shelving_result.assembly_graph,
            "physical_results": "custombuild.bookcase-physical-results.v2",
            "engine": FURNITURE_ENGINE_VERSION,
            "calculation_rules": RULES_VERSION,
            "family_rules": FURNITURE_RULES_VERSION,
            "dimensions": DIMENSION_REVIEW_VERSION,
            "catalogue": CATALOG_VERSION,
            "export": furniture_review_schema("shelving"),
            "readback": READBACK_SCHEMA,
        }
    )


def _at_width(workspace: FurnitureWorkspace, width: int) -> FurnitureWorkspace:
    data = workspace.model_dump(mode="json")
    data["design"]["intent"]["width_um"] = width
    data["design"]["installation"]["width_um"] += width - workspace.design.intent.width_um
    return FurnitureWorkspace.model_validate(data)


def _matching_profiles(
    workspace: FurnitureWorkspace, records: list[Any], organization_id: str, now: datetime
) -> tuple[list[BookcaseProfile], list[TestedWidthRange]]:
    design = workspace.design
    actual_width = design.intent.width_um

    @cache
    def scope_at(width: int) -> str | None:
        if width == actual_width:
            return bookcase_scope(workspace)
        # Preserve every other design and survey input, including all clearances,
        # both actual lots and the site ID. Rebuild both endpoint constructions.
        try:
            return bookcase_scope(_at_width(workspace, width))
        except ValueError:
            return None

    matches = []
    outside = []
    for record in records:
        if not isinstance(record, dict) or record.get("organization_id") != organization_id:
            continue
        bounds = None
        interval = record.get("width_range")
        if interval is None:
            if record.get("scope_sha256") != scope_at(actual_width):
                continue
        else:
            bounds = TestedWidthRange.model_validate(interval)
            if (
                record.get("scope_sha256") is None
                or scope_at(bounds.maximum_um) != record.get("scope_sha256")
                or scope_at(bounds.minimum_um) != bounds.minimum_scope_sha256
            ):
                continue
        profile = BookcaseProfile.model_validate(record)
        if profile.valid_from <= now < profile.valid_until:
            if bounds and not bounds.minimum_um <= actual_width <= bounds.maximum_um:
                outside.append(bounds)
            else:
                matches.append(profile)
    return matches, outside


def _checked_order(workspace: FurnitureWorkspace) -> tuple[FurnitureResult, list[dict[str, Any]]]:
    spec = workspace.design
    if (
        isinstance(spec.intent, ShelvingIntent)
        and spec.intent.base_doors
        and not spec.intent.trial_angle
    ):
        cabinet = build_furniture(spec).shelving_result
        assert cabinet is not None
        count = len(cabinet.unretained_carcass_joints)

        raise ValueError(
            "Underskåpens J95-infästning i valnöt och luckornas öppningsrörelse "
            "saknar typkvalificering. "
            f"{count} bärande stomfogar saknar specificerad permanent låsning."
        )
    if not isinstance(spec.intent, ShelvingIntent) or not spec.intent.trial_angle:
        raise ValueError("Välj fast bokhylla med GN 970-vinklar för verkstadsritningar.")
    design = build_furniture(spec)
    # A complete matched profile may resolve only these evidence-dependent rules.
    permitted = {
        "CB-SHELF-ANGLE-001": {"BLOCK"},
        "CB-SOLID-WOOD-001": {"BLOCK"},
        "CB-FURNITURE-MATERIAL-001": {"WARNING"},
        "CB-JOINT-001": {"WARNING"},
        "CB-TIP-001": {"WARNING"},
    }
    if spec.intent.base_doors:
        permitted["CB-HARDWARE-001"] = {"WARNING"}
    evaluations = evaluate_furniture(design)["evaluations"]
    problems = [
        r["detail"]
        for r in evaluations
        if r["status"] != "PASS" and r["status"] not in permitted.get(r["rule_id"], set())
    ]
    if problems:
        raise ValueError(" ".join(problems))
    site = spec.installation
    if not site or not site.observed("ceiling_height") or not site.observed("front_space"):
        raise ValueError("Ange uppmätt takhöjd och fritt utrymme framför möbeln.")
    assert design.shelving_result is not None
    survey = review_standing_site(design.shelving_result, site)
    missing = [WALL_NAMES[wall.wall] for wall in survey.walls if wall.measured_distance_um is None]
    if missing:
        raise ValueError(
            "Mät avståndet från färdig stomme till väggytan bakom eventuell list vid "
            + ", ".join(missing)
            + ". Ange platsmätningen separat från reservationen."
        )
    if not site.qualified_site_ref:
        raise ValueError("Ange plats-ID för provad vägginfästning, golv, fixturer och bruksmiljö.")
    if not spec.material.batch_id or not spec.back_material.batch_id:
        raise ValueError("Ange provad materialbatch för både stomme och rygg.")
    return design, evaluations


def qualified_bookcase(workspace: FurnitureWorkspace, organization_id: str) -> BookcaseProfile:
    design, evaluations = _checked_order(workspace)
    now = datetime.now(UTC)
    records = json.loads((REGISTRY / "profiles.json").read_bytes())
    if not isinstance(records, list):
        raise ValueError("Förteckningen över typkvalificeringar har ogiltigt format.")
    try:
        matches, outside = _matching_profiles(workspace, records, organization_id, now)
    except ValidationError as exc:
        raise ValueError(
            "Typunderlaget för ordern behöver giltiga rapporter och mätresultat "
            "för materialbatcher, förband, brukslast, stabilitet och monteringssteg."
        ) from exc
    if not matches and outside:
        ranges = ", ".join(
            f"{um_to_mm(lo)}–{um_to_mm(hi)} mm"
            for lo, hi in sorted({(r.minimum_um, r.maximum_um) for r in outside})
        )
        raise ValueError(
            f"Stombredden {um_to_mm(workspace.design.intent.width_um)} mm ligger "
            f"utanför registrerat intervall för denna konstruktion och plats: {ranges}."
        )
    if len(matches) != 1:
        raise ValueError(
            "Ingen entydigt kvalificerad profil för dessa mått, materialbatcher, "
            "beslag, brukslast och monteringsplats. Välj en provad kombination."
        )
    profile = matches[0]
    profile.reports()
    _check_test_results(profile, design, evaluations)
    if profile.width_range:
        # An interval with an invalid endpoint is invalid for interior orders as well.
        for width in (profile.width_range.minimum_um, profile.width_range.maximum_um):
            if width != workspace.design.intent.width_um:
                endpoint, rules = _checked_order(_at_width(workspace, width))
                _check_test_results(profile, endpoint, rules)
    return profile


def _check_test_results(
    profile: BookcaseProfile, design: FurnitureResult, evaluations: list[dict[str, Any]]
) -> None:
    spec = design.spec
    assert isinstance(spec.intent, ShelvingIntent) and design.shelving_result is not None
    if resolve_material(spec.material).material_type == MaterialType.SOLID_WOOD:
        movement = profile.physical_results.wood_movement
        if movement is None:
            raise ValueError("Massivträ kräver uppmätt rörelsemån över platsens fuktintervall.")
        movement.require_clearance()
    door_service = profile.physical_results.door_service
    if spec.intent.base_doors:
        if door_service is None:
            raise ValueError(
                "J95 kräver luckprov för varje fack med skruvfäste, frigång och livslängd."
            )
        door_service.require_openings(design.shelving_result)
    elif door_service is not None:
        raise ValueError("Luckprovet gäller inte den öppna bokhyllan.")
    profile.physical_results.carcass_lot.require_match(
        spec.material, "Stomme", tuple(p for p in design.parts if p.role != PartRole.BACK)
    )
    profile.physical_results.back_lot.require_match(
        spec.back_material, "Rygg", tuple(p for p in design.parts if p.role == PartRole.BACK)
    )
    profile.physical_results.require_capacity(
        bookcase_test_limits(design.shelving_result, evaluations)
    )


def workshop_filename(revision: int, qualification: object) -> str:
    purpose = "workshop-drawings" if qualification else "design-review"
    return f"custombuild-{purpose}-{revision}.zip"
