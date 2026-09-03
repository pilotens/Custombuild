"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  BACK_MATERIALS,
  MACHINES,
  MATERIALS,
  type DesignSpec,
} from "@/lib/design-types";
import {
  WorkshopProductionContextError,
  exactMillimetreTextToMicrometres,
  micrometresToMillimetreText,
  parseWorkshopProductionContext,
  type RevisionProductionContextSnapshot,
  type WorkshopGrainDirection,
  type WorkshopProductionContext,
  type WorkshopStockRole,
  type WorkshopStockZone,
} from "@/lib/workshop-production-context";
import styles from "./workshop-context-editor.module.css";

export interface WorkshopStockZoneDraft {
  xMm: string;
  yMm: string;
  widthMm: string;
  heightMm: string;
}

export interface WorkshopStockProfileDraft {
  role: WorkshopStockRole;
  supplierProfileId: string;
  supplierProfileVersion: string;
  sheetWidthMm: string;
  sheetHeightMm: string;
  sheetCount: string;
  trimMarginMm: string;
  kerfMm: string;
  grainDirection: WorkshopGrainDirection | "";
  allowRotation: "true" | "false" | "";
  defectZones: WorkshopStockZoneDraft[];
  keepOutZones: WorkshopStockZoneDraft[];
}

export interface WorkshopRegistrationDraft {
  stockRole: WorkshopStockRole | "";
  sheetIndex: string;
  fixtureMethodId: string;
  fixtureMethodVersion: string;
  pinDiameterMm: string;
  positionToleranceMm: string;
  pins: Array<{ xMm: string; yMm: string }>;
}

export interface WorkshopContextDraft {
  profiles: WorkshopStockProfileDraft[];
  registrations: WorkshopRegistrationDraft[];
}

export interface WorkshopContextDraftState {
  enabled: boolean;
  draft: WorkshopContextDraft;
  dirty: boolean;
  valid: boolean;
  validationMessage: string | undefined;
  sourceValueSignature: string;
  sourceBindingSignature: string;
  pendingValueSignature: string | undefined;
}

export interface WorkshopContextEditorProps {
  spec: DesignSpec;
  value?: WorkshopProductionContext;
  frozenContext?: RevisionProductionContextSnapshot;
  disabled?: boolean;
  onChange: (
    context: WorkshopProductionContext | undefined,
    legacyPatch: Partial<Pick<
      DesignSpec,
      | "stock_width_mm"
      | "stock_height_mm"
      | "stock_count"
      | "back_stock_width_mm"
      | "back_stock_height_mm"
      | "back_stock_count"
      | "machine_profile_id"
    >>,
  ) => void;
  onValidityChange?: (valid: boolean) => void;
  draftState?: WorkshopContextDraftState;
  onDraftStateChange?: (state: WorkshopContextDraftState) => void;
}

function emptyZone(): WorkshopStockZoneDraft {
  return { xMm: "", yMm: "", widthMm: "", heightMm: "" };
}

function derivedMaterial(spec: DesignSpec, role: WorkshopStockRole) {
  return role === "carcass"
    ? MATERIALS.find((material) => material.id === spec.material_id)
    : BACK_MATERIALS.find((material) => material.id === spec.back_material_id);
}

function emptyProfile(spec: DesignSpec, role: WorkshopStockRole): WorkshopStockProfileDraft {
  const back = role === "back";
  return {
    role,
    supplierProfileId: "",
    supplierProfileVersion: "",
    sheetWidthMm: String(back ? spec.back_stock_width_mm : spec.stock_width_mm),
    sheetHeightMm: String(back ? spec.back_stock_height_mm : spec.stock_height_mm),
    sheetCount: String(back ? spec.back_stock_count : spec.stock_count),
    trimMarginMm: "",
    kerfMm: "",
    grainDirection: "",
    allowRotation: "",
    defectZones: [],
    keepOutZones: [],
  };
}

