import type { DesignSpec, RuleEvaluation } from "@/lib/design-types";

export interface ValidationPresentation {
  title: string;
  summary: string;
  category: "design" | "workshop";
}

type RuleCopy = Omit<ValidationPresentation, "category">;

const DESIGN_COPY: Readonly<Record<string, RuleCopy>> = {
  "GEO-001": {
    title: "Justera måtten",
    summary: "Några mått eller avstånd behöver ändras för att alla delar ska få plats.",
  },
  "BASE-SUPPORT-001": {
    title: "Ge avdelarna stöd",
    summary: "Avdelarna behöver stå över underskåpens sidor så att lasten får stöd hela vägen ner.",
  },
  "CB-SUPPORT-001": {
    title: "Ge avdelarna stöd",
    summary: "Avdelarna behöver stå över underskåpens sidor så att lasten får stöd hela vägen ner.",
  },
  "STR-DEF-001": {
    title: "Hyllorna behöver mer stöd",
    summary: "Hyllorna böjer sig för mycket med den valda lasten och behöver mer stöd.",
  },
  "CB-DEFLECTION-001": {
    title: "Hyllorna behöver mer stöd",
    summary: "Hyllorna böjer sig för mycket med den valda lasten och behöver mer stöd.",
  },
  "CB-BENDING-001": {
    title: "Stärk hyllorna",
    summary: "Hyllornas beräknade belastning behöver minska, till exempel genom fler avdelare.",
  },
  "STAB-RACK-001": {
    title: "Stabilisera stommen",
    summary: "Stommen behöver bättre stöd mot att skeva, till exempel med ett bakstycke.",
  },
  "CB-STABILITY-001": {
    title: "Stabilisera stommen",
    summary: "Stommen behöver bättre stöd mot att skeva, till exempel med ett bakstycke.",
  },
  "STR-TOPO-001": {
    title: "Återställ saknade delar",
    summary: "Bärande delar saknas och behöver återställas så att konstruktionen hänger ihop.",
  },
  "PART-CUSTOM-001": {
    title: "Kontrollera ändrade delar",
    summary: "Ändringar i delar eller indelning behöver granskas, eller återställas till den tidigare konstruktionen.",
  },
};

const WORKSHOP_COPY: Readonly<Record<string, RuleCopy>> = {
  "DFM-STOCK-001": {
    title: "Kontrollera materialet",
    summary: "Alla delar ryms inte i det valda materialet.",
  },
  "DFM-MACHINE-001": {
    title: "Kontrollera maskinen",
    summary: "Verkstaden behöver kontrollera att en vald maskin kan bearbeta alla delar.",
  },
  "DFM-GRAIN-001": {
    title: "Kontrollera fiberriktningen",
    summary: "Materialets fiberriktning behöver bekräftas innan delarna kan placeras på skivorna.",
  },
  "STAB-TIP-002": {
    title: "Fäst möbeln i väggen",
    summary: "Väggtyp och infästning behöver kontrolleras så att möbeln inte kan välta.",
  },
  "CB-TIP-001": {
    title: "Fäst möbeln i väggen",
    summary: "Väggtyp och infästning behöver kontrolleras så att möbeln inte kan välta.",
  },
  "CB-JOINT-001": {
    title: "Kontrollera infästningen",
    summary: "Hyllornas stöd och förband behöver kontrolleras innan möbeln kan tillverkas.",
  },
  "CB-HARDWARE-001": {
    title: "Välj gångjärn",
    summary: "Gångjärn, montageplattor och borrhål behöver passa luckorna och kontrolleras före tillverkning.",
  },
};

/** Presentation only: never changes rule status, evidence or available fixes. */
export function validationPresentation(evaluation: RuleEvaluation): ValidationPresentation {
  const designCopy = Object.hasOwn(DESIGN_COPY, evaluation.rule_id)
    ? DESIGN_COPY[evaluation.rule_id]
    : undefined;
  const workshopCopy = Object.hasOwn(WORKSHOP_COPY, evaluation.rule_id)
    ? WORKSHOP_COPY[evaluation.rule_id]
    : undefined;
  const category = workshopCopy ? "workshop" : "design";
  const copy = designCopy ?? workshopCopy;

  // Preserve passing and unknown results verbatim, including non-applicable checks.
  // A PASS applies to this rule only; it is not an authorization to manufacture.
  if (evaluation.status === "PASS" || !copy) {
    return { title: evaluation.title, summary: evaluation.summary, category };
  }

  // The server warns already at 80% of the deflection limit, unlike the local
  // screening. Do not describe that warning as an exceeded numerical limit.
  if (evaluation.rule_id === "CB-DEFLECTION-001" && evaluation.status === "WARNING") {
    return {
      title: "Kontrollera hyllornas stöd",
      summary: "Hyllornas svikt behöver kontrolleras för den valda lasten; mer stöd ger större marginal.",
      category,
    };
  }

  if (evaluation.rule_id === "DFM-STOCK-001") {
    // Only the exact engine message establishes an oversized part. The number
    // of affected parts or utilization alone cannot distinguish format/capacity.
    if (/^\d+ delar ryms inte i valt skivformat\.$/.test(evaluation.summary)) {
      return {
        title: "Skivformatet är för litet",
        summary: "Minst en del är större än det valda skivformatet; verkstaden behöver kontrollera materialvalet.",
        category,
      };
    }
    if (/^\d+ delar kan inte placeras inom valda materialseparerade skivor\.$/.test(evaluation.summary)) {
      return {
        title: "Materialet räcker inte",
        summary: "Alla delar får inte plats på de valda skivorna; verkstaden behöver kontrollera format och antal.",
        category,
      };
    }
  }

  if (
    evaluation.rule_id === "CB-JOINT-001"
    && evaluation.diagnostics?.some(({ label, value }) => label === "hylltyp" && value === "adjustable")
  ) {
    return {
      title: "Kontrollera hyllbärarna",
      summary: "Hyllbärarna saknar verifierat underlag; fasta hyllor kan vara ett alternativ.",
      category,
    };
  }

  return { ...copy, category };
}

