import { cpSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const projectRoot = resolve(import.meta.dirname, "..");
const candidates = [
  join(projectRoot, ".next", "standalone", "apps", "web", "server.js"),
  join(projectRoot, ".next", "standalone", "server.js"),
];
const serverPath = candidates.find(existsSync);

if (!serverPath) {
  throw new Error("Standalone-servern saknas. Kör `npm run build` före `npm start`.");
}

const standaloneRoot = dirname(serverPath);
const staticSource = join(projectRoot, ".next", "static");
if (existsSync(staticSource)) {
  cpSync(staticSource, join(standaloneRoot, ".next", "static"), { recursive: true });
}
const publicSource = join(projectRoot, "public");
if (existsSync(publicSource)) {
  cpSync(publicSource, join(standaloneRoot, "public"), { recursive: true });
}

await import(pathToFileURL(serverPath).href);