function draftFromContext(spec: DesignSpec, context?: WorkshopProductionContext): WorkshopContextDraft {
  if (!context) {
    return {
      profiles: [
        emptyProfile(spec, "carcass"),
        ...(spec.back_panel ? [emptyProfile(spec, "back")] : []),
      ],
      registrations: [],
    };
  }
  return {
    profiles: context.stock_profiles.map((profile) => ({
      role: profile.role,
      supplierProfileId: profile.supplier_profile_id,
      supplierProfileVersion: profile.supplier_profile_version,
      sheetWidthMm: micrometresToMillimetreText(profile.sheet_width_um),
      sheetHeightMm: micrometresToMillimetreText(profile.sheet_height_um),
      sheetCount: String(profile.sheet_count),
      trimMarginMm: micrometresToMillimetreText(profile.trim_margin_um),
      kerfMm: micrometresToMillimetreText(profile.kerf_um),
      grainDirection: profile.grain_direction,
      allowRotation: String(profile.allow_rotation) as "true" | "false",
      defectZones: profile.defect_zones.map((zone) => ({
        xMm: micrometresToMillimetreText(zone.x_um),
        yMm: micrometresToMillimetreText(zone.y_um),
        widthMm: micrometresToMillimetreText(zone.width_um),
        heightMm: micrometresToMillimetreText(zone.height_um),
      })),
      keepOutZones: profile.fixture_keep_out_zones.map((zone) => ({
        xMm: micrometresToMillimetreText(zone.x_um),
        yMm: micrometresToMillimetreText(zone.y_um),
        widthMm: micrometresToMillimetreText(zone.width_um),
        heightMm: micrometresToMillimetreText(zone.height_um),
      })),
    })),
    registrations: (context.two_sided_registrations ?? []).map((registration) => ({
      stockRole: registration.stock_role,
      sheetIndex: String(registration.sheet_index + 1),
      fixtureMethodId: registration.fixture_method_id,
      fixtureMethodVersion: registration.fixture_method_version,
      pinDiameterMm: micrometresToMillimetreText(registration.pin_diameter_um),
      positionToleranceMm: micrometresToMillimetreText(registration.position_tolerance_um),
      pins: registration.pins.map((pin) => ({
        xMm: micrometresToMillimetreText(pin.x_um),
        yMm: micrometresToMillimetreText(pin.y_um),
      })),
    })),
  };
}

function parseCount(value: string, label: string, maximum = 100): number {
  if (!/^\d+$/.test(value.trim())) {
    throw new WorkshopProductionContextError([`${label} måste vara ett heltal.`]);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new WorkshopProductionContextError([`${label} måste vara 1–${maximum}.`]);
  }
  return parsed;
}

function parseZoneDraft(zone: WorkshopStockZoneDraft): WorkshopStockZone {
  return {
    x_um: exactMillimetreTextToMicrometres(zone.xMm, { maximumUm: 10_000_000 }),
    y_um: exactMillimetreTextToMicrometres(zone.yMm, { maximumUm: 5_000_000 }),
    width_um: exactMillimetreTextToMicrometres(zone.widthMm, { minimumUm: 1, maximumUm: 10_000_000 }),
    height_um: exactMillimetreTextToMicrometres(zone.heightMm, { minimumUm: 1, maximumUm: 5_000_000 }),
  };
}

