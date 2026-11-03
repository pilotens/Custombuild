import { ReferencePostEditor } from "./reference-posts";
import { useState } from "react";
import {
  EMPTY_REFERENCE_CONFIRMATIONS,
  referenceConfirmationsComplete,
  referenceConstructionIssues,
  referenceImageVerificationIsCurrent,
  referenceVerificationFingerprint,
} from "@/lib/reference-image";
import type {
  DesignSpec,
  ReferenceImageImport,
  ReferenceImageConfirmedInputs,
  ReferenceConstruction,
} from "@/lib/design-types";
import { differences, type ReferenceImageDraft } from "@/lib/reference-image";

export function ReferenceConstructionFields({
  value,
  change,
}: {
  value: ReferenceConstruction;
  change: (value: ReferenceConstruction) => void;
}) {
  return (
    <fieldset>
      <legend>Vad ska konstruktionen innehålla?</legend>
      <label>
        Stöd vid sidorna och mellan facken{" "}
        <select
          value={value.uprights}
          onChange={(e) =>
            change({
              ...value,
              uprights: e.target.value as ReferenceConstruction["uprights"],
            })
          }
        >
          <option value="unknown">Ej fastställt</option>
          <option value="panels">Skivsidor och avdelare</option>
          <option value="separate_posts">
            Separata stolpar eller uppstickande stöd
          </option>
        </select>
      </label>
      <label>
        Ryggens avsedda täckning{" "}
        <select
          value={value.back}
          onChange={(e) =>
            change({
              ...value,
              back: e.target.value as ReferenceConstruction["back"],
            })
          }
        >
          <option value="unknown">Ej fastställd</option>
          <option value="all_openings">Alla fack inklusive underskåp</option>
          <option value="upper_only">Endast överdelen</option>
          <option value="open">Öppen baksida</option>
        </select>
      </label>
      <label>
        Hyllplan per rad{" "}
        <select
          value={value.shelves}
          onChange={(e) =>
            change({
              ...value,
              shelves: e.target.value as ReferenceConstruction["shelves"],
            })
          }
        >
          <option value="unknown">Ej fastställt</option>
          <option value="per_bay">En del per fack</option>
          <option value="continuous">En genomgående planka</option>
        </select>
      </label>
      <label>
        Önskat stommaterial{" "}
        <select
          value={value.material}
          onChange={(e) =>
            change({
              ...value,
              material: e.target.value as ReferenceConstruction["material"],
            })
          }
        >
          <option value="unknown">Ej fastställt</option>
          <option value="solid-walnut">Massiv valnöt</option>
          <option value="birch-plywood">Björkplywood</option>
          <option value="mdf">MDF</option>
        </select>
      </label>
      <label>
        Detalj som saknas i modellen{" "}
        <input
          maxLength={300}
          value={value.unresolved_detail}
          placeholder="Till exempel ett utstickande ändstöd"
          onChange={(e) =>
            change({ ...value, unresolved_detail: e.target.value })
          }
        />
      </label>
      {value.uprights === "separate_posts" || value.posts?.length ? (
        <ReferencePostEditor
          posts={value.posts ?? []}
          change={(posts) => {
            const next = { ...value };
            if (posts.length) next.posts = posts;
            else delete next.posts;
            change(next);
          }}
        />
      ) : null}
    </fieldset>
  );
}

export function draftGuidePositions(
  draft: ReferenceImageDraft,
  horizontal: boolean,
): number[] {
  const count = horizontal ? draft.shelfCount : draft.dividerCount;
  if (horizontal && draft.shelfHeightRatios.length === count)
    return draft.shelfHeightRatios;
  if (!horizontal && draft.bayWidthRatios.length === count + 1) {
    const total = draft.bayWidthRatios.reduce((a, b) => a + b, 0);
    let pos = 0;
    return draft.bayWidthRatios.slice(0, -1).map((v) => (pos += v / total));
  }
  return Array.from({ length: count }, (_, i) => (i + 1) / (count + 1));
}

