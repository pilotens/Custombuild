import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
} from "@/lib/design-constraints";
import { MATERIALS, type DesignSpec, type RuleEvaluation } from "@/lib/design-types";

export type ValidationGuidanceTarget =
  | { kind: "step"; step: 0 | 1 | 2 | 3; control: string }
  | { kind: "production"; control: string; gate?: "stockless_review" }
  | { kind: "none"; control: string };

export interface ValidationGuidance {
  problem: string;
  impact: string;
  solution: string;
  requiredInput: string;
  target: ValidationGuidanceTarget;
}

export interface AutomaticValidationFix {
  label: string;
  patch: Partial<DesignSpec>;
  reason: string;
}

export interface AutomaticValidationFixChange {
  field: keyof DesignSpec;
  before: unknown;
  after: unknown;
}

export interface AutomaticValidationFixPreview extends AutomaticValidationFix {
  changes: AutomaticValidationFixChange[];
}

function identity(evaluation: RuleEvaluation): string {
  return `${evaluation.rule_id} ${evaluation.title}`.toLowerCase();
}

function hasIdentity(evaluation: RuleEvaluation, ...needles: string[]): boolean {
  const value = identity(evaluation);
  return needles.some((needle) => value.includes(needle));
}

function diagnosticValue(evaluation: RuleEvaluation, label: string): string | undefined {
  return evaluation.diagnostics?.find((item) => item.label.toLowerCase().includes(label))?.value.toLowerCase();
}

function stocklessReviewEligible(evaluation: RuleEvaluation): boolean {
  return evaluation.status === "BLOCK"
    && evaluation.affected_part_ids.length > 0
    && evaluation.suggestion?.action === "create_stockless_review_package";
}

const STOCKLESS_REVIEW_BLOCKER_RULE_IDS = new Set([
  "DFM-MACHINE-001",
  "DFM-STOCK-001",
]);

export function permitsStocklessDesignReview(evaluations: RuleEvaluation[]): boolean {
  const blockers = evaluations.filter((evaluation) => evaluation.status === "BLOCK");
  const blockerIds = new Set(blockers.map((evaluation) => evaluation.rule_id));
  return (blockers.length === 1 || blockers.length === 2)
    && blockerIds.size === blockers.length
    && blockerIds.has("DFM-STOCK-001")
    && blockers.every((evaluation) => (
      STOCKLESS_REVIEW_BLOCKER_RULE_IDS.has(evaluation.rule_id)
      && stocklessReviewEligible(evaluation)
    ));
}

function stocklessReviewTarget(evaluation: RuleEvaluation): ValidationGuidanceTarget {
  return stocklessReviewEligible(evaluation)
    ? { kind: "production", control: "Lagerobundet granskningspaket", gate: "stockless_review" }
    : { kind: "none", control: "Ingen säker automatisk åtgärd" };
}

function targetForEvaluation(evaluation: RuleEvaluation): ValidationGuidanceTarget {
  if (hasIdentity(evaluation, "dfm-grain", "fiberriktning")) {
    return { kind: "none", control: "Ingen säker åtgärd i gränssnittet" };
  }
  if (hasIdentity(evaluation, "hardware", "beslag", "tip", "väggförank")) {
    return { kind: "production", control: "Kontrollpunkter och underlag" };
  }
  if (hasIdentity(evaluation, "material")) return { kind: "step", step: 3, control: "Material och utförande" };
  if (hasIdentity(evaluation, "geo", "maskin", "arbetsområde")) return { kind: "step", step: 1, control: "Yttermått" };
  return { kind: "step", step: 2, control: "Indelning och konstruktionsstöd" };
}

