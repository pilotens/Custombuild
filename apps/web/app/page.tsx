import { CustombuildWorkspace } from "@/components/custombuild-workspace";
import { loadPublicRuntimeConfig } from "@/lib/runtime-config";
import { connection } from "next/server";

export default async function HomePage() {
  // Do not let Next bake deployment-specific public endpoints into the image.
  // The validated allow-list is read for each server runtime and serialized as
  // an explicit React Server Component prop.
  await connection();
  const runtimeConfig = loadPublicRuntimeConfig();
  return <CustombuildWorkspace runtimeConfig={runtimeConfig} />;
}
