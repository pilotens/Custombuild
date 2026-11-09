"""Machine-neutral workshop release for an exactly tested fixed bookcase and site.

The registry is part of the deployed, reviewed source. Orders cannot supply a
profile, report, exemption or approval. Removing an entry revokes future downloads.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from custombuild_domain.bookcase_site import WALL_NAMES, review_standing_site
from custombuild_domain.furniture import (
    FURNITURE_ENGINE_VERSION,
    FurnitureWorkspace,
    ShelvingIntent,
    furniture_review_schema,
)
from custombuild_domain.furniture_catalog import CATALOG_VERSION
from custombuild_domain.furniture_dimensions import DIMENSION_REVIEW_VERSION
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.identity import content_hash
from custombuild_rules.engine import RULES_VERSION
from custombuild_rules.furniture import FURNITURE_RULES_VERSION, evaluate_furniture
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .bookcase_test_results import BookcaseTestResults, bookcase_test_limits

REGISTRY = Path(__file__).with_name("bookcase_qualifications")
READBACK_SCHEMA = "custombuild.fixed-bookcase-readback.v2"
Category = Literal["materials", "connection", "service_load", "stability", "installation"]
REQUIRED_REPORTS = {"materials", "connection", "service_load", "stability", "installation"}


class EvidenceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: Category
    file: str = Field(pattern=r"^[A-Za-z0-9_-]{1,96}\.pdf$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BookcaseProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    organization_id: str = Field(min_length=1, max_length=128)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    evidence: tuple[EvidenceFile, ...]
    physical_results: BookcaseTestResults

    @model_validator(mode="after")
    def complete(self) -> "BookcaseProfile":
        if (
            self.valid_until <= self.valid_from
            or self.physical_results.tested_on > self.valid_from.date()
            or len(self.evidence) != len(REQUIRED_REPORTS)
            or {r.category for r in self.evidence} != REQUIRED_REPORTS
            or len({r.file for r in self.evidence}) != len(REQUIRED_REPORTS)
        ):
            raise ValueError("Provprofilen behöver giltighetstid och fem spårbara provunderlag.")
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
            "physical_results": "custombuild.bookcase-physical-results.v1",
            "engine": FURNITURE_ENGINE_VERSION,
            "calculation_rules": RULES_VERSION,
            "family_rules": FURNITURE_RULES_VERSION,
            "dimensions": DIMENSION_REVIEW_VERSION,
            "catalogue": CATALOG_VERSION,
            "export": furniture_review_schema("shelving"),
            "readback": READBACK_SCHEMA,
        }
    )


def qualified_bookcase(workspace: FurnitureWorkspace, organization_id: str) -> BookcaseProfile:
    spec = workspace.design
    if isinstance(spec.intent, ShelvingIntent) and spec.intent.base_doors:
        raise ValueError(
            "Underskåpens J95-infästning i valnöt och luckornas öppningsrörelse "
            "saknar typkvalificering. Granskningspaket med borrbilder är tillgängligt."
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
    now = datetime.now(UTC)
    records = json.loads((REGISTRY / "profiles.json").read_bytes())
    if not isinstance(records, list):
        raise ValueError("Förteckningen över typkvalificeringar har ogiltigt format.")
    scope = bookcase_scope(workspace)
    candidates = [
        r
        for r in records
        if isinstance(r, dict)
        and r.get("organization_id") == organization_id
        and r.get("scope_sha256") == scope
    ]
    try:
        profiles = TypeAdapter(list[BookcaseProfile]).validate_python(candidates)
    except ValidationError as exc:
        raise ValueError(
            "Typunderlaget för ordern behöver giltiga rapporter och mätresultat "
            "för förband, brukslast, stabilitet och monteringssteg."
        ) from exc
    matches = [p for p in profiles if p.valid_from <= now < p.valid_until]
    if len(matches) != 1:
        raise ValueError(
            "Ingen entydigt kvalificerad profil för dessa mått, materialbatcher, "
            "beslag, brukslast och monteringsplats. Välj en provad kombination."
        )
    profile = matches[0]
    profile.reports()
    profile.physical_results.require_capacity(
        bookcase_test_limits(design.shelving_result, evaluations)
    )
    return profile


def workshop_filename(revision: int, qualification: object) -> str:
    purpose = "workshop-drawings" if qualification else "design-review"
    return f"custombuild-{purpose}-{revision}.zip"
