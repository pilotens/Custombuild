"""Revision-bound trial preparation; never an authorization to cut or load furniture."""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.identity import content_hash

TRIAL_READINESS_VERSION = "furniture-trial-readiness-1.0.0"


def furniture_trial_readiness(
    workspace: FurnitureWorkspace,
    result: FurnitureResult,
    rules: dict[str, Any],
    manufacturing: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    """Describe proven design checks and the evidence still needed for this snapshot.

    A typed batch name, reference machine or passing screening rule cannot stand
    in for a workshop's independently qualified material, setup or physical test.
    Production candidate/run evidence lives in a separate authenticated workflow.
    """
    checks: list[dict[str, Any]] = []

    def add(
        code: str,
        title: str,
        state: str,
        detail: str,
        action: str,
        evidence: list[str] | None = None,
    ) -> None:
        checks.append(
            {
                "code": code,
                "title": title,
                "state": state,
                "detail": detail,
                "action": action,
                "evidence": evidence or [],
            }
        )

    shelving = result.spec.intent.family == "shelving"
    add(
        "FAMILY_SCOPE",
        "Möbelfamiljens tillverkningsstöd",
        "checked" if shelving else "blocked",
        "Hyllsystemets delar och bearbetningar kan överföras till separat CAM-beredning."
        if shelving
        else "Bord och byråer har parametrisk geometri men saknar komplett tillverkningsstöd "
        "för beslag, hålbilder och infästningar.",
        "Bered den sparade hyllrevisionen med den verkliga verkstadens uppgifter."
        if shelving
        else "Färdigställ och verifiera familjens beslag, bearbetningar och tillverkningsflöde "
        "innan ett tillverkningsprov bokas.",
        [f"Möbelfamilj: {result.spec.intent.family}"],
    )
    dimensions = handoff["dimensions"]
    dimension_issues = dimensions["issues"]
    add(
        "DIMENSION_CHAIN",
        "Kundmått, list och montageutrymme",
        "blocked"
        if dimension_issues
        else "checked"
        if dimensions["installation"] is not None
        else "requires_evidence",
        " ".join(issue["message"] for issue in dimension_issues)
        if dimension_issues
        else "Modellens måttkedja är konsekvent med angivna installationsmått. "
        "Verkliga platsmått är ännu inte verifierade."
        if dimensions["installation"] is not None
        else "Modellmått finns, men kundens installationsmått och montageutrymme är inte angivna.",
        "Mät platsen och fastställ alla sex montageutrymmen samt listens funktion, placering "
        "och mått. Räkna om stommen om måttkedjan ändras.",
        [str(issue["code"]) for issue in dimension_issues],
    )
    stock_issues = manufacturing["issues"]
    stock_selected = manufacturing["state"] != "not_selected"
    add(
        "STOCK_AND_WORK_AREA",
        "Råformat, fiberriktning och arbetsområde",
        "blocked" if stock_issues else "checked" if stock_selected else "requires_evidence",
        " ".join(issue["message"] for issue in stock_issues)
        if stock_issues
        else "Varje del ryms geometriskt i de valda råformaten och referensmaskinens arbetsområde. "
        "Nesting, verktygsutrymme och uppspänning återstår."
        if stock_selected
        else "Verkliga råformat och maskinens arbetsområde återstår att välja och kontrollera.",
        "Ange tillgängligt råformat, fiberriktning och kantmarginal per material. "
        "Överstora delar kräver större råformat/arbetsområde eller en uttryckligen omkonstruerad "
        "och verifierad modulindelning. Fler hyllfack delar inte topp, botten eller sidor.",
        [str(issue["code"]) for issue in stock_issues],
    )
    blocked_rules = [rule for rule in rules["evaluations"] if rule["status"] == "BLOCK"]
    add(
        "CONSTRUCTION_SCREENING",
        "Bärighet och stabilitet",
        "blocked" if blocked_rules else "requires_evidence",
        " ".join(f"{rule['title']}: {rule['detail']}" for rule in blocked_rules)
        if blocked_rules
        else "Inga blockerande screeningregler finns. Beräkningarnas antaganden, lastfall "
        "och verkliga material måste fortfarande styrkas.",
        "Åtgärda blockerande konstruktionsregler. Fastställ användningslast, förankring och "
        "verifiering av nedböjning, stabilitet och förband för hela möbeln.",
        [str(rule["rule_id"]) for rule in blocked_rules],
    )
    used_materials = sorted(
        {
            (part.material_id, part.material_version, part.actual_thickness_um)
            for part in result.parts
        }
    )
    add(
        "ACTUAL_MATERIAL",
        "Verkligt material och uppmätt batch",
        "requires_evidence",
        "Materialkatalogens värden används för screening. Inskrivet batchnamn och tjocklek "
        "är inte ett materialintyg eller ett verifierat fogprov. Ek är ännu inte kvalificerat "
        "i den här möbelkatalogen.",
        "Dokumentera verkligt material, leverantör, batch, tjockleksvariation och egenskaper "
        "för både synliga och dolda delar. Beräkna om och granska revisionen vid materialbyte.",
        [
            f"{material}@{version}, {thickness / 1_000:.3f} mm"
            for material, version, thickness in used_materials
        ],
    )
    add(
        "ADHESIVE_FREE_RETENTION",
        "Limfria förband och mekanisk låsning",
        "requires_evidence",
        "Passande geometri bevisar inte utdragssäkerhet, mekanisk låsning eller hållfasthet. "
        "Lim räknas inte som en lösning på saknad förbandskvalificering.",
        "Verifiera mekanisk låsning och bärighet med dokumenterade fogprov i samma material, "
        "tjocklek, verktyg och passning som den aktuella revisionen.",
        list(handoff["hardware_requirements"]),
    )
    add(
        "ASSEMBLY_AND_INSTALLATION",
        "Monteringsordning, transport och installation",
        "requires_evidence",
        "En kollisionsfri slutmodell bevisar inte att delarna går att föra ihop, transportera "
        "eller resa i kundens rum.",
        "Verifiera monteringsordning, verktygsåtkomst, demontering, transportväg, resningshöjd "
        "och avsedd förankring med den aktuella konstruktionen.",
    )
    add(
        "AGREED_TOLERANCES",
        "Avtalade kontrollmått och godkännandegränser",
        "requires_evidence",
        "Modellens featuretoleranser är inte ett avtal om hela möbelns mått, diagonaler, "
        "planhet eller fogpassning. Tomma mätfält är inte godkända resultat.",
        "Fastställ acceptansgränser före provet och använd delprotokollet samt "
        "inspection/assembly-checks.csv för mätning, resultat och granskare.",
    )
    add(
        "WORKSHOP_CAM",
        "Verklig verkstad och verifierad CAM",
        "requires_evidence",
        "Denna designgranskning granskar inte ett fryst körpaket eller en fysisk maskinkörning. "
        "En referensprofil kvalificerar ingen verklig verkstad.",
        "Bind CAM till rätt revision, material, maskin, verktyg, uppspänning, sidregistrering "
        "och postprocessor. Verifiera program, simulering och oberoende granskning. "
        "Ett verkstadsbyte kräver ny beredning och granskning.",
    )
    add(
        "PHYSICAL_TRIAL",
        "Stegvis prov och slutlig mätning",
        "requires_evidence",
        "Inget genomfört fysiskt prov eller godkänt mätresultat är knutet till denna rapport.",
        "Börja med fogprov, därefter representativ referensdel och slutligen komplett montering. "
        "Mät mot i förväg avtalade gränser och stoppa vid avvikelse innan nästa kostnadssteg.",
    )
    counts = {
        state: sum(check["state"] == state for check in checks)
        for state in ("checked", "blocked", "requires_evidence")
    }
    next_check = next(
        (check for check in checks if check["state"] == "blocked"),
        next(check for check in checks if check["state"] == "requires_evidence"),
    )
    report = {
        "schema_version": "custombuild.furniture-trial-readiness.v1",
        "version": TRIAL_READINESS_VERSION,
        "scope": "design_review_preparation",
        "design_hash": result.design_hash,
        "workspace_sha256": content_hash(workspace.model_dump(mode="json")),
        "revision": workspace.design.revision,
        "state": "requires_design_change" if counts["blocked"] else "requires_workshop_evidence",
        "checks": checks,
        "counts": counts,
        "blocker_count": counts["blocked"],
        "evidence_required_count": counts["requires_evidence"],
        "next_action": next_check["action"],
        "production_qualified": False,
        "physical_cutting_authorized": False,
    }
    return {**report, "report_sha256": content_hash(report)}


def furniture_trial_markdown(report: dict[str, Any]) -> bytes:
    """Readable copy of the same report, including identities and unfilled evidence."""
    states = {
        "checked": "KONTROLLERAT I MODELLEN",
        "blocked": "BLOCKERAR",
        "requires_evidence": "BEVIS ÅTERSTÅR",
    }
    lines = [
        "# Beredning inför tillverkningsprov",
        "",
        "Designgranskning. Inte godkännande för fysisk skärning eller belastningsprov.",
        "",
        f"Revision: {report['revision']}",
        f"Design: {report['design_hash']}",
        f"Arbetsfil: {report['workspace_sha256']}",
        f"Rapport: {report['report_sha256']}",
        f"Rapportversion: {report['version']}",
        "",
        f"Nästa steg: {report['next_action']}",
    ]
    for check in report["checks"]:
        lines.extend(
            [
                "",
                f"## {states[check['state']]} – {check['title']}",
                "",
                check["detail"],
                "",
                f"Åtgärd: {check['action']}",
            ]
        )
        if check["evidence"]:
            lines.extend(["", "Underlag: " + "; ".join(check["evidence"])])
    return ("\n".join(lines) + "\n").encode("utf-8")