function parseEditorDraft(
  draft: WorkshopContextDraft,
  spec: DesignSpec,
): {
  context: WorkshopProductionContext;
  patch: Parameters<WorkshopContextEditorProps["onChange"]>[1];
} {
  const profileDimensions = new Map<WorkshopStockRole, {
    widthMm: number;
    heightMm: number;
    count: number;
  }>();
  const stockProfiles = draft.profiles.map((profile) => {
    const material = derivedMaterial(spec, profile.role);
    if (!material) throw new WorkshopProductionContextError(["Designens material saknar en versionslåst katalogpost."]);
    const widthUm = exactMillimetreTextToMicrometres(profile.sheetWidthMm, { minimumUm: 1, maximumUm: 10_000_000 });
    const heightUm = exactMillimetreTextToMicrometres(profile.sheetHeightMm, { minimumUm: 1, maximumUm: 5_000_000 });
    const count = parseCount(profile.sheetCount, "Antal råskivor");
    if (!profile.grainDirection || !profile.allowRotation) {
      throw new WorkshopProductionContextError(["Ange fiberriktning och om 90° rotation är tillåten."]);
    }
    profileDimensions.set(profile.role, { widthMm: widthUm / 1_000, heightMm: heightUm / 1_000, count });
    return {
      role: profile.role,
      declaration_authority: "CLIENT_DECLARED" as const,
      supplier_profile_id: profile.supplierProfileId,
      supplier_profile_version: profile.supplierProfileVersion,
      material_id: material.id,
      material_version: material.version,
      sheet_width_um: widthUm,
      sheet_height_um: heightUm,
      thickness_um: exactMillimetreTextToMicrometres(
        String(
          profile.role === "carcass"
            ? spec.measured_thickness_mm
            : spec.measured_back_thickness_mm,
        ),
        { minimumUm: 1, maximumUm: 100_000 },
      ),
      sheet_count: count,
      trim_margin_um: exactMillimetreTextToMicrometres(profile.trimMarginMm, { maximumUm: 500_000 }),
      kerf_um: exactMillimetreTextToMicrometres(profile.kerfMm, { maximumUm: 100_000 }),
      grain_direction: profile.grainDirection,
      allow_rotation: profile.allowRotation === "true",
      defect_zones: profile.defectZones.map(parseZoneDraft),
      fixture_keep_out_zones: profile.keepOutZones.map(parseZoneDraft),
    };
  });
  const carcass = profileDimensions.get("carcass");
  if (!carcass) throw new WorkshopProductionContextError(["En stomprofil är obligatorisk."]);
  const back = profileDimensions.get("back");
  const patch = {
    stock_width_mm: carcass.widthMm,
    stock_height_mm: carcass.heightMm,
    stock_count: carcass.count,
    ...(back ? {
      back_stock_width_mm: back.widthMm,
      back_stock_height_mm: back.heightMm,
      back_stock_count: back.count,
    } : {}),
  };
  const registrations = draft.registrations.map((registration) => {
    if (!registration.stockRole) {
      throw new WorkshopProductionContextError(["Välj råmaterialroll för varje tvåsidig registrering."]);
    }
    return {
      stock_role: registration.stockRole,
      sheet_index: parseCount(registration.sheetIndex, "Skivnummer") - 1,
      declaration_authority: "CLIENT_DECLARED" as const,
      flip_axis: "X" as const,
      fixture_method_id: registration.fixtureMethodId,
      fixture_method_version: registration.fixtureMethodVersion,
      pin_diameter_um: exactMillimetreTextToMicrometres(registration.pinDiameterMm, {
        minimumUm: 1_000,
        maximumUm: 50_000,
      }),
      position_tolerance_um: exactMillimetreTextToMicrometres(registration.positionToleranceMm, {
        minimumUm: 1,
        maximumUm: 10_000,
      }),
      pins: registration.pins.map((pin) => ({
        x_um: exactMillimetreTextToMicrometres(pin.xMm, { maximumUm: 10_000_000 }),
        y_um: exactMillimetreTextToMicrometres(pin.yMm, { maximumUm: 5_000_000 }),
      })),
    };
  });
  const context = parseWorkshopProductionContext({
    stock_profiles: stockProfiles,
    ...(registrations.length > 0 ? { two_sided_registrations: registrations } : {}),
  }, { ...spec, ...patch });
  if (!context) throw new WorkshopProductionContextError(["Verkstadskontexten saknas."]);
  return { context, patch };
}

function profileRoleLabel(role: WorkshopStockRole): string {
  return role === "carcass" ? "Stomskivor" : "Bakstyckesskivor";
}

function updateAt<T>(values: readonly T[], index: number, update: (value: T) => T): T[] {
  return values.map((value, current) => current === index ? update(value) : value);
}

function workshopContextValueSignature(value?: WorkshopProductionContext): string {
  return JSON.stringify(value) ?? "undefined";
}

