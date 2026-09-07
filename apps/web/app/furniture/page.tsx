import { FurnitureWorkspacePage } from "@/components/furniture-workspace";
import { loadPublicRuntimeConfig } from "@/lib/runtime-config";
import { connection } from "next/server";

export default async function FurniturePage() {
  await connection();
  return <FurnitureWorkspacePage runtimeConfig={loadPublicRuntimeConfig()} />;
}