function knownGuidance(evaluation: RuleEvaluation): Omit<ValidationGuidance, "problem"> | undefined {
  if (hasIdentity(evaluation, "base-support", "cb-support", "lodrät lastväg")) {
    return {
      impact: "En övre avdelare utan en underskåpssida på samma centrumlinje belastar mellanbottnen som en overifierad punktlast.",
      solution: evaluation.suggestion?.explanation
        ?? "Rikta en fullhöjd underskåpssida under varje övre avdelare och räkna sedan om konstruktionen.",
      requiredInput: "Krav: samma centrumlinje för varje övre avdelare och underskåpssida, inom den tolerans som visas i kontrollvärdena.",
      target: { kind: "step", step: 2, control: "Underskåp → Skåpsmoduler" },
    };
  }
  if (hasIdentity(evaluation, "str-def", "cb-deflection", "hyllnedböj", "långtidsnedböj")) {
    return {
      impact: "För stor långtidsnedböjning kan ge permanent svikt, lutande hyllor och förband som belastas utanför den verifierade modellen.",
      solution: evaluation.suggestion?.explanation
        ?? "Minska den fria spännvidden med fler fullhöga avdelare, sänk hyllasten eller välj ett styvare versionshanterat material.",
      requiredInput: evaluation.allowed_value !== undefined
        ? `Mål: beräknad nedböjning högst ${evaluation.allowed_value} ${evaluation.unit ?? ""}.`.trim()
        : "Kontrollen saknar en uttrycklig numerisk gräns. Verifiera gränsen i regelunderlaget innan konstruktionen godkänns.",
      target: { kind: "step", step: 2, control: "Konstruktionsstöd → Vertikala avdelare" },
    };
  }
  if (hasIdentity(evaluation, "cb-bending", "böjspänning")) {
    return {
      impact: "En hylla som överskrider verifierad böjhållfasthet kan deformeras eller brista under den angivna lasten.",
      solution: evaluation.suggestion?.explanation
        ?? "Minska spännvidden med fler avdelare. Alternativt sänk lasten eller välj ett tjockare material med verifierad hållfasthet.",
      requiredInput: `Mål: beräknad spänning högst ${evaluation.allowed_value ?? "regelns gräns"} ${evaluation.unit ?? ""}.`.trim(),
      target: { kind: "step", step: 2, control: "Konstruktionsstöd → Vertikala avdelare" },
    };
  }
  if (hasIdentity(evaluation, "stab-rack", "cb-stability", "sidostabilitet", "skevning")) {
    return {
      impact: "Utan verifierad stagning kan stommen skeva, förbanden öppna sig och möbeln förlora sidostabilitet.",
      solution: evaluation.suggestion?.explanation
        ?? "Lägg till ett infällt bakstycke som kopplas till stommen och verifiera dess infästning.",
      requiredInput: "Kontroll: Material och utförande → Bakstycke ska vara aktiverat; infästningen verifieras i tillverkningsunderlaget.",
      target: { kind: "step", step: 3, control: "Material och utförande → Bakstycke" },
    };
  }
  if (hasIdentity(evaluation, "stab-tip", "cb-tip", "tipprisk", "väggförank")) {
    return {
      impact: "En hög eller grund möbel kan välta när den belastas eller utsätts för en horisontell kraft.",
      solution: "Fastställ väggtypen, välj ett förankringssystem godkänt för både vägg och last och registrera verifieringen innan revisionen låses.",
      requiredInput: "Underlag: väggtyp, infästningens fabrikat/SKU, tillverkarens lastdata, antal infästningar och monteringsanvisning.",
      target: { kind: "production", control: "Kontrollpunkt → Tipprisk och väggförankring" },
    };
  }
  if (hasIdentity(evaluation, "cb-hardware", "beslag och borrbild")) {
    return {
      impact: "Fel gångjärn, frontspel eller borrbild kan ge kollisioner, svaga infästningar och obrukbara fronter.",
      solution: "Välj ett versionslåst gångjärn och montageplatta, kontrollera öppningsvinkel och frontspel och verifiera borrbilden mot uppmätt skivtjocklek.",
      requiredInput: "Underlag: fabrikat, exakt SKU, databladets revision, öppningsvinkel, frontspel, koppmått och montageplattans borrbild.",
      target: { kind: "production", control: "Kontrollpunkt → Beslag och borrbild för underskåp" },
    };
  }
  if (hasIdentity(evaluation, "dfm-grain", "fiberriktning")) {
    return {
      impact: "En okänd råskiveaxel kan rotera riktade delar fel och gör nesting, operationer och CAM geometriskt opålitliga.",
      solution: "Serverbind en exakt X- eller Y-axel i den versionslåsta lagerprofilen för varje riktat skivmaterial. Ett uppladdat dokument eller ett varningsgodkännande kan inte ersätta denna strukturerade bindning.",
      requiredInput: "Strukturerad lagerprofil med material-ID, materialversion, verkligt skiv-ID och fiberriktningsaxel X eller Y.",
      target: { kind: "none", control: "Ingen säker åtgärd i gränssnittet" },
    };
  }
  if (hasIdentity(evaluation, "dfm-machine", "maskinens arbetsområde")) {
    const stocklessEligible = stocklessReviewEligible(evaluation);
    return {
      impact: "En del utanför maskinens verifierade arbetsområde kan inte spännas upp eller bearbetas med den valda profilen.",
      solution: stocklessEligible && evaluation.suggestion?.explanation
        ? evaluation.suggestion.explanation
        : "Välj och serverbind en versionshanterad maskinprofil vars verifierade arbetsområde rymmer varje del; annars måste konstruktionen ändras innan nesting eller CAM får skapas.",
      requiredInput: "Underlag: verklig maskinprofil, versions-ID, verifierat arbetsområde, tillåten rotation och separat fixturplan.",
      target: stocklessReviewTarget(evaluation),
    };
  }
  if (hasIdentity(evaluation, "dfm-stock", "råmaterial")) {
    const stocklessEligible = stocklessReviewEligible(evaluation);
    return {
      impact: "En del som inte ryms i valt råformat kan inte ingå i en giltig nesting eller produceras från den valda skivan.",
      solution: stocklessEligible && evaluation.suggestion?.explanation
        ? evaluation.suggestion.explanation
        : "Bind ett exakt verifierat antal skivor som rymmer hela behovet, eller välj en serverbunden lagerprofil som passar varje del; skapa inte nesting eller CAM innan kapaciteten räcker.",
      requiredInput: "Underlag: serverbunden lagerprofil med material-ID, version, tjocklek, råformat, antal och verifierad fiberriktning.",
      target: stocklessReviewTarget(evaluation),
    };
  }
  if (hasIdentity(evaluation, "cb-joint", "hyllspår", "hyllbärare")) {
    const adjustable = diagnosticValue(evaluation, "hylltyp")?.includes("adjustable");
    return {
      impact: "Ett spår kan bära lokalt men ändå dras isär. Utan verifierad självlåsning eller mekanisk säkring är det inte ett permanent låsförband.",
      solution: adjustable
        ? "Verifiera exakt hyllbärare och borrbild. Stommen måste dessutom få ett versionslåst självlåsande torrförband eller en demonterbar mekanisk säkring."
        : "Välj och versionslås ett självlåsande torrförband eller en demonterbar mekanisk säkring som hindrar isärdragning. Lim, fogmassa och epoxy är förbjudna.",
      requiredInput: adjustable
        ? "Automatisk lösning: Material och utförande → Fasta hyllor. Alternativt underlag: hyllbärarens SKU, datablad, kapacitet, materialkompatibilitet och borrbild."
        : `Underlag: förbandstyp och version, monteringsriktning, mekanisk hållning mot isärdragning samt lokal kapacitet minst ${evaluation.calculated_value ?? "den beräknade lasten"} ${evaluation.unit ?? ""}.`.trim(),
      target: adjustable
        ? { kind: "step", step: 3, control: "Material och utförande → Fasta hyllor" }
        : {
            kind: "none",
            control: "Förbandsval saknas i nuvarande produktions-MVP",
          },
    };
  }
  if (hasIdentity(evaluation, "str-topo", "sammanhängande bärande geometri")) {
    return {
      impact: "Saknade ram-, avdelar- eller hyllsegment lämnar öppna förband eller delar utan kontinuerligt upplag.",
      solution: "Återställ de borttagna bärande delarna. Ändra därefter indelningen parametriskt så att angränsande delar och förband räknas om tillsammans.",
      requiredInput: `Delar som ska återställas: ${evaluation.affected_part_ids.join(", ") || "de delar som anges av kontrollen"}.`,
      target: { kind: "step", step: 2, control: "Indelning och konstruktionsstöd" },
    };
  }
  if (hasIdentity(evaluation, "part-custom", "individuellt ändrade delar", "ombyggd bärande")) {
    return {
      impact: "Fria deländringar kan bryta förbandskedjan och gör att den parametriska modellen inte längre garanterar upplag, kollisioner eller CAM-operationer.",
      solution: "Återställ den senast parametriska indelningen och gör ändringen med mått-, hyll- och fackkontrollerna så att alla anslutande delar byggs om tillsammans.",
      requiredInput: "Automatisk lösning: återställ borttagna och individuellt modifierade delar samt den föregående parametriska indelningen.",
      target: { kind: "step", step: 2, control: "Indelning och konstruktionsstöd" },
    };
  }
  if (hasIdentity(evaluation, "geo-001", "giltig parametrisk geometri")) {
    return {
      impact: "Ogiltiga grundmått kan skapa negativa innerdimensioner, överlappande delar eller en modell som CAD/CAM inte kan generera.",
      solution: "Återställ berörda mått, antal och fria indelningar till närmaste giltiga parametriska värde och kontrollera sedan modellen på nytt.",
      requiredInput: `Krav: positiva innerdimensioner, ${DESIGN_CONSTRAINTS.shelfCount.minimum}–${DESIGN_CONSTRAINTS.shelfCount.maximum} hyllor, ${DESIGN_CONSTRAINTS.dividerCount.minimum}–${DESIGN_CONSTRAINTS.dividerCount.maximum} avdelare, minst 8 % fackbredd och minst 5 % mellan hyllnivåer.`,
      target: { kind: "step", step: 1, control: "Yttermått" },
    };
  }
  return undefined;
}