function workshopContextBindingSignature(spec: DesignSpec): string {
  return JSON.stringify([
    spec.material_id,
    spec.measured_thickness_mm,
    spec.measured_back_thickness_mm,
    spec.back_panel,
    spec.back_material_id,
    spec.stock_width_mm,
    spec.stock_height_mm,
    spec.stock_count,
    spec.back_stock_width_mm,
    spec.back_stock_height_mm,
    spec.back_stock_count,
  ]);
}

export function createWorkshopContextDraftState(
  spec: DesignSpec,
  value?: WorkshopProductionContext,
): WorkshopContextDraftState {
  const draft = draftFromContext(spec, value);
  const sourceValueSignature = workshopContextValueSignature(value);
  const sourceBindingSignature = workshopContextBindingSignature(spec);
  if (!value) {
    return {
      enabled: false,
      draft,
      dirty: false,
      valid: true,
      validationMessage: undefined,
      sourceValueSignature,
      sourceBindingSignature,
      pendingValueSignature: undefined,
    };
  }
  try {
    parseEditorDraft(draft, spec);
    return {
      enabled: true,
      draft,
      dirty: false,
      valid: true,
      validationMessage: undefined,
      sourceValueSignature,
      sourceBindingSignature,
      pendingValueSignature: undefined,
    };
  } catch (caught) {
    const detail = caught instanceof Error ? caught.message : "Verkstadsprofilen är ogiltig.";
    return {
      enabled: true,
      draft,
      dirty: false,
      valid: false,
      validationMessage: `Designens materialbindning har ändrats. Kontrollera verkstadsprofilen innan nästa revision: ${detail}`,
      sourceValueSignature,
      sourceBindingSignature,
      pendingValueSignature: undefined,
    };
  }
}

