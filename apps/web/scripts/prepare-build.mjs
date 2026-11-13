// Next can retain the previous app-router manifest on an incremental standalone
// build. Only remove compiled server output from a recognized Next build.
import { existsSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const app = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(app, process.env.NEXT_DIST_DIR ?? ".next");
const manifest = join(output, "required-server-files.json");

if (existsSync(manifest)) {
  const metadata = JSON.parse(readFileSync(manifest, "utf8"));
  if (metadata.appDir === app && resolve(app, metadata.config.distDir) === output) {
    rmSync(join(output, "server"), { recursive: true, force: true });
  }
}