export function validationGuidance(
  evaluation: RuleEvaluation,
  _spec?: DesignSpec,
): ValidationGuidance {
  void _spec;
  if (evaluation.status === "PASS") {
    return {
      problem: evaluation.summary,
      impact: "Kontrollen är uppfylld för den aktuella konstruktionen och de indata som regeln redovisar.",
      solution: "Ingen ändring krävs. Kör kontrollen på nytt om konstruktion, material eller verifierat underlag ändras.",
      requiredInput: evaluation.diagnostics?.length
        ? `Verifierat kontrollunderlag: ${evaluation.diagnostics.map((item) => `${item.label} ${item.value}${item.unit ? ` ${item.unit}` : ""}`).join("; ")}.`
        : "Behåll den aktuella konstruktionen och kontrollera regeln på nytt efter nästa designändring.",
      target: targetForEvaluation(evaluation),
    };
  }
  const known = knownGuidance(evaluation);
  if (known) return { problem: evaluation.summary, ...known };

  const target = targetForEvaluation(evaluation);
  const targetName = target.kind === "step" ? target.control : target.control;
  return {
    problem: evaluation.summary,
    impact: evaluation.status === "BLOCK"
      ? "Kontrollen skyddar mot en konstruktion eller tillverkningsoperation som ännu inte är verifierad. Revisionen får därför inte låsas innan felet är löst."
      : "Om kontrollen lämnas obekräftad kan underlaget bygga på ett antagande som inte gäller för den verkliga möbeln eller verkstaden.",
    solution: evaluation.suggestion?.explanation
      ?? `Öppna ${targetName}, ändra det värde som kontrollen pekar ut och fortsätt tills regeln räknas om som Godkänd.`,
    requiredInput: evaluation.diagnostics?.length
      ? `Utgå från kontrollvärdena: ${evaluation.diagnostics.map((item) => `${item.label} ${item.value}${item.unit ? ` ${item.unit}` : ""}`).join("; ")}.`
      : evaluation.calculated_value !== undefined || evaluation.allowed_value !== undefined
        ? "Utgå endast från de beräkningsvärden och gränser som kontrollen redovisar. Saknat underlag ska registreras i tillverkningsunderlaget."
        : "Kontrollen redovisar inget numeriskt kontrollvärde. Saknat underlag ska registreras och verifieras innan revisionen godkänns.",
    target,
  };
}