export function WorkshopContextEditor({
  spec,
  value,
  frozenContext,
  disabled = false,
  onChange,
  onValidityChange,
  draftState,
  onDraftStateChange,
}: WorkshopContextEditorProps) {
  const machineChoiceName = useId();
  const [internalDraftState, setInternalDraftState] = useState<WorkshopContextDraftState>(
    () => createWorkshopContextDraftState(spec, value),
  );
  const editorState = draftState ?? internalDraftState;
  const { enabled, draft, validationMessage } = editorState;
  const frozenMachine = frozenContext
    ? MACHINES.find((machine) => machine.id === frozenContext.machine_profile_id)
    : undefined;
  const valueSignature = useMemo(() => workshopContextValueSignature(value), [value]);
  const bindingSignature = workshopContextBindingSignature(spec);
  const latestSyncRef = useRef({ spec, value, onChange });

  useEffect(() => {
    latestSyncRef.current = { spec, value, onChange };
  }, [onChange, spec, value]);

  const publishDraftState = useCallback((next: WorkshopContextDraftState) => {
    setInternalDraftState(next);
    onValidityChange?.(next.valid);
    onDraftStateChange?.(next);
  }, [onDraftStateChange, onValidityChange]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      if (editorState.dirty) {
        if (editorState.pendingValueSignature === valueSignature) {
          publishDraftState({
            ...editorState,
            dirty: false,
            sourceValueSignature: valueSignature,
            sourceBindingSignature: bindingSignature,
            pendingValueSignature: undefined,
          });
          return;
        }
        if (editorState.sourceBindingSignature !== bindingSignature && editorState.enabled) {
          try {
            const parsed = parseEditorDraft(editorState.draft, latestSyncRef.current.spec);
            const pendingValueSignature = workshopContextValueSignature(parsed.context);
            publishDraftState({
              ...editorState,
              valid: true,
              validationMessage: undefined,
              sourceBindingSignature: bindingSignature,
              pendingValueSignature,
            });
            latestSyncRef.current.onChange(parsed.context, parsed.patch);
          } catch (caught) {
            const message = caught instanceof Error ? caught.message : "Verkstadskontexten är ogiltig.";
            publishDraftState({
              ...editorState,
              valid: false,
              validationMessage: message,
              sourceBindingSignature: bindingSignature,
              pendingValueSignature: undefined,
            });
          }
        }
        return;
      }
      if (
        editorState.sourceValueSignature === valueSignature
        && editorState.sourceBindingSignature === bindingSignature
      ) {
        return;
      }
      const synchronized = createWorkshopContextDraftState(
        latestSyncRef.current.spec,
        latestSyncRef.current.value,
      );
      publishDraftState(synchronized);
      if (latestSyncRef.current.value && synchronized.valid) {
        const parsed = parseEditorDraft(synchronized.draft, latestSyncRef.current.spec);
        const synchronizedValueSignature = workshopContextValueSignature(parsed.context);
        if (synchronizedValueSignature !== valueSignature) {
          publishDraftState({
            ...synchronized,
            dirty: true,
            pendingValueSignature: synchronizedValueSignature,
          });
          latestSyncRef.current.onChange(parsed.context, parsed.patch);
        }
      }
    });
    return () => { cancelled = true; };
  }, [bindingSignature, editorState, publishDraftState, valueSignature]);

  function applyDraft(next: WorkshopContextDraft) {
    try {
      const parsed = parseEditorDraft(next, spec);
      publishDraftState({
        enabled: true,
        draft: next,
        dirty: true,
        valid: true,
        validationMessage: undefined,
        sourceValueSignature: valueSignature,
        sourceBindingSignature: bindingSignature,
        pendingValueSignature: workshopContextValueSignature(parsed.context),
      });
      onChange(parsed.context, parsed.patch);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Verkstadskontexten är ogiltig.";
      publishDraftState({
        enabled: true,
        draft: next,
        dirty: true,
        valid: false,
        validationMessage: message,
        sourceValueSignature: valueSignature,
        sourceBindingSignature: bindingSignature,
        pendingValueSignature: undefined,
      });
    }
  }

  function startBinding() {
    const next = draftFromContext(spec);
    publishDraftState({
      enabled: true,
      draft: next,
      dirty: true,
      valid: false,
      validationMessage: "Fyll i samtliga leverantörs- och verkstadsuppgifter.",
      sourceValueSignature: valueSignature,
      sourceBindingSignature: bindingSignature,
      pendingValueSignature: undefined,
    });
  }

  function returnToStockless() {
    publishDraftState({
      enabled: false,
      draft: draftFromContext(spec),
      dirty: true,
      valid: true,
      validationMessage: undefined,
      sourceValueSignature: valueSignature,
      sourceBindingSignature: bindingSignature,
      pendingValueSignature: workshopContextValueSignature(undefined),
    });
    onChange(undefined, {});
  }

  function selectMachine(machineProfileId: string) {
    const machine = MACHINES.find((candidate) => candidate.id === machineProfileId);
    if (!machine || machine.id === spec.machine_profile_id) return;
    onChange(value, { machine_profile_id: machine.id });
  }

  function updateProfile(index: number, patch: Partial<WorkshopStockProfileDraft>) {
    applyDraft({
      ...draft,
      profiles: updateAt(draft.profiles, index, (profile) => ({ ...profile, ...patch })),
    });
  }

  function updateZone(
    profileIndex: number,
    kind: "defectZones" | "keepOutZones",
    zoneIndex: number,
    patch: Partial<WorkshopStockZoneDraft>,
  ) {
    updateProfile(profileIndex, {
      [kind]: updateAt(draft.profiles[profileIndex]![kind], zoneIndex, (zone) => ({ ...zone, ...patch })),
    });
  }

  function renderZones(profile: WorkshopStockProfileDraft, profileIndex: number, kind: "defectZones" | "keepOutZones") {
    const label = kind === "defectZones" ? "Defektzoner" : "Fixturens keep-out-zoner";
    return (
      <fieldset className={styles.zones}>
        <legend>{label}</legend>
        {profile[kind].map((zone, zoneIndex) => (
          <div className={styles.zoneGrid} key={`${kind}-${zoneIndex}`}>
            {(["xMm", "yMm", "widthMm", "heightMm"] as const).map((field) => (
              <label key={field}>
                <span>{{ xMm: "X", yMm: "Y", widthMm: "Bredd", heightMm: "Höjd" }[field]} (mm)</span>
                <input
                  inputMode="decimal"
                  value={zone[field]}
                  disabled={disabled}
                  onChange={(event) => updateZone(profileIndex, kind, zoneIndex, { [field]: event.target.value })}
                />
              </label>
            ))}
            <button
              type="button"
              disabled={disabled}
              onClick={() => updateProfile(profileIndex, {
                [kind]: profile[kind].filter((_, current) => current !== zoneIndex),
              })}
            >Ta bort zon</button>
          </div>
        ))}
        <button
          type="button"
          disabled={disabled || profile[kind].length >= 100}
          onClick={() => updateProfile(profileIndex, { [kind]: [...profile[kind], emptyZone()] })}
        >Lägg till {kind === "defectZones" ? "defektzon" : "keep-out-zon"}</button>
      </fieldset>
    );
  }

  return (
    <section className={styles.root} aria-label="Råmaterial och tvåsidig registrering">
      <header>
        <h3>Verkstadsprofil</h3>
        <p className={styles.notice}>
          Standardläget är lagerobundet. Bind bara uppgifter som materialleverantören eller
          CNC-verkstaden faktiskt har lämnat; Custombuild gissar aldrig batch, fiberriktning,
          kerf, fixtur eller registreringspinnar.
        </p>
      </header>

      <fieldset className={styles.profile}>
        <legend>Maskinprofil för validering</legend>
        <p className={styles.notice}>
          Välj den versionslåsta katalogprofil som motsvarar arbetsytans storlek. Profilerna
          används endast för validering och skapar inte körklar eller frisläppt CNC-kod.
        </p>
        <div className={styles.grid}>
          {MACHINES.map((machine) => {
            const descriptionId = `${machineChoiceName}-${machine.id}`;
            return (
              <label key={machine.id}>
                <span>
                  <strong>{machine.name}</strong>
                  <small id={descriptionId}>
                    Versionslåst profil-ID: {machine.id} · katalogversion {machine.version} ·
                    arbetsområde X {machine.workAreaMm.x} ×
                    Y {machine.workAreaMm.y} × Z {machine.workAreaMm.z} mm
                  </small>
                </span>
                <input
                  type="radio"
                  name={machineChoiceName}
                  value={machine.id}
                  checked={spec.machine_profile_id === machine.id}
                  disabled={disabled}
                  aria-describedby={descriptionId}
                  onChange={() => selectMachine(machine.id)}
                />
              </label>
            );
          })}
        </div>
      </fieldset>

      {!enabled ? (
        <div className={styles.actions}>
          <strong>Lagerobundet granskningspaket</strong>
          <button type="button" disabled={disabled} onClick={startBinding}>
            Bind leverantörsdeklarerad verkstadsprofil
          </button>
        </div>
      ) : (
        <>
          <div className={styles.profiles}>
            {draft.profiles.map((profile, profileIndex) => {
              const material = derivedMaterial(spec, profile.role);
              const thickness = profile.role === "carcass"
                ? spec.measured_thickness_mm
                : spec.measured_back_thickness_mm;
              return (
                <fieldset className={styles.profile} key={profile.role}>
                  <legend>{profileRoleLabel(profile.role)}</legend>
                  <p className={styles.derived}>
                    Designbunden identitet: <strong>{material?.id} @ {material?.version}</strong>
                    {" · "}{thickness} mm. Dessa värden kommer från den frysta designen och kan inte skrivas över här.
                  </p>
                  <div className={styles.grid}>
                    <label>
                      <span>Leverantörens profil-ID (deklarerat)</span>
                      <input value={profile.supplierProfileId} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { supplierProfileId: event.target.value })} />
                    </label>
                    <label>
                      <span>Profilversion eller batch</span>
                      <input value={profile.supplierProfileVersion} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { supplierProfileVersion: event.target.value })} />
                    </label>
                    <label>
                      <span>Skivbredd (mm)</span>
                      <input inputMode="decimal" value={profile.sheetWidthMm} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { sheetWidthMm: event.target.value })} />
                    </label>
                    <label>
                      <span>Skivhöjd (mm)</span>
                      <input inputMode="decimal" value={profile.sheetHeightMm} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { sheetHeightMm: event.target.value })} />
                    </label>
                    <label>
                      <span>Antal fysiska skivor</span>
                      <input inputMode="numeric" value={profile.sheetCount} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { sheetCount: event.target.value })} />
                    </label>
                    <label>
                      <span>Trimkant (mm)</span>
                      <input inputMode="decimal" value={profile.trimMarginMm} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { trimMarginMm: event.target.value })} />
                    </label>
                    <label>
                      <span>Kerf/verktygsspalt (mm)</span>
                      <input inputMode="decimal" value={profile.kerfMm} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { kerfMm: event.target.value })} />
                    </label>
                    <label>
                      <span>Fiberriktning i råskivan</span>
                      <select value={profile.grainDirection} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { grainDirection: event.target.value as WorkshopStockProfileDraft["grainDirection"] })}>
                        <option value="">Välj från leverantörsdata</option>
                        <option value="X">X-axeln</option>
                        <option value="Y">Y-axeln</option>
                        <option value="NONE">Icke-riktat material</option>
                      </select>
                    </label>
                    <label>
                      <span>Tillåt 90° rotation vid nesting</span>
                      <select value={profile.allowRotation} disabled={disabled} onChange={(event) => updateProfile(profileIndex, { allowRotation: event.target.value as WorkshopStockProfileDraft["allowRotation"] })}>
                        <option value="">Välj uttryckligen</option>
                        <option value="false">Nej</option>
                        <option value="true">Ja</option>
                      </select>
                    </label>
                  </div>
                  {renderZones(profile, profileIndex, "defectZones")}
                  {renderZones(profile, profileIndex, "keepOutZones")}
                </fieldset>
              );
            })}
          </div>

          <fieldset className={styles.registrations}>
            <legend>Tvåsidig registrering per fysisk skiva</legend>
            <p className={styles.notice}>X-flippen är versionslåst. Ange bara verkstadens faktiska metod och stock-frame-koordinater.</p>
            {draft.registrations.map((registration, registrationIndex) => (
              <div className={styles.registration} key={`registration-${registrationIndex}`}>
                <div className={styles.grid}>
                  <label>
                    <span>Råmaterialroll</span>
                    <select value={registration.stockRole} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, stockRole: event.target.value as WorkshopRegistrationDraft["stockRole"] })),
                    })}>
                      <option value="">Välj skivtyp</option>
                      <option value="carcass">Stomme</option>
                      {spec.back_panel ? <option value="back">Bakstycke</option> : null}
                    </select>
                  </label>
                  <label>
                    <span>Fysiskt skivnummer</span>
                    <input inputMode="numeric" value={registration.sheetIndex} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, sheetIndex: event.target.value })),
                    })} />
                  </label>
                  <label>
                    <span>Fixtur-/registreringsmetod-ID</span>
                    <input value={registration.fixtureMethodId} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, fixtureMethodId: event.target.value })),
                    })} />
                  </label>
                  <label>
                    <span>Fixturmetodens version</span>
                    <input value={registration.fixtureMethodVersion} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, fixtureMethodVersion: event.target.value })),
                    })} />
                  </label>
                  <label>
                    <span>Registreringspinnens diameter (mm)</span>
                    <input inputMode="decimal" value={registration.pinDiameterMm} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, pinDiameterMm: event.target.value })),
                    })} />
                  </label>
                  <label>
                    <span>Positionstolerans (mm)</span>
                    <input inputMode="decimal" value={registration.positionToleranceMm} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, positionToleranceMm: event.target.value })),
                    })} />
                  </label>
                  <div className={styles.derived}>Flip-axel: <strong>X</strong> (fast kontrakt)</div>
                </div>
                {registration.pins.map((pin, pinIndex) => (
                  <div className={styles.pinGrid} key={`pin-${pinIndex}`}>
                    <label><span>Pinne {pinIndex + 1}, X (mm)</span><input inputMode="decimal" value={pin.xMm} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({
                        ...row,
                        pins: updateAt(row.pins, pinIndex, (current) => ({ ...current, xMm: event.target.value })),
                      })),
                    })} /></label>
                    <label><span>Pinne {pinIndex + 1}, Y (mm)</span><input inputMode="decimal" value={pin.yMm} disabled={disabled} onChange={(event) => applyDraft({
                      ...draft,
                      registrations: updateAt(draft.registrations, registrationIndex, (row) => ({
                        ...row,
                        pins: updateAt(row.pins, pinIndex, (current) => ({ ...current, yMm: event.target.value })),
                      })),
                    })} /></label>
                  </div>
                ))}
                <div className={styles.rowActions}>
                  <button type="button" disabled={disabled || registration.pins.length >= 16} onClick={() => applyDraft({
                    ...draft,
                    registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, pins: [...row.pins, { xMm: "", yMm: "" }] })),
                  })}>Lägg till pinne</button>
                  <button type="button" disabled={disabled || registration.pins.length <= 2} onClick={() => applyDraft({
                    ...draft,
                    registrations: updateAt(draft.registrations, registrationIndex, (row) => ({ ...row, pins: row.pins.slice(0, -1) })),
                  })}>Ta bort sista pinnen</button>
                  <button type="button" disabled={disabled} onClick={() => applyDraft({
                    ...draft,
                    registrations: draft.registrations.filter((_, current) => current !== registrationIndex),
                  })}>Ta bort registrering</button>
                </div>
              </div>
            ))}
            <button type="button" disabled={disabled || draft.registrations.length >= 200} onClick={() => applyDraft({
              ...draft,
              registrations: [...draft.registrations, {
                stockRole: "",
                sheetIndex: "",
                fixtureMethodId: "",
                fixtureMethodVersion: "",
                pinDiameterMm: "",
                positionToleranceMm: "",
                pins: [{ xMm: "", yMm: "" }, { xMm: "", yMm: "" }],
              }],
            })}>Lägg till tvåsidig skiva</button>
          </fieldset>

          {validationMessage
            ? <p className={styles.error} role="alert">{validationMessage}</p>
            : <p className={styles.valid} role="status">Verkstadsprofilen är komplett och exakt bunden till aktuella designval.</p>}
          <div className={styles.actions}>
            <button type="button" disabled={disabled} onClick={returnToStockless}>Återgå till lagerobundet paket</button>
          </div>
        </>
      )}

      {frozenContext ? (
        <section className={styles.summary} aria-label="Fryst verkstadskontext">
          <header>
            <h4>Fryst i serverrevisionen</h4>
            <p className={styles.notice}>
              Kund-/verkstadsdeklarerad (inte verifierad). Detta är den oföränderliga kontext
              som nästa generering måste matcha exakt; paketet är fortfarande validation-only.
            </p>
          </header>
          <dl>
            <div>
              <dt>Maskinprofil</dt>
              <dd>
                {frozenMachine?.name ?? frozenContext.machine_profile_id} · profil-ID {frozenContext.machine_profile_id}
                {frozenMachine
                  ? ` · katalogversion ${frozenMachine.version} · X ${frozenMachine.workAreaMm.x} × Y ${frozenMachine.workAreaMm.y} × Z ${frozenMachine.workAreaMm.z} mm`
                  : ""}
              </dd>
            </div>
            {frozenContext.stock_profiles?.map((profile) => (
              <div key={profile.role}>
                <dt>{profileRoleLabel(profile.role)}</dt>
                <dd>{profile.supplier_profile_id} @ {profile.supplier_profile_version} · {micrometresToMillimetreText(profile.sheet_width_um)} × {micrometresToMillimetreText(profile.sheet_height_um)} × {profile.sheet_count} · fiber {profile.grain_direction}</dd>
              </div>
            ))}
            <dt>Tvåsidiga skivor</dt>
            <dd>{frozenContext.two_sided_registrations?.length ?? 0} deklarerade registreringar</dd>
          </dl>
        </section>
      ) : null}
    </section>
  );
}
