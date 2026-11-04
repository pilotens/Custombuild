"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { CustombuildApiClient } from "@/lib/api-client";
import {
  sampleMeasuredWork,
  type MeasuredResult,
  type MeasuredWork,
} from "@/lib/measured-assembly";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import { MeasuredRegister } from "./measured-register";
import { MeasuredRoomPlanning } from "./measured-planning";
import { TrestleConstruction } from "./trestle-construction";
import css from "./measured-register.module.css";

export function MeasuredWorkbench({
  runtimeConfig,
}: {
  runtimeConfig: PublicRuntimeConfig;
}) {
  const [report, setReport] = useState<MeasuredResult>(),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false);
  const [returnPart, setReturnPart] = useState<string>();
  const [constructionVersion, setConstructionVersion] = useState(0);
  const serial = useRef(0);
  async function load(read: () => Promise<MeasuredWork>, keepPart?: string) {
    const epoch = ++serial.current;
    setLoading(true);
    setError("");
    if (!keepPart) setReport(undefined);
    try {
      const api = new CustombuildApiClient(
        runtimeConfig.apiUrl,
        undefined,
        runtimeConfig.developmentToken,
      );
      const result = await api.reviewMeasuredAssembly(await read());
      if (
        result.physical_cutting_authorized !== false ||
        !/^[0-9a-f]{64}$/.test(result.assembly_sha256)
      )
        throw Error("Granskningens identitet saknas.");
      if (epoch === serial.current) {
        setReport(result);
        setReturnPart(keepPart);
      }
    } catch (reason) {
      if (epoch === serial.current)
        setError(
          reason instanceof Error
            ? reason.message
            : "Arbetsfilen kunde inte granskas.",
        );
    } finally {
      if (epoch === serial.current) setLoading(false);
    }
  }
  return (
    <main className={css.workbench}>
      <header>
        <Link href="/furniture">Till möbelstudion</Link>
        <h1>Måttsatt montering</h1>
        <p>
          Öppna en arbetsritning med mått, bearbetningar och förband. Varje del
          granskas mot sina motdelar. Referensfiler ger inget
          tillverkningstillstånd.
        </p>
      </header>
      <TrestleConstruction key={constructionVersion} runtimeConfig={runtimeConfig} open={(work) => void load(async () => work)}
        openVerified={(verified) => { ++serial.current; setLoading(false); setError(""); setReturnPart(undefined); setReport(verified); }} />
      <div className={css.toolbar}>
        <label>
          Öppna arbetsritning (JSON){" "}
          <input
            type="file"
            accept=".json,application/json"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) setConstructionVersion(v => v + 1);
              if (file)
                void load(async () => {
                  if (file.size > 4 * 1024 * 1024)
                    throw Error("Arbetsritningen får vara högst 4 MB.");
                  return JSON.parse(await file.text()) as MeasuredWork;
                });
            }}
          />
        </label>
        <button onClick={() => void load(async () => sampleMeasuredWork())}>
          Prova ett ramhörn
        </button>
        <button onClick={() => void load(() => new CustombuildApiClient(
          runtimeConfig.apiUrl, undefined, runtimeConfig.developmentToken,
        ).measuredTrestleExample())}>Prova ett bockbord</button>
      </div>
      {loading && <p role="status">Granskar delarna …</p>}
      {error && <p role="alert">{error}</p>}
      {report && (
        <>
          <h2>{report.assembly.title}</h2>
          {(report.unreviewed_profile_parts?.length ?? 0) > 0 && <p role="status" className={css.attention}>
            {report.unreviewed_profile_parts.length} formprofiler visas i modellen men ingår inte
            i den rätvinkliga förbands- och kollisionskontrollen. Modellens montering är ofullständigt verifierad.
          </p>}
          <MeasuredRoomPlanning
            key={report.assembly_sha256 + "-room"}
            report={report}
            change={(input) => { setConstructionVersion(v => v + 1); void load(async () => input, returnPart); }}
          />
          <MeasuredRegister
            key={report.assembly_sha256}
            report={report}
            initialPart={returnPart}
            onChange={(input, id) => { setConstructionVersion(v => v + 1); void load(async () => input, id); }}
          />
        </>
      )}
    </main>
  );
}