function normalizedGeometryPatch(spec: DesignSpec): Partial<DesignSpec> {
  const finiteBounded = (
    value: number,
    fallback: number,
    minimum: number,
    maximum: number,
  ) => Math.min(maximum, Math.max(minimum, Number.isFinite(value) ? value : fallback));
  const boundedInteger = (
    value: number,
    fallback: number,
    minimum: number,
    maximum: number,
  ) => Math.min(maximum, Math.max(minimum, Math.round(Number.isFinite(value) ? value : fallback)));
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const thicknessCandidate = Number.isFinite(spec.measured_thickness_mm) && spec.measured_thickness_mm > 0
    ? spec.measured_thickness_mm
    : material.measuredThicknessMm;
  const thickness = finiteBounded(thicknessCandidate, material.measuredThicknessMm, 17, 19);
  const shelfCount = boundedInteger(
    spec.shelf_count,
    0,
    DESIGN_CONSTRAINTS.shelfCount.minimum,
    DESIGN_CONSTRAINTS.shelfCount.maximum,
  );
  const dividerCount = boundedInteger(
    spec.divider_count,
    0,
    DESIGN_CONSTRAINTS.dividerCount.minimum,
    DESIGN_CONSTRAINTS.dividerCount.maximum,
  );
  const baseCabinetCount = spec.furniture_type === "wall_library"
    ? boundedInteger(
        spec.base_cabinet_count,
        1,
        1,
        DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum,
      )
    : 0;
  const depth = finiteBounded(
    spec.depth_mm,
    320,
    DESIGN_CONSTRAINTS.depthMm.minimum,
    DESIGN_CONSTRAINTS.depthMm.maximum,
  );
  const bayCount = dividerCount + 1;
  const minimumShelfBayWidthMm = DESIGN_CONSTRAINTS.minimumShelfWidthMm + 2;
  const minimumWidthForShelves = bayCount * minimumShelfBayWidthMm + (bayCount + 1) * thickness;
  const minimumWidthForBases = baseCabinetCount > 0
    ? baseCabinetCount * DESIGN_CONSTRAINTS.minimumBaseCabinetOpeningMm
      + (baseCabinetCount + 1) * thickness
    : 0;
  const width = Math.min(
    DESIGN_CONSTRAINTS.widthMm.maximum,
    Math.max(
      finiteBounded(
        spec.width_mm,
        1_200,
        DESIGN_CONSTRAINTS.widthMm.minimum,
        DESIGN_CONSTRAINTS.widthMm.maximum,
      ),
      minimumWidthForShelves,
      minimumWidthForBases,
    ),
  );
  const plinthHeightMm = spec.plinth ? spec.plinth_height_mm : 0;
  const minimumShelfZoneHeightMm = shelfCount === 0
    ? DESIGN_CONSTRAINTS.minimumShelfOpeningMm
    : shelfCount === 1
      ? 2 * DESIGN_CONSTRAINTS.minimumShelfOpeningMm + thickness
      : (shelfCount + 1) * (DESIGN_CONSTRAINTS.minimumShelfOpeningMm + thickness);
  let baseHeight = spec.furniture_type === "wall_library"
    ? finiteBounded(
        spec.base_cabinet_height_mm,
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        DESIGN_CONSTRAINTS.baseCabinetHeightMm.maximum,
      )
    : 0;
  if (spec.furniture_type === "wall_library") {
    const maximumBaseHeightAtEnvelope = Math.min(
      maximumBaseCabinetHeightMm(DESIGN_CONSTRAINTS.heightMm.maximum, thickness),
      Math.floor(
        DESIGN_CONSTRAINTS.heightMm.maximum
        - thickness
        - plinthHeightMm
        - minimumShelfZoneHeightMm,
      ),
    );
    baseHeight = Math.min(baseHeight, maximumBaseHeightAtEnvelope);
  }
  const minimumHeightForShelves = spec.furniture_type === "wall_library"
    ? plinthHeightMm + baseHeight + thickness + minimumShelfZoneHeightMm
    : plinthHeightMm + 2 * thickness + minimumShelfZoneHeightMm;
  const minimumHeightForBaseClearance = spec.furniture_type === "wall_library"
    ? Math.floor(baseHeight + thickness + DESIGN_CONSTRAINTS.baseCabinetUpperClearanceMm) + 1
    : 0;
  const height = Math.min(
    DESIGN_CONSTRAINTS.heightMm.maximum,
    Math.max(
      finiteBounded(
        spec.height_mm,
        2_100,
        DESIGN_CONSTRAINTS.heightMm.minimum,
        DESIGN_CONSTRAINTS.heightMm.maximum,
      ),
      Math.ceil(minimumHeightForShelves),
      minimumHeightForBaseClearance,
    ),
  );
  return {
    nominal_thickness_mm: material.nominalThicknessMm,
    measured_thickness_mm: thickness,
    width_mm: width,
    height_mm: height,
    depth_mm: depth,
    shelf_count: shelfCount,
    divider_count: dividerCount,
    load_per_shelf_kg: finiteBounded(
      spec.load_per_shelf_kg,
      0,
      DESIGN_CONSTRAINTS.shelfLoadKg.minimum,
      DESIGN_CONSTRAINTS.shelfLoadKg.maximum,
    ),
    bay_sizing_mode: "count",
    bay_width_ratios: [],
    shelf_height_ratios: [],
    symmetry_locked: true,
    reinforcement_mode: "manual",
    ...(spec.furniture_type === "wall_library"
      ? {
          base_cabinet_count: baseCabinetCount,
          base_cabinet_height_mm: baseHeight,
          base_cabinet_depth_mm: depth,
        }
      : {
          base_cabinet_count: 0,
          base_cabinet_height_mm: 0,
          base_cabinet_depth_mm: 0,
        }),
  };
}