const FIELD_LABELS: Partial<Record<keyof DesignSpec, string>> = {
  furniture_type: "Möbeltyp",
  width_mm: "Bredd",
  height_mm: "Höjd",
  depth_mm: "Djup",
  material_id: "Material",
  material_name: "Materialnamn",
  back_material_id: "Bakstyckets material",
  nominal_thickness_mm: "Skivtjocklek",
  measured_thickness_mm: "Uppmätt skivtjocklek",
  measured_back_thickness_mm: "Bakstyckets uppmätta tjocklek",
  shelf_count: "Hyllplan",
  fixed_shelves: "Fasta hyllor",
  load_per_shelf_kg: "Last per hyllrad",
  back_panel: "Bakstycke",
  back_panel_type: "Bakstyckets montering",
  plinth: "Sockel",
  plinth_height_mm: "Sockelhöjd",
  divider_count: "Avdelare",
  bay_sizing_mode: "Fackindelning",
  target_bay_width_mm: "Önskad fackbredd",
  bay_width_ratios: "Fackens fördelning",
  shelf_height_ratios: "Hyllornas placering",
  symmetry_locked: "Symmetrisk indelning",
  part_overrides: "Individuella deländringar",
  removed_part_ids: "Borttagna delar",
  topology_baseline: "Sparad ursprungsindelning",
  base_cabinet_height_mm: "Underskåpens höjd",
  base_cabinet_depth_mm: "Underskåpens djup",
  base_cabinet_count: "Underskåp",
  reinforcement_mode: "Förstärkning",
  joint_system: "Förband",
  edge_band_mm: "Kantlist",
  wall_anchor_required: "Väggförankring krävs",
  wall_anchor_verified: "Väggförankring verifierad",
  stock_width_mm: "Stomskivans bredd",
  stock_height_mm: "Stomskivans höjd",
  stock_count: "Stomskivor",
  back_stock_width_mm: "Bakstycksskivans bredd",
  back_stock_height_mm: "Bakstycksskivans höjd",
  back_stock_count: "Bakstycksskivor",
  machine_profile_id: "Maskinprofil",
  workshop_context: "Verkstadsunderlag",
};

export function validationPreviewFieldLabel(field: keyof DesignSpec): string {
  return FIELD_LABELS[field] ?? field;
}

const VALUE_LABELS: Partial<Record<keyof DesignSpec, Readonly<Record<string, string>>>> = {
  reinforcement_mode: { auto: "Automatiskt", manual: "Manuellt" },
  bay_sizing_mode: { count: "Antal fack", target_width: "Önskad fackbredd" },
  furniture_type: { bookcase: "Bokhylla", wall_library: "Hyllvägg med underskåp" },
  back_panel_type: { inset_groove: "Infällt i spår", surface_mounted: "Utanpåliggande" },
  joint_system: { dado: "Spårförband" },
};

function formattedNumber(value: number): string {
  return value.toLocaleString("sv-SE", { maximumFractionDigits: 20 });
}

/** Human-readable preview; the original values remain available in exact details. */
export function validationPreviewValue(field: keyof DesignSpec, value: unknown): string {
  if (value === undefined || value === null) return "Inte angivet";
  if (typeof value === "boolean") return value ? "Ja" : "Nej";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return String(value);
    const formatted = formattedNumber(value);
    if (field.endsWith("_mm")) return `${formatted} mm`;
    if (field === "load_per_shelf_kg") return `${formatted} kg`;
    if (field.endsWith("_count")) return `${formatted} st`;
    return formatted;
  }
  if (typeof value === "string") {
    const labels = VALUE_LABELS[field];
    return labels && Object.hasOwn(labels, value) ? labels[value]! : value;
  }
  if (Array.isArray(value)) {
    if (field === "bay_width_ratios" || field === "shelf_height_ratios") {
      return value.length === 0
        ? "Jämn fördelning"
        : value.map((item) => typeof item === "number" ? formattedNumber(item) : String(item)).join(" / ");
    }
    if (field === "removed_part_ids") {
      return value.length === 0
        ? "Inga borttagna delar"
        : `${value.length} ${value.length === 1 ? "borttagen del" : "borttagna delar"}`;
    }
  }
  if (typeof value === "object") {
    if (field === "part_overrides") {
      const count = Object.keys(value).length;
      return count === 0
        ? "Inga individuella ändringar"
        : `${count} ${count === 1 ? "individuell deländring" : "individuella deländringar"}`;
    }
    if (field === "topology_baseline") return "Sparad indelning";
  }
  return JSON.stringify(value) ?? String(value);
}
