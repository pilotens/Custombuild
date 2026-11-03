import { connection } from "next/server";
import { MeasuredWorkbench } from "@/components/measured-workbench";
import { loadPublicRuntimeConfig } from "@/lib/runtime-config";

export default async function MeasuredAssemblyPage() {
  await connection();
  return (
    <MeasuredWorkbench runtimeConfig={loadPublicRuntimeConfig(process.env)} />
  );
}