export function automaticValidationFix(
  evaluation: RuleEvaluation,
  spec: DesignSpec,
): AutomaticValidationFix | undefined {
  const suggestion = evaluation.suggestion;
  if (suggestion?.action === "set_divider_count" && typeof suggestion.value === "number") {
    const dividerCount = Math.max(0, Math.min(16, Math.round(suggestion.value)));
    return {
      label: suggestion.label,
      patch: {
        reinforcement_mode: "manual",
        divider_count: dividerCount,
        bay_sizing_mode: "count",
        bay_width_ratios: [],
        symmetry_locked: true,
        ...(spec.furniture_type === "wall_library" ? { base_cabinet_count: dividerCount + 1 } : {}),
      },
      reason: suggestion.explanation,
    };
  }
  if (suggestion?.action === "align_base_cabinets" && typeof suggestion.value === "number") {
    return {
      label: suggestion.label,
      patch: {
        base_cabinet_count: Math.max(1, Math.min(17, Math.round(suggestion.value))),
        bay_width_ratios: [],
        symmetry_locked: true,
      },
      reason: suggestion.explanation,
    };
  }
  if (suggestion?.action === "enable_back") {
    return {
      label: suggestion.label,
      patch: { back_panel: true },
      reason: suggestion.explanation,
    };
  }
  if (hasIdentity(evaluation, "geo-001", "giltig parametrisk geometri")) {
    return {
      label: "Återställ giltig geometri",
      patch: normalizedGeometryPatch(spec),
      reason: "Ogiltiga mått och fria indelningar återställdes till närmaste giltiga parametriska värden",
    };
  }
  if (hasIdentity(evaluation, "str-topo", "sammanhängande bärande geometri")) {
    return {
      label: "Återställ saknade bärande delar",
      patch: { removed_part_ids: [] },
      reason: "Saknade bärande delar återställdes och modellen byggdes om",
    };
  }
  if (hasIdentity(evaluation, "part-custom", "individuellt ändrade delar", "ombyggd bärande")) {
    const baseline = spec.topology_baseline;
    return {
      label: "Återställ parametrisk konstruktion",
      patch: {
        removed_part_ids: [],
        part_overrides: {},
        topology_baseline: undefined,
        ...(baseline
          ? {
              divider_count: baseline.divider_count,
              shelf_count: baseline.shelf_count,
              base_cabinet_count: baseline.base_cabinet_count,
              bay_width_ratios: baseline.bay_width_ratios,
              shelf_height_ratios: baseline.shelf_height_ratios,
              bay_sizing_mode: "count",
              reinforcement_mode: "manual",
            }
          : {}),
      },
      reason: "Individuella deländringar återställdes till den föregående parametriska konstruktionen",
    };
  }
  if (
    hasIdentity(evaluation, "cb-joint", "hyllspår", "hyllbärare")
    && diagnosticValue(evaluation, "hylltyp")?.includes("adjustable")
  ) {
    return {
      label: "Byt till fasta hyllor",
      patch: { fixed_shelves: true },
      reason: "Fasta hyllor valdes så att bärande spåranslutningar kan genereras för fortsatt designgranskning",
    };
  }
  return undefined;
}

function sameDesignValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

/**
 * Returns the exact DesignSpec fields an existing deterministic fix would change.
 * It never invents a patch and omits no-op fields so the confirmation describes
 * the single workspace transaction truthfully.
 */
export function automaticValidationFixPreview(
  evaluation: RuleEvaluation,
  spec: DesignSpec,
): AutomaticValidationFixPreview | undefined {
  const fix = automaticValidationFix(evaluation, spec);
  if (!fix) return undefined;

  const changes = (Object.keys(fix.patch) as Array<keyof DesignSpec>)
    .map((field): AutomaticValidationFixChange => ({
      field,
      before: spec[field],
      after: fix.patch[field],
    }))
    .filter((change) => !sameDesignValue(change.before, change.after));

  return changes.length > 0 ? { ...fix, changes } : undefined;
}
