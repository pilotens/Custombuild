import { connection } from "next/server";
import { FurnitureWorkspacePage } from "@/components/furniture-workspace";
import { FURNITURE_FAMILY_LABELS, type FurnitureFamily } from "@/lib/furniture-workspace";
import { loadPublicRuntimeConfig } from "@/lib/runtime-config";

export default async function FurniturePage({
  searchParams,
}: { searchParams: Promise<{ family?: string; project?: string }> }) {
  await connection();
  const { family, project } = await searchParams;
  const initialFamily = family && Object.hasOwn(FURNITURE_FAMILY_LABELS, family)
    ? family as FurnitureFamily : undefined;
  return <FurnitureWorkspacePage runtimeConfig={loadPublicRuntimeConfig()} initialFamily={initialFamily} initialProject={project} />;
}