export function ReferenceGuideEditor({
  draft,
  change,
  horizontal,
}: {
  draft: ReferenceImageDraft;
  change: (patch: Partial<ReferenceImageDraft>) => void;
  horizontal: boolean;
}) {
  const positions = draftGuidePositions(draft, horizontal);
  const noun = horizontal ? "hyllinje" : "stödlinje";
  function commit(values: number[]) {
    const sorted = [...values].sort((a, b) => a - b);
    change(
      horizontal
        ? { shelfCount: sorted.length, shelfHeightRatios: sorted }
        : { dividerCount: sorted.length, bayWidthRatios: differences(sorted) },
    );
  }
  return (
    <details>
      <summary>
        Granska {horizontal ? "hyllinjer" : "stödlinjer"} ({positions.length})
      </summary>
      <p>
        {horizontal
          ? "Procent av överdelens höjd, räknat nedifrån."
          : "Procent av stommens bredd från vänster. Ytterkanterna ingår inte."}
      </p>
      {positions.map((pos, i) => (
        <div key={`${i}-${pos}`}>
          <label>
            {noun} {i + 1} (%){" "}
            <input
              type="number"
              min="0.1"
              max="99.9"
              step="0.1"
              defaultValue={Number((pos * 100).toFixed(2))}
              onBlur={(e) => {
                const n = e.currentTarget.valueAsNumber;
                if (n > 0 && n < 100 && n !== Number((pos * 100).toFixed(2)))
                  commit(positions.map((v, j) => (j === i ? n / 100 : v)));
                else
                  e.currentTarget.value = String(
                    Number((pos * 100).toFixed(2)),
                  );
              }}
            />
          </label>
          <button
            type="button"
            onClick={() => commit(positions.filter((_, j) => j !== i))}
          >
            Ta bort {noun} {i + 1}
          </button>
        </div>
      ))}
      <button
        type="button"
        disabled={positions.length >= (horizontal ? 40 : 16)}
        onClick={() => {
          const edges = [0, ...positions, 1];
          let i = 0;
          for (let j = 1; j < edges.length - 1; j++)
            if (edges[j + 1]! - edges[j]! > edges[i + 1]! - edges[i]!) i = j;
          commit([...positions, (edges[i]! + edges[i + 1]!) / 2]);
        }}
      >
        Lägg till {noun}
      </button>
      <button
        type="button"
        onClick={() =>
          change(
            horizontal ? { shelfHeightRatios: [] } : { bayWidthRatios: [] },
          )
        }
      >
        Fördela {horizontal ? "hyllor" : "fack"} jämnt
      </button>
    </details>
  );
}

export function ReferenceApproval({
  spec,
  change,
}: {
  spec: DesignSpec;
  change: (ref: ReferenceImageImport) => void;
}) {
  const ref = spec.reference_image_import!;
  const [checks, setChecks] = useState({ ...EMPTY_REFERENCE_CONFIRMATIONS });
  const issues = referenceConstructionIssues(spec, ref.construction);
  if (referenceImageVerificationIsCurrent(spec))
    return (
      <p>
        Den parametriska tolkningen är bekräftad för denna modell. Fysisk
        frisläppning hanteras separat.
      </p>
    );
  const labels: Record<keyof ReferenceImageConfirmedInputs, string> = {
    dimensions_measured: "Jag har mätt yttermått och installationsutrymme.",
    layout_confirmed:
      "Jag har jämfört fack, fronter, stöd och hyllplan med bilden.",
    material_confirmed: "Jag har kontrollerat stommaterial och tjocklek.",
    construction_assumptions_confirmed:
      "Jag har granskat rygg, infästningar och återstående konstruktionsantaganden.",
  };
  return (
    <fieldset>
      <legend>Bekräfta referenstolkningen</legend>
      {(Object.keys(labels) as (keyof ReferenceImageConfirmedInputs)[]).map(
        (key) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={checks[key]}
              onChange={(e) =>
                setChecks({ ...checks, [key]: e.target.checked })
              }
            />{" "}
            {labels[key]}
          </label>
        ),
      )}
      <button
        type="button"
        disabled={issues.length > 0 || !referenceConfirmationsComplete(checks)}
        onClick={() => {
          const confirmed: ReferenceImageImport = {
            ...ref,
            confirmed_inputs: checks,
            verification_status: "parametric_confirmed",
          };
          change({
            ...confirmed,
            verified_model_fingerprint: referenceVerificationFingerprint({
              ...spec,
              reference_image_import: confirmed,
            }),
          });
        }}
      >
        Bekräfta den parametriska tolkningen
      </button>
      <p>En känd avvikelse måste lösas före bekräftelse.</p>
    </fieldset>
  );
}
