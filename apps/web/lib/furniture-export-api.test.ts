// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient } from "./api-client";
import type { FurnitureExportReady } from "./furniture-workspace";

const project = "7b291920-f11c-4621-8c57-41468dc2aeda";
const job = "1317bce6-0bb3-438b-9099-f43f7540e577";
const designHash = "a".repeat(64);
const content = new TextEncoder().encode("review-package");
const origin = "https://api.example.test";
const path = `/v1/furniture/projects/${project}/exports/${job}/download`;
const client = new CustombuildApiClient(origin, "test-bearer-token");

async function ready(): Promise<FurnitureExportReady> {
  const sha256 = Buffer.from(await crypto.subtle.digest("SHA-256", content)).toString("hex");
  return { job_id: job, state: "succeeded", artifact_id: "8c11497f-6888-5ce8-aa04-25b4761a392f",
    file_name: "custombuild-design-review.zip", download_path: path, sha256,
    size_bytes: content.byteLength, content_type: "application/zip", design_hash: designHash,
    revision: 3, physical_cutting_authorized: false };
}

function response(artifact: FurnitureExportReady, body: Uint8Array<ArrayBuffer> = content) {
  const result = new Response(body, { headers: {
    "Content-Type": "application/zip", "Content-Length": String(artifact.size_bytes),
    Digest: `sha-256=${Buffer.from(artifact.sha256, "hex").toString("base64")}`,
    ETag: `"${artifact.sha256}"`,
  } });
  Object.defineProperty(result, "url", { value: origin + path });
  return result;
}

afterEach(() => vi.unstubAllGlobals());

describe("durable furniture export downloads", () => {
  it("fetches authenticated binary bytes and verifies headers, size and checksum", async () => {
    const artifact = await ready();
    const fetcher = vi.fn().mockResolvedValue(response(artifact));
    vi.stubGlobal("fetch", fetcher);
    const controller = new AbortController();
    const blob = await client.downloadFurnitureExport(project, job, 3, designHash, artifact, controller.signal);
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(content);
    expect(blob.type).toBe("application/zip");
    expect(fetcher).toHaveBeenCalledWith(origin + path, expect.objectContaining({
      cache: "no-store", redirect: "error", signal: controller.signal,
      headers: { Accept: "application/zip", Authorization: "Bearer test-bearer-token" },
    }));
  });

  it.each([
    { download_path: "https://attacker.example/download" },
    { download_path: "//attacker.example/download" },
    { download_path: path + "?redirect=elsewhere" },
    { job_id: "another-job" },
    { design_hash: "b".repeat(64) },
    { revision: 2 },
    { physical_cutting_authorized: true },
    { content_type: "text/html" },
    { size_bytes: 0 },
    { size_bytes: 16 * 1024 * 1024 + 1 },
    { size_bytes: 1.5 },
    { sha256: "not-a-digest" },
    { artifact_id: "not-an-artifact" },
    { file_name: "../unsafe.zip" },
  ])("rejects untrusted metadata before sending credentials: %j", async changes => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const artifact = { ...await ready(), ...changes } as unknown as FurnitureExportReady;
    await expect(client.downloadFurnitureExport(project, job, 3, designHash, artifact)).rejects.toThrow("metadata");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each(["Content-Length", "Content-Type", "Digest", "ETag"])("rejects incorrect %s", async header => {
    const artifact = await ready();
    const result = response(artifact);
    result.headers.set(header, "wrong");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(result));
    await expect(client.downloadFurnitureExport(project, job, 3, designHash, artifact)).rejects.toThrow("svarshuvuden");
  });

  it("rejects changed bytes even with successful status and correct metadata", async () => {
    const artifact = await ready();
    const changed = content.slice();
    changed[0] = changed[0]! ^ 1;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(artifact, changed)));
    await expect(client.downloadFurnitureExport(project, job, 3, designHash, artifact)).rejects.toThrow("SHA-256");
  });

  it("rejects truncated or oversized response bodies", async () => {
    const artifact = await ready();
    for (const body of [content.slice(1), new Uint8Array(content.length + 1)]) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(artifact, body)));
      await expect(client.downloadFurnitureExport(project, job, 3, designHash, artifact)).rejects.toThrow("storlek");
    }
  });

  it("rejects redirected binary responses", async () => {
    const artifact = await ready();
    const result = response(artifact);
    Object.defineProperty(result, "redirected", { value: true });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(result));
    await expect(client.downloadFurnitureExport(project, job, 3, designHash, artifact)).rejects.toThrow("API-kanalen");
  });

  it("requires authenticated identity before download", async () => {
    const artifact = await ready();
    vi.stubGlobal("fetch", vi.fn());
    await expect(new CustombuildApiClient(origin).downloadFurnitureExport(project, job, 3, designHash, artifact))
      .rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("accepts durable cancellation and rejects wrong-job status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => Response.json({
      job_id: job, state: "cancelled", message: "Revision changed",
    })));
    expect(await client.furnitureExportResult(project, job)).toMatchObject({ state: "cancelled" });
    await expect(client.furnitureExportResult(project, "another-job")).rejects.toThrow("begärda jobbet");
  });
});
